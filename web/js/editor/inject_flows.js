/**
 * Editor 画布的三个提交流程与连线取值（自 editor_canvas.js 机械抽出，行为不变）。
 *
 * applyPose（📥姿势 本地注入 POST /prompt）、applyCostume（📥服装 本地注入）、
 * runAiSplit（⚡拆解 子图提交：graphToPrompt 裁剪编辑器及上游后单节点执行），
 * 以及 linkSourceValue（连线源的值尽力解析）与 referenceLinkOverrideWarning
 * （有连线且 widget 值不同 → 一次性提示以连线值为准）。
 *
 * v1.8 提交竞态逻辑逐字保持：本地注入执行极快，executed 可能先于 /prompt 响应到达，
 * 提交前先记 submitPrevId 并清空 lastSubmitId（submitToken/submitSeq 防旧响应回写）。
 *
 * ctx = { node, M, fx, get wSource(){...}, get wEdits(){...} }：M 为 node._mps 状态袋
 * （同一对象）；fx 为跨块函数注册表（writeEdits/currentSig/currentSourceSig 来自
 * edits_sync，resetSplitBtn 来自 template_panel）；widget 通过 getter 注入以保持
 * rebindWidgets 换绑后的实时引用。
 */
import { app } from "/scripts/app.js";
import { buildSplitPayload, resolvePoseSource, costumeIsSensitive, textMentionsCostume } from "./pose.js";
import { textHash } from "./reverse.js";

export function mountInjectFlows(ctx) {
  const { node, M, fx } = ctx;

  // 📥姿势按钮：本地注入（不调 LLM）。选中角色→只注入它；未选中→按左右/面积规则
  // 自动分配。与 ⚡拆解 同一条 ui.mps_draft 写回通道；校验失败会作为节点错误显示。
  async function applyPose(btn) {
    const api = (typeof app !== "undefined" && app.api) || (window.app && window.app.api);
    if (!api || !api.clientId) { alert("前端尚未就绪，稍后再试"); return; }
    const fail = (t) => { M.els.status.textContent = t; M.els.status.className = "mps-status err"; };
    if (M.submitting) {
      fail("已有拆解/注入任务在飞，请等待完成");
      return;
    }
    if (!M.S || M.S.kind !== "layout") {
      fail("画布还没有拆解完成的布局：先 ⚡拆解（或贴入 layout 草稿）再注入姿势");
      return;
    }
    const linked = (name) => {
      const inp = (node.inputs || []).find((x) => x && x.name === name);
      return !!inp && inp.link != null;
    };
    const wPoseIn = node.widgets.find((x) => x.name === "pose_in");
    const wPoseIn2 = node.widgets.find((x) => x.name === "pose_in_2");
    // 姿势三入口：连线 > 手填/点选 widget；两者都空才拒绝（v1.7.1 起 pose_in 是 widget）
    const srcA = resolvePoseSource({ linked: linked("pose_in"), widgetValue: wPoseIn && wPoseIn.value });
    const srcB = resolvePoseSource({ linked: linked("pose_in_2"), widgetValue: wPoseIn2 && wPoseIn2.value });
    if (srcA === "none" && srcB === "none") {
      fail("没有可用的姿势来源：点 🎯姿势①/② 选择、在 pose_in/pose_in_2 手填，或连接姿势来源（连线值优先）");
      return;
    }
    const wMode = node.widgets.find((x) => x.name === "pose_mode");
    if (wMode && wMode.value === "关闭") {
      fail("pose_mode=关闭：请先切回 自动 / 强制双人 再注入");
      return;
    }
    const items = (M.S.d && M.S.d.items) || [];
    let sel = -1;
    if (M.sel != null && items[M.sel] && items[M.sel].label === "character") sel = M.sel;
    M.submitting = true;
    btn.disabled = true;
    btn.textContent = "⏳注入中…";
    M.poseBtn = btn;
    M.splitAt = Date.now(); // 复用空队列监视器的防卡死计时
    M.posePendingAt = Date.now(); // 运行状态：区分 mps_draft 是注入产物
    M.costumePendingAt = 0;
    M.splitPendingAt = 0;
    M.posePairHint = /双人/.test(String((node.widgets.find((x) => x.name === "pose_in") || {}).value || ""));
    // 记录本次注入提交对应的 source：结果回来时若 source 已变则丢弃旧结果（P0-1）
    M.poseSrcSig = fx.currentSourceSig();
    M.costumeSrcSig = "";
    M.splitSrcSig = "";
    M.els.status.textContent = "⏳ 姿势注入中…（本地写入不调 LLM；"
      + (sel >= 0 ? "注入到选中角色" : "按左右/面积规则自动分配") + "）";
    M.els.status.className = "mps-status";
    try {
      if (typeof app.graphToPrompt !== "function") throw new Error("当前前端不支持 graphToPrompt");
      fx.writeEdits(); // 冲洗画布最新内容到 edits widget，提交的就是眼前这份
      const full = await app.graphToPrompt();
      const all = (full && (full.output || full)) || {};
      const wAi = node.widgets.find((x) => x.name === "ai_draft");
      const inputs = buildSplitPayload(all, node.id, {
        edits: ctx.wEdits.value,           // 注入必须保留画布内容（拆解默认是清空重拆）
        ai_draft: !!(wAi && wAi.value),
        force_split: false,
        apply_pose: sel >= 0 ? sel : -2, // -2=自动分配（后端约定），>=0=选中角色下标
        apply_costume: -1,                // 明确关闭另一条注入通道（防旧值残留）
      });
      if (!inputs) throw new Error("编辑器被旁路或未注册（检查 Ctrl+B / 节点状态）");
      // 本地注入执行极快，executed 可能先于 /prompt 响应到达：先记上一提交 id
      // 并清空 lastSubmitId，结果先到时按“本次提交”接受（见 submit_gate.js）。
      const submitToken = ++M.submitSeq;
      M.submitPrevId = M.lastSubmitId || M.lastDraftPid || "";
      M.lastSubmitId = "";
      const r = await api.fetchApi("/prompt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: inputs, client_id: api.clientId }),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error((j.error && j.error.message) || ("HTTP " + r.status));
      }
      const j = await r.json().catch(() => ({}));
      // 结果先到并已复位时不要再把旧 pid 写回，否则下一条结果会被误判 foreign。
      if (M.submitSeq === submitToken && M.submitting) {
        M.lastSubmitId = (j && j.prompt_id) || "";
      }
      const submitted = (inputs[String(node.id)] && inputs[String(node.id)].inputs) || {};
      console.info("[MPS] submit", {
        kind: "pose",
        prompt_id: M.lastSubmitId,
        srcSig: M.poseSrcSig,
        editsLen: (submitted.edits || "").length,
        editsHash: textHash(submitted.edits || ""),
        pose_in: submitted.pose_in,
        pose_in_2: submitted.pose_in_2,
        pose_mode: submitted.pose_mode,
        costume_in: submitted.costume_in,
      });
    } catch (e) {
      fx.resetSplitBtn();
      fail("姿势注入提交失败: " + e.message);
    }
  }

  // 📥服装按钮：与 📥姿势 同一条本地注入通道（不调 LLM），只是写入 ；服装： 段。
  // 选中角色→只注入它；未选中→双参考按左右分配、单参考给最突出角色。
  async function applyCostume(btn) {
    const api = (typeof app !== "undefined" && app.api) || (window.app && window.app.api);
    if (!api || !api.clientId) { alert("前端尚未就绪，稍后再试"); return; }
    const fail = (t) => { M.els.status.textContent = t; M.els.status.className = "mps-status err"; };
    if (M.submitting) {
      fail("已有拆解/注入任务在飞，请等待完成");
      return;
    }
    if (!M.S || M.S.kind !== "layout") {
      fail("画布还没有拆解完成的布局：先 ⚡拆解（或贴入 layout 草稿）再注入服装");
      return;
    }
    const linked = (name) => {
      const inp = (node.inputs || []).find((x) => x && x.name === name);
      return !!inp && inp.link != null;
    };
    const wCost = node.widgets.find((x) => x.name === "costume_in");
    const wCost2 = node.widgets.find((x) => x.name === "costume_in_2");
    const srcA = resolvePoseSource({ linked: linked("costume_in"), widgetValue: wCost && wCost.value });
    const srcB = resolvePoseSource({ linked: linked("costume_in_2"), widgetValue: wCost2 && wCost2.value });
    if (srcA === "none" && srcB === "none") {
      fail("没有可用的服装来源：连接 Costume Gallery 的 description 到 costume_in/costume_in_2，或在服装参考面板手填");
      return;
    }
    const wMode = node.widgets.find((x) => x.name === "costume_mode");
    if (wMode && wMode.value === "关闭") {
      fail("costume_mode=关闭：请先切回 自动 再注入");
      return;
    }
    // 敏感分类提示（不拦截）：只影响提示词内容，不会额外发送到外部服务。
    const sensitive = ["costume_in", "costume_in_2"].some((name) => {
      const ww = node.widgets.find((x) => x && x.name === name);
      const lv = linkSourceValue(name);
      return costumeIsSensitive(lv != null ? lv : (ww && ww.value));
    });
    // 画布 summary/角色描述里已有旧服装时，本地注入只追加服装段，可能新旧混合
    // （反推草稿尤其常见：summary 或 object 里还留着旧连衣裙/背心）。
    const oldCostumeWarn = (() => {
      const dd = M.S && M.S.d;
      if (!dd) return "";
      const texts = [dd.summary && dd.summary.caption,
        ...(dd.items || []).filter((x) => x && x.label === "character").map((x) => x.caption)];
      return texts.some(textMentionsCostume)
        ? "\n⚠ 画布 summary/角色描述里已有旧服装措辞：📥服装只追加「；服装：」段，模型可能混合新旧衣服；要彻底替换请用 ⚡拆解"
        : "";
    })();
    const items = (M.S.d && M.S.d.items) || [];
    let sel = -1;
    if (M.sel != null && items[M.sel] && items[M.sel].label === "character") sel = M.sel;
    M.submitting = true;
    btn.disabled = true;
    btn.textContent = "⏳注入中…";
    M.costumeBtn = btn;
    M.splitAt = Date.now();
    M.costumePendingAt = Date.now();
    M.posePendingAt = 0;
    M.splitPendingAt = 0;
    M.posePairHint = false;
    M.costumeSrcSig = fx.currentSourceSig();
    M.poseSrcSig = "";
    M.splitSrcSig = "";
    M.els.status.textContent = "⏳ 服装注入中…（本地写入不调 LLM；"
      + (sel >= 0 ? "注入到选中角色" : "按左右/面积规则自动分配") + "）"
      + (sensitive ? "\n⚠ 该服装参考属于内衣/性感/泳装分类，请确认用途与内容合规" : "")
      + oldCostumeWarn;
    M.els.status.className = "mps-status";
    try {
      if (typeof app.graphToPrompt !== "function") throw new Error("当前前端不支持 graphToPrompt");
      fx.writeEdits(); // 冲洗画布最新内容到 edits widget，提交的就是眼前这份
      const full = await app.graphToPrompt();
      const all = (full && (full.output || full)) || {};
      const wAi = node.widgets.find((x) => x.name === "ai_draft");
      const inputs = buildSplitPayload(all, node.id, {
        edits: ctx.wEdits.value,
        ai_draft: !!(wAi && wAi.value),
        force_split: false,
        apply_costume: sel >= 0 ? sel : -2,
        apply_pose: -1,
      });
      if (!inputs) throw new Error("编辑器被旁路或未注册（检查 Ctrl+B / 节点状态）");
      const submitToken = ++M.submitSeq;
      M.submitPrevId = M.lastSubmitId || M.lastDraftPid || "";
      M.lastSubmitId = "";
      const r = await api.fetchApi("/prompt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: inputs, client_id: api.clientId }),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error((j.error && j.error.message) || ("HTTP " + r.status));
      }
      const j = await r.json().catch(() => ({}));
      if (M.submitSeq === submitToken && M.submitting) {
        M.lastSubmitId = (j && j.prompt_id) || "";
      }
      const submitted = (inputs[String(node.id)] && inputs[String(node.id)].inputs) || {};
      console.info("[MPS] submit", {
        kind: "costume",
        prompt_id: M.lastSubmitId,
        srcSig: M.costumeSrcSig,
        editsLen: (submitted.edits || "").length,
        editsHash: textHash(submitted.edits || ""),
        costume_in: submitted.costume_in,
        costume_in_2: submitted.costume_in_2,
        costume_mode: submitted.costume_mode,
      });
    } catch (e) {
      fx.resetSplitBtn();
      fail("服装注入提交失败: " + e.message);
    }
  }
  // 连线源的值（尽力解析）：用于判断 ⚡拆解 时 widget 里点选/手填的值是否真的被连线覆盖。
  function linkSourceValue(name) {
    const idx = (node.inputs || []).findIndex((i) => i && i.name === name);
    const inp = idx >= 0 ? node.inputs[idx] : null;
    if (!inp || inp.link == null) return null;
    const g = node.graph || app.graph;
    let link = null;
    try {
      if (g && g.links) {
        if (typeof g.links.get === "function") link = g.links.get(inp.link) || g.links.get(String(inp.link));
        else if (Array.isArray(g.links)) link = g.links.find((l) => l && String(l.id) === String(inp.link));
        else link = g.links[inp.link];
      }
    } catch (e) { /* noop */ }
    const origin = link && g && typeof g.getNodeById === "function" ? g.getNodeById(link.origin_id) : null;
    if (!origin) return null;
    const outName = origin.outputs && origin.outputs[link.origin_slot] && origin.outputs[link.origin_slot].name;
    for (const nm of [outName, name, "value", "text", "description"]) {
      if (!nm) continue;
      const ww = (origin.widgets || []).find((x) => x && x.name === nm && typeof x.value === "string");
      if (ww) return ww.value;
    }
    const strings = (origin.widgets || []).filter((x) => x && typeof x.value === "string" && x.value.trim());
    return strings.length === 1 ? strings[0].value : null;
  }

  // 「有连线 + widget 非空且值不同」→ 返回一次提示文本（ComfyUI 提交以连线值为准）。
  function referenceLinkOverrideWarning() {
    if (M.poseLinkWarned) return "";
    const hit = [];
    for (const name of ["pose_in", "pose_in_2", "costume_in", "costume_in_2"]) {
      const idx = (node.inputs || []).findIndex((i) => i && i.name === name);
      const inp = idx >= 0 ? node.inputs[idx] : null;
      if (!inp || inp.link == null) continue;
      const ww = node.widgets.find((x) => x && x.name === name);
      const wv = ww && String(ww.value || "").trim();
      if (!wv) continue;
      const lv = linkSourceValue(name);
      if (lv != null && lv.trim() === wv) continue;
      hit.push(name);
    }
    if (!hit.length) return "";
    M.poseLinkWarned = true;
    return `⚠ ${hit.join(" / ")} 已连线：本次 ⚡拆解 以连线值为准（面板里手填的值不会生效）`;
  }

  async function runAiSplit(btn) {
    const api = (typeof app !== "undefined" && app.api) || (window.app && window.app.api);
    if (!api || !api.clientId) { alert("前端尚未就绪，稍后再试"); return; }
    if (M.submitting) {
      M.els.status.textContent = "已有拆解/注入任务在飞，请等待完成";
      M.els.status.className = "mps-status err";
      return;
    }
    // 判断"要不要拆解"以 source 为准（用户改了散文就要能重新拆解），
    // edits 只是上一次的结果写回，不能因为它含 items 就拦截新散文的拆解。
    const src = (ctx.wSource.value || "").trim();
    const cur = src || (ctx.wEdits.value || "").trim();
    const looksJson = cur.startsWith("{") || cur.startsWith("[");
    if (!cur) {
      M.els.status.textContent = "source 为空：先在 source 里贴入散文提示词";
      M.els.status.className = "mps-status err";
      return;
    }
    if (looksJson) {
      // 锁定 sig，防止 1.4s 防抖重建立刻覆盖这条提示
      M.builtSig = M.pendSig = fx.currentSig();
      M.els.status.textContent = "源已是布局草稿/JSON，无需拆解（想重拆：把 source 换成散文）";
      M.els.status.className = "mps-status";
      return;
    }
    btn.disabled = true;
    btn.textContent = "⏳拆解中…";
    M.submitting = true;
    M.splitBtn = btn;
    M.splitAt = Date.now();
    M.splitPendingAt = Date.now(); // 运行状态：本次 mps_draft 是拆解产物
    M.posePendingAt = 0;
    M.costumePendingAt = 0;
    // 记录本次拆解提交对应的 source：LLM 执行期间用户改 source 时，旧结果晚到
    // 会被丢弃，避免旧画布被盖上当前 source 的签名（P0-1）
    M.splitSrcSig = fx.currentSourceSig();
    M.poseSrcSig = "";
    M.costumeSrcSig = "";
    const linkWarn = referenceLinkOverrideWarning();
    const costumeTexts = ["costume_in", "costume_in_2"].map((name) => {
      const ww = node.widgets.find((x) => x && x.name === name);
      const lv = linkSourceValue(name);
      return String(lv != null ? lv : (ww && ww.value) || "");
    });
    const sensitiveWarn = costumeTexts.some(costumeIsSensitive)
      ? "\n⚠ 服装参考属于内衣/性感/泳装分类，将随拆解发送到 LLM，请确认用途与内容合规"
      : "";
    M.els.status.textContent = "⏳ AI 拆解中…（LLM 生成布局草稿，通常 10~120 秒，完成后画布自动刷新）"
      + (linkWarn ? "\n" + linkWarn : "") + sensitiveWarn;
    M.els.status.className = "mps-status";
    try {
      // 以整个工作流当前状态取数，再裁剪出编辑器及其上游子图：
      // pose_in / pose_in_2 / underlay 等链接输入的上游（如 Pose Gallery）必须
      // 一并提交，否则拆解 LLM 收不到姿势描述。
      if (typeof app.graphToPrompt !== "function") throw new Error("当前前端不支持 graphToPrompt");
      const full = await app.graphToPrompt();
      const all = (full && (full.output || full)) || {};
      // 显式关闭两条注入通道：隐藏 widget 若被旧工作流写成非 -1，会走注入分支
      // 而不是 AI 拆解，导致“⚡拆解”实际不调 LLM。
      const inputs = buildSplitPayload(all, node.id, { apply_pose: -1, apply_costume: -1 });
      if (!inputs) throw new Error("编辑器被旁路或未注册，无法单独拆解（检查 Ctrl+B / 节点状态）");
      const submitToken = ++M.submitSeq;
      M.submitPrevId = M.lastSubmitId || M.lastDraftPid || "";
      M.lastSubmitId = "";
      const r = await api.fetchApi("/prompt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: inputs, client_id: api.clientId }),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error((j.error && j.error.message) || ("HTTP " + r.status));
      }
      const j = await r.json().catch(() => ({}));
      if (M.submitSeq === submitToken && M.submitting) {
        M.lastSubmitId = (j && j.prompt_id) || "";
      }
      const submitted = (inputs[String(node.id)] && inputs[String(node.id)].inputs) || {};
      console.info("[MPS] submit", {
        kind: "split",
        prompt_id: M.lastSubmitId,
        srcSig: M.splitSrcSig,
        editsLen: (submitted.edits || "").length,
        editsHash: textHash(submitted.edits || ""),
        pose_in: submitted.pose_in,
        pose_in_2: submitted.pose_in_2,
        pose_mode: submitted.pose_mode,
        costume_in: submitted.costume_in,
        costume_in_2: submitted.costume_in_2,
        costume_mode: submitted.costume_mode,
      });
    } catch (e) {
      fx.resetSplitBtn();
      M.els.status.textContent = "拆解提交失败: " + e.message;
      M.els.status.className = "mps-status err";
    }
  }

  return { applyPose, applyCostume, runAiSplit, linkSourceValue, referenceLinkOverrideWarning };
}
