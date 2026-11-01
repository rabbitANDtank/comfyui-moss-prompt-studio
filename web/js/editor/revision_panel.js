/**
 * AI 定向改稿审批卡（P1a-2）。
 *
 * 数据源：ui.mps_ops（run_status.processExecData 解析后直传本面板，不动画布）。
 * 卡片内容：逐条 op（reason + 词级 diff + 虚线预览坐标）+ 拒收清单 + dropped +
 * lint-in-the-loop（text_fit 容量警告 + 改后 token 估算）+ 🔒锁定文字开关。
 * 采纳：勾选子集 → fx.applyOps 镜像算出结果草稿 → 一次 undo 快照写回（与注入同路）。
 * 拒绝：关卡，零副作用。全部采纳/采纳所选 都走 decisionForOps（JS 镜像 applyOps）——
 * 🔒锁文字对两者同样生效（复查 B1），后端全集结果经镜像重算逐字节一致（契约测试钉死）。
 *
 * 纯函数（decisionForOps/formatOpSummary/diffForOp/opTouchesText/revisionLint）导出供 Node 测试；
 * DOM 挂载 openRevisionPanel/closeRevisionPanel。
 */
import { applyOps, wordDiff } from "./edit_ops.js";
import { splitCaptionSegments } from "./pose.js";
import { estimateTextFit, estimatePromptTokens, TOKEN_WARN_LIMIT } from "./text_fit.js";

/** 判断一条 op 是否触碰可见文字（🔒锁文字开关作用面）。 */
export function opTouchesText(op) {
  if (!op || typeof op !== "object") return false;
  if (op.op === "set_text") return true;
  if (op.op === "add_item") return !!(op.item && (op.item.label === "text" || "text" in op.item));
  return false;
}

/** op 的展示行摘要（target/label/变化类型一行字）。draft 供 remove/add 之外的目标取名。 */
export function formatOpSummary(op, draft) {
  const items = (draft && draft.items) || [];
  const t = op && op.target;
  const nameOf = (i) => {
    const it = items[i];
    if (!it) return `#${String(i)}`;
    const head = splitCaptionSegments(it.caption || "").head;
    return `#${String(i)} ${head.slice(0, 12)}`;
  };
  switch (op && op.op) {
    case "set_caption": return `改描述 · ${nameOf(t && t.index)}`;
    case "set_segment": return `改${op.segment === "pose" ? "姿势" : "服装"}段 · ${nameOf(t && t.index)}`;
    case "set_text": return `改文字 · ${nameOf(t && t.index)}`;
    case "move_resize": return `移动/缩放 · ${nameOf(t && t.index)}`;
    case "add_item": return "新增条目（追加到末尾）";
    case "remove_item": return `删除 · ${nameOf(t && t.index)}`;
    case "set_summary": return "改全局 " + Object.keys(op.fields || {}).join("/");
    default: return "未知操作";
  }
}

/** 一条 op 的 diff 段落：旧文本 → 新文本（词级）；无文本变化的 op 给 null。 */
export function diffForOp(op, draft) {
  if (!op || typeof op !== "object") return null;
  const items = (draft && draft.items) || [];
  const t = op.target;
  const it = t ? items[t.index] : null;
  if (op.op === "set_caption" && it) {
    const oldHead = splitCaptionSegments(it.caption || "").head;
    return { oldText: oldHead, newText: String(op.head || ""), field: "描述（分段保留）" };
  }
  if (op.op === "set_text" && it) {
    return { oldText: String(it.text || ""), newText: String(op.text || ""), field: "可见文字" };
  }
  if (op.op === "set_summary" && (op.fields || {}).caption != null && draft && draft.summary) {
    return { oldText: String(draft.summary.caption || ""), newText: String(op.fields.caption), field: "全局描述" };
  }
  return null;
}

/** 采纳决策：勾选集 → 应用结果 + 提示信息。锁文字=触碰 text 的 op 强制不选。 */
export function decisionForOps(payload, pickedKeys, { lockText = false } = {}) {
  const ops = (payload && payload.ops) || [];
  const chosen = ops.filter((op, i) => pickedKeys.has(String(i))
    && !(lockText && opTouchesText(op)));
  if (!chosen.length) {
    return { ok: false, reason: "empty", chosen: [], draft: null };
  }
  const scope = (payload && payload.scope && Array.isArray(payload.scope.indexes))
    ? payload.scope : null;
  const out = applyOps(payload.base_draft, chosen, scope);
  if (out.error) return { ok: false, reason: "invalid", error: out.error, chosen, draft: null };
  return { ok: true, chosen, draft: out.draft, rejected: out.rejected, dropped: out.dropped };
}

/** 审批卡的 lint 提示：涉及 text/新增的条目跑容量估算 + 整稿 token 估算。 */
export function revisionLint(proposedDraft) {
  const warns = [];
  if (!proposedDraft || !Array.isArray(proposedDraft.items)) return warns;
  proposedDraft.items.forEach((it, i) => {
    if (!it || it.label !== "text") return;
    const fit = estimateTextFit(it);
    if (fit && !fit.ok) {
      warns.push(`items[${String(i)}] 文字容量不足：估计 ${String(fit.lines)} 行 × ${String(Math.round(fit.fontSize))}px 超出框（建议放大框或重排）`);
    }
  });
  const tokens = estimatePromptTokens(JSON.stringify(proposedDraft));
  if (tokens > TOKEN_WARN_LIMIT) {
    warns.push(`改后提示词粗估约 ${String(tokens)} tokens（>${String(TOKEN_WARN_LIMIT)}），可能被编码器截断`);
  }
  return warns;
}

export function mountRevisionPanel(ctx) {
  const { M, fx } = ctx;
  const { root } = ctx.dom;

  function closeRevisionPanel() {
    const old = root.querySelector(".mps-revision");
    if (old) old.remove();
  }

  /** 打开审批卡。payload 已含 base_draft/sig 校验后的 ops 载荷。 */
  function openRevisionPanel(payload) {
    closeRevisionPanel();
    fx.closePosePicker && fx.closePosePicker();
    fx.closeTemplatePanel && fx.closeTemplatePanel();
    const ops = payload.ops || [];
    const rejected = payload.rejected || [];
    const dropped = payload.dropped || 0;
    const picked = new Set(ops.map((_, i) => String(i)));
    let lockText = false;

    const panel = document.createElement("div");
    panel.className = "mps-revision";
    panel.style.cssText = "border:1px solid #3a4a5a;border-radius:8px;background:#1a2026;margin:4px 0;padding:8px;";

    const head = document.createElement("div");
    head.style.cssText = "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px;";
    const title = document.createElement("span");
    title.style.cssText = "color:#9bb7d4;font-weight:600;";
    title.textContent = `⤢ AI 改稿提案（${String(ops.length)} 条${rejected.length ? ` · ${String(rejected.length)} 条被拒` : ""}${dropped ? ` · ${String(dropped)} 条超限丢弃` : ""}）`;
    const reqSpan = document.createElement("span");
    reqSpan.style.cssText = "color:#888;font-size:11px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
    reqSpan.textContent = `「${payload.request || ""}」`;
    reqSpan.title = payload.request || "";
    const closeBtn = document.createElement("button");
    closeBtn.className = "mps-mini";
    closeBtn.textContent = "✕ 关闭（放弃提案）";
    closeBtn.onclick = () => { closeRevisionPanel(); };
    head.append(title, reqSpan, closeBtn);
    panel.appendChild(head);

    const list = document.createElement("div");
    list.style.cssText = "max-height:280px;overflow-y:auto;display:flex;flex-direction:column;gap:4px;";
    panel.appendChild(list);

    const rowOf = (op, i) => {
      const row = document.createElement("label");
      row.style.cssText = "display:flex;gap:6px;align-items:flex-start;border:1px solid #2c3844;border-radius:6px;padding:4px 6px;cursor:pointer;";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = picked.has(String(i));
      cb.onchange = () => { if (cb.checked) picked.add(String(i)); else picked.delete(String(i)); renderFoot(); };
      const body = document.createElement("div");
      body.style.cssText = "flex:1;min-width:0;";
      const line1 = document.createElement("div");
      line1.style.cssText = "font-size:12px;color:#c8d6e5;";
      line1.textContent = formatOpSummary(op, payload.base_draft);
      const line2 = document.createElement("div");
      line2.style.cssText = "font-size:11px;color:#8899aa;";
      line2.textContent = op.reason || "";
      body.append(line1, line2);
      const df = diffForOp(op, payload.base_draft);
      if (df) {
        const diffEl = document.createElement("div");
        diffEl.style.cssText = "font-size:11px;margin-top:2px;word-break:break-all;";
        for (const seg of wordDiff(df.oldText, df.newText)) {
          const sp = document.createElement("span");
          if (seg.type === "add") sp.style.cssText = "background:#1d3a24;color:#7ddc9a;";
          else if (seg.type === "del") sp.style.cssText = "background:#3a1d1d;color:#e39a9a;text-decoration:line-through;";
          sp.textContent = seg.text;
          diffEl.appendChild(sp);
        }
        body.appendChild(diffEl);
      }
      if (opTouchesText(op)) {
        row.dataset.touchesText = "1";
      }
      row.append(cb, body);
      row._cb = cb;
      return row;
    };

    const rows = ops.map((op, i) => rowOf(op, i));
    rows.forEach((r) => list.appendChild(r));
    if (!ops.length) {
      const empty = document.createElement("div");
      empty.style.cssText = "color:#8899aa;font-size:12px;padding:6px;";
      empty.textContent = "没有可采纳的修改" + (rejected.length ? "（下方为被拒原因）" : "——LLM 认为无需改动，或要求过于含糊");
      list.appendChild(empty);
    }
    if (rejected.length) {
      const rj = document.createElement("div");
      rj.style.cssText = "margin-top:4px;border-top:1px dashed #2c3844;padding-top:4px;";
      const rt = document.createElement("div");
      rt.style.cssText = "color:#8899aa;font-size:11px;";
      rt.textContent = `已拒收 ${String(rejected.length)} 条（不进画布）：`;
      rj.appendChild(rt);
      rejected.slice(0, 6).forEach((r) => {
        const l = document.createElement("div");
        l.style.cssText = "color:#776;font-size:11px;";
        l.textContent = `· ${formatOpSummary(r.op, payload.base_draft)} — ${r.reason}`;
        rj.appendChild(l);
      });
      if (rejected.length > 6) {
        const more = document.createElement("div");
        more.style.cssText = "color:#776;font-size:11px;";
        more.textContent = `…另有 ${String(rejected.length - 6)} 条`;
        rj.appendChild(more);
      }
      panel.appendChild(rj);
    }

    // 🔒 锁定文字 + lint 提示 + 采纳按钮（renderFoot 重算）
    const foot = document.createElement("div");
    foot.style.cssText = "margin-top:6px;display:flex;flex-direction:column;gap:4px;";
    const tools = document.createElement("div");
    tools.style.cssText = "display:flex;gap:8px;align-items:center;flex-wrap:wrap;";
    const lockLabel = document.createElement("label");
    lockLabel.style.cssText = "font-size:11px;color:#9bb7d4;cursor:pointer;display:flex;gap:4px;align-items:center;";
    const lockCb = document.createElement("input");
    lockCb.type = "checkbox";
    lockCb.checked = lockText;
    lockCb.onchange = () => {
      lockText = lockCb.checked;
      if (lockText) {
        rows.forEach((r, i) => {
          if (r.dataset.touchesText === "1") { r._cb.checked = false; picked.delete(String(i)); }
        });
      }
      renderFoot();
    };
    lockLabel.append(lockCb, document.createTextNode("🔒锁定画面文字（改动可见文字的项不采纳）"));
    tools.appendChild(lockLabel);
    foot.appendChild(tools);
    const lintEl = document.createElement("div");
    lintEl.style.cssText = "font-size:11px;color:#c9a86a;white-space:pre-wrap;";
    foot.appendChild(lintEl);
    const btns = document.createElement("div");
    btns.style.cssText = "display:flex;gap:8px;";
    foot.appendChild(btns);
    panel.appendChild(foot);
    // 与模板面板同款插入位置：工具栏之后、运行状态行之前
    const { runStatus } = ctx.dom;
    runStatus ? root.insertBefore(panel, runStatus) : root.appendChild(panel);

    const mkBtn = (text, primary) => {
      const b = document.createElement("button");
      b.className = "mps-mini" + (primary ? " mps-primary" : "");
      b.textContent = text;
      return b;
    };

    function renderFoot() {
      lintEl.textContent = "";
      // 锁后集合：🔒锁文字对"全部采纳"同样生效（复查 B1），chosen 也按锁过滤
      const chosen = ops.filter((op, i) => picked.has(String(i))
        && !(lockText && opTouchesText(op)));
      // lint 用"勾选子集应用后的草稿"预演（与采纳同一条代码路径）
      const preview = decisionForOps(payload, picked, { lockText });
      const warns = preview.ok ? revisionLint(preview.draft) : [];
      if (warns.length) lintEl.textContent = "\u26a0 " + warns.join("\n\u26a0 ");
      btns.textContent = "";
      const usable = ops.filter((op) => !(lockText && opTouchesText(op))).length;
      const lockedN = ops.length - usable;
      const btnAll = mkBtn(lockedN > 0
        ? `全部采纳（${String(usable)} 条，\ud83d\udd12${String(lockedN)} 条被锁排除）`
        : `全部采纳（${String(ops.length)} 条）`, true);
      btnAll.disabled = usable <= 0;
      btnAll.onclick = () => {
        const dec = decisionForOps(payload, new Set(ops.map((_, i) => String(i))), { lockText });
        if (!dec.ok) { lintEl.textContent = "\u274c " + (dec.error || "全部操作被\ud83d\udd12锁定排除：解除锁定或改用「采纳所选」"); return; }
        adopt(dec.draft);
      };
      const btnSel = mkBtn(`采纳所选（${String(chosen.length)} 条）`);
      btnSel.disabled = !chosen.length;
      btnSel.onclick = () => {
        const dec = decisionForOps(payload, picked, { lockText });
        if (!dec.ok) {
          lintEl.textContent = dec.reason === "invalid" ? `\u274c ${dec.error}` : "勾选的操作都被\ud83d\udd12锁定排除了：解除锁定或改选其他操作";
          return;
        }
        adopt(dec.draft);
      };
      const btnNone = mkBtn("全部拒绝");
      btnNone.onclick = () => { closeRevisionPanel(); fx.rememberRun?.("\ud83d\udeab 已拒绝 AI 改稿提案，画布未改动", ""); };
      btns.append(btnAll, btnSel, btnNone);
    }

    function adopt(newDraft) {
      // 采纳走模板载入同款路径：setWidgetInput + adoptEdits（带回滚）；先压一次
      // undo 快照（preRecorded 让 adoptEdits 不重复压栈），可 ↶撤销整个提案。
      const raw = JSON.stringify(newDraft, null, 2);
      const prevEdits = ctx.wEdits ? ctx.wEdits.value : "";
      const hadCanvas = !!M.S;
      try {
        if (hadCanvas) fx.pushUndo(fx.snapshot());
        fx.setWidgetInput(ctx.wEdits, raw);
        const ok = fx.adoptEdits(true, { keepUndo: hadCanvas, preRecorded: true });
        if (!ok) throw new Error("改稿结果无法解析为合法草稿");
        closeRevisionPanel();
        fx.rememberRun?.(`✅ AI 改稿已采纳：${String(newDraft.items.length)} 项 · 可 ↶撤销`, "ok");
      } catch (e) {
        if (hadCanvas && M.undo.length) M.undo.pop();
        fx.setWidgetInput(ctx.wEdits, prevEdits);
        lintEl.textContent = "❌ 采纳失败：" + (e && e.message);
      }
    }

    renderFoot();
  }

  return { openRevisionPanel, closeRevisionPanel };
}
