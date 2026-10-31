/**
 * Editor 画布的运行状态与执行结果处理（自 editor_canvas.js 机械抽出，行为不变）。
 *
 * processExecData（ui.mps_draft 双通道去重/结果归属/写回 + 底图上屏）、
 * rememberRun/reportDraftArrived（语义状态行）、3s /queue 轮询（排队/执行中兜底显示）、
 * onApiError/onApiStatus/onApiInterrupted/onApiExecuted 与 node.onExecuted 旧路径、
 * tryHookApi（新前端事件总线补挂）以及 700ms M.timer（source 变化静默重建 +
 * 拆解/注入 180s 卡死兜底）。v1.8 竞态逻辑（lastSubmitId/submitPrevId/结果归属
 * 判定/700ms 定时器/3s 轮询）自原文件逐字保持。
 *
 * ctx = { node, M, fx, dom, rebindWidgets, get wEdits(){...} }：M 为 node._mps 状态袋
 * （同一对象）；fx 为跨块函数注册表（currentSig/currentSourceSig/rebuildFromWidgets/
 * rebuildFromEdits/writeEdits 相关来自 edits_sync，resetSplitBtn 来自 template_panel，
 * pushUndo/snapshot 来自 items，showKeyWarn/applyWidthPin 来自 init 闭包）。
 */
import { app } from "/scripts/app.js";
import { editorFlushRegistry } from "./comfy-hooks.js";
import { POSE_MARKER, COSTUME_MARKER } from "./pose.js";
import { queueRemaining, shouldResetOnQueueIdle, shouldResetOnTimeout, queueHasNode,
  isDuplicateDraft, resultAttributionDecision } from "./submit_gate.js";
import { commitDraftWriteback } from "./result_writeback.js";
import { warnApiKeyIfSet } from "./llm_config.js";

export function mountRunStatus(ctx) {
  const { node, M, fx } = ctx;
  const { underlay } = ctx.dom;

  // 执行结果处理：底图上屏 + AI 拆解写回。
  // 兼容两种消息形态（ui 字典本体 / 包一层 .ui）与两条投递路径（旧前端 onExecuted /
  // 新前端 api 事件总线）。meta.promptId：新前端 executed 事件带的提交 id（旧前端没有）。
  let lastImgName = null;
  function processExecData(data, meta = {}) {
    if (!data) return;
    if (data.mps_draft !== undefined) {
      // ComfyUI 4.9 的 ws 会把长字符串拆成单字符数组传输，归一成字符串
      const md = Array.isArray(data.mps_draft) ? data.mps_draft.join("") : data.mps_draft;
      if (typeof md === "string" && md.trim()) {
        // 双通道去重：旧前端 onExecuted 与新前端 api executed 可能各投递一次。
        // 迟到结果若被第一条路径丢弃，不能在第二条路径又以“无提交记录”被采纳。
        const now = Date.now();
        const curPid = meta.promptId ? String(meta.promptId) : "";
        // 同一结果的双通道投递才去重：不同 prompt_id 的同文结果（旧提交 vs
        // 本次提交）不得被去重误杀，否则真正属于本次提交的结果会被丢弃。
        const dupDraft = isDuplicateDraft({
          lastText: M.lastDraftText, lastAt: M.lastDraftAt, lastPid: M.lastDraftPid,
          text: md, now, promptId: curPid,
        });
        M.lastDraftText = md;
        M.lastDraftAt = now;
        M.lastDraftPid = curPid;
        // 结果归属（F3/P1）：后端回传 mps_src_sig 时严格比较；新前端事件带
        // prompt_id 时还必须匹配最近一次手工提交（完整 Queue/旧提交的结果丢弃）。
        const curSig = fx.currentSourceSig();
        const pendingSig = M.poseBtn ? M.poseSrcSig
          : (M.costumeBtn ? M.costumeSrcSig
            : (M.splitBtn ? M.splitSrcSig : M.queueSrcSig));
        const uiSrcSig = Array.isArray(data.mps_src_sig) ? data.mps_src_sig.join("") : data.mps_src_sig;
        const decision = resultAttributionDecision({
          promptId: meta.promptId,
          lastSubmitId: M.lastSubmitId,
          previousSubmitId: M.submitPrevId,
          uiSig: uiSrcSig,
          currentSig: curSig,
          pendingSig,
          duplicate: dupDraft,
        });
        // 本次结果是否为 📥姿势/📥服装 注入：必须在 resetSplitBtn 清掉按钮前判断（F5）
        const isPoseResult = !!M.poseBtn
          || (!!M.posePendingAt && Date.now() - M.posePendingAt < 30000 && !M.splitPendingAt);
        const isCostumeResult = !!M.costumeBtn
          || (!!M.costumePendingAt && Date.now() - M.costumePendingAt < 30000
            && !M.splitPendingAt && !isPoseResult);
        const isInjectResult = isPoseResult || isCostumeResult;
        // P1：结果到达（含重复/过期）就结束本次提交等待；否则 M.submitting 会一直
        // 否决 Queue/Ctrl+Enter 直到 180s 兜底。去重只决定是否再次写回画布。
        if (decision.resetPending) fx.resetSplitBtn();
        if (decision.outcome === "duplicate") {
          // 双通道第二次投递：提交状态已复位，不重复写回
        } else if (decision.outcome === "foreign") {
          // prompt_id 不属于最近一次提交：丢弃内容，但保持等待本次提交的结果
          if (decision.notify) {
            rememberRun("⚠ 收到不属于最近一次提交的结果，已丢弃；仍在等待本次提交", "err");
          }
        } else if (decision.outcome === "stale") {
          M.posePendingAt = 0; M.costumePendingAt = 0;
          M.splitPendingAt = 0; M.posePairHint = false;
          // 当前 source 的有效画布不能被丢掉：preferEdits=true 先尝试 owner 匹配的 edits；
          // 只有全部候选解析失败时才按原逻辑清理旧 edits（F8）
          try { fx.rebuildFromWidgets({ preferEdits: true }); } catch (e) { /* noop */ }
          rememberRun("⚠ 拆解/注入结果对应旧 source，已丢弃旧画布；请对新 source 重新拆解", "err");
        } else {
          // P2：先解析/结构校验到临时对象，成功后才写 edits 并重建；失败保留
          // 旧画布与旧 edits（不再把坏 JSON 留在 widget 里）。
          const written = commitDraftWriteback({
            raw: md,
            getEdits: () => ctx.wEdits.value,
            setEdits: (v) => {
              ctx.wEdits.value = v;
              if (ctx.wEdits.inputEl) ctx.wEdits.inputEl.value = v;
            },
            rebuild: () => {
              // 📥姿势/📥服装 结果支持 ↶撤销：先记录注入前画布；拆解/换源仍清空 undo（F5）
              if (isInjectResult && M.S) {
                try { fx.pushUndo(fx.snapshot()); } catch (e) { /* noop */ }
              }
              return fx.rebuildFromEdits(curSig, { keepUndo: isInjectResult });
            },
          });
          if (!written.ok) {
            M.els.status.textContent = "拆解/注入结果不是合法草稿，已保留原画布：" + written.error;
            M.els.status.className = "mps-status err";
            rememberRun("❌ 结果解析失败，已保留原画布；请重试或检查 LLM 输出", "err");
          } else {
            try { reportDraftArrived(written.draft); } catch (e) { /* 状态提示尽力而为 */ }
          }
        }
      } else if (M.submitting || M.splitBtn || M.poseBtn || M.costumeBtn) {
        // 空草稿同样是“结果到达”：复位提交状态，避免永久否决 Queue（P1）
        fx.resetSplitBtn();
        rememberRun("⚠ 收到空草稿结果，已复位提交状态；请重试", "err");
      }
    }
    const imgs = data.images;
    if (imgs && imgs.length) {
      const im = imgs[imgs.length - 1];
      if (im.filename === lastImgName) return; // 双通道去重
      lastImgName = im.filename;
      const viewPath = `/view?filename=${encodeURIComponent(im.filename)}&subfolder=${encodeURIComponent(im.subfolder || "")}&type=${encodeURIComponent(im.type || "temp")}&r=${Date.now()}`;
      // 子路径部署（反向代理 /comfy/ 等）下裸 /view 会 404；优先走 api.apiURL
      const apiRef = (typeof app !== "undefined" && app.api) || (typeof window !== "undefined" && window.app && window.app.api);
      underlay.src = (apiRef && typeof apiRef.apiURL === "function") ? apiRef.apiURL(viewPath) : viewPath;
      M.els.undChk.checked = true;
      underlay.style.display = "";
    }
  }

  // ---------- 运行状态（v1.7.1）：事件 + 轮询 ----------
  // 语义事件（拆解完成/姿势注入完成/散文已收到）即时更新；3s 轮询 /queue 兜底显示
  // 本节点排队/执行中（跨客户端触发、事件丢失等场景）。
  const setRun = (t, cls) => {
    if (!M.els.runStatus) return;
    M.els.runStatus.textContent = t;
    M.els.runStatus.className = "mps-status mps-runstatus " + (cls || "");
  };
  M.lastRunMsg = "";      // 最近一次语义事件（轮询空闲时保留显示）
  M.posePendingAt = 0;    // 📥姿势提交时间戳：区分 mps_draft 是拆解还是注入产物
  M.costumePendingAt = 0; // 📥服装提交时间戳
  M.splitPendingAt = 0;
  function rememberRun(t, cls) { M.lastRunMsg = t; setRun(t, cls); }

  function reportDraftArrived(draft) {
    const chars = (draft.items || []).filter((i) => i && i.label === "character");
    const poseMarked = chars.filter((c) => (c.caption || "").includes(POSE_MARKER));
    const costumeMarked = chars.filter((c) => (c.caption || "").includes(COSTUME_MARKER));
    const isPoseInject = !!(M.posePendingAt && Date.now() - M.posePendingAt < 30000 && !M.splitPendingAt);
    const isCostumeInject = !isPoseInject
      && !!(M.costumePendingAt && Date.now() - M.costumePendingAt < 30000 && !M.splitPendingAt);
    if (isPoseInject) {
      const pair = poseMarked.length && chars.length >= 2
        && (M.posePairHint || /双人/.test(node.widgets.find((x) => x.name === "pose_in")?.value || ""));
      rememberRun(`✅ 姿势注入完成：${poseMarked.length}/${chars.length} 角色已注入${pair ? "（双人姿势整体）" : ""} · 共 ${draft.items.length} 项 · 校验通过`, "ok");
    } else if (isCostumeInject) {
      rememberRun(`✅ 服装注入完成：${costumeMarked.length}/${chars.length} 角色已注入 · 共 ${draft.items.length} 项 · 校验通过`, "ok");
    } else {
      const bits = [];
      if (poseMarked.length) bits.push(`姿势 ${poseMarked.length} 人`);
      if (costumeMarked.length) bits.push(`服装 ${costumeMarked.length} 人`);
      rememberRun(`✅ AI 拆解完成：${draft.items.length} 项（角色 ${chars.length}）${bits.length ? ` · 已注入 ${bits.join(" / ")}` : " · 未注入姿势/服装"} · 画布已刷新`, "ok");
    }
    M.posePendingAt = 0; M.costumePendingAt = 0;
    M.splitPendingAt = 0; M.posePairHint = false;
  }

  // /queue 轮询：显示排队/执行中；空闲时保留最近语义消息
  M.queueTimer = setInterval(async () => {
    // 节点已删除（onRemoved 之外的兜底）：停止轮询，避免闭包持有已移除节点
    if (!node.graph) { clearInterval(M.queueTimer); return; }
    try {
      const a = (typeof app !== "undefined" && app.api) || (window.app && window.app.api);
      if (!a || !a.fetchApi) return;
      const r = await a.fetchApi("/queue");
      const q = await r.json();
      const idStr = String(node.id);
      // /queue item = [number, prompt_id, prompt, extra, outputs]；prompt 在第 3 项（F4）
      const inRunning = queueHasNode(q.queue_running, idStr);
      if (inRunning) { setRun("⏳ 本节点执行中…（LLM 拆解/注入可能需要 10~120 秒）"); return; }
      const pending = Array.isArray(q.queue_pending) ? q.queue_pending : [];
      const idx = pending.findIndex((item) => queueHasNode([item], idStr));
      if (idx >= 0) { setRun(`⏳ 排队中：前面还有 ${idx} 个任务`); return; }
      if (M.lastRunMsg) setRun(M.lastRunMsg);
    } catch (e) { /* 轮询失败静默 */ }
  }, 3000);

  const onApiError = (ev) => {
    try {
      const d = ev && ev.detail;
      if (!d || String(d.node_id) !== String(node.id)) return;
      fx.resetSplitBtn();
      // 把 sig 标记为已构建：阻止轮询里挂起的 rebuildFromWidgets 立刻覆盖掉这条错误提示
      M.builtSig = M.pendSig = fx.currentSig();
      M.els.status.textContent = "拆解失败: " + (d.exception_message || "未知错误");
      M.els.status.className = "mps-status err";
      rememberRun("❌ 本节点执行失败：" + (d.exception_message || "未知错误"), "err");
      M.posePendingAt = 0; M.costumePendingAt = 0; M.splitPendingAt = 0;
    } catch (e) { /* noop */ }
  };

  // 队列跑完却没等到本节点的 executed（结果被缓存跳过 / 事件丢失）→ 解除按钮卡死
  const onApiStatus = (ev) => {
    try {
      if (!M.splitBtn && !M.poseBtn && !M.costumeBtn) return;
      const st = ev && ev.detail;
      const hasPending = !!(M.splitBtn || M.poseBtn || M.costumeBtn);
      const elapsedMs = Date.now() - (M.splitAt || 0);
      // 显式解析两种 status 负载；queue_remaining=0 必须命中（F2）
      if (shouldResetOnQueueIdle({ queueRemaining: queueRemaining(st), hasPending, elapsedMs })) {
        fx.resetSplitBtn();
        M.builtSig = M.pendSig = fx.currentSig();
        M.els.status.textContent = "队列已空但未收到结果（可能被缓存跳过）：请再点一次";
        M.els.status.className = "mps-status err";
      }
    } catch (e) { /* noop */ }
  };

  // 执行被用户/系统中断：复位按钮与 M.submitting，避免永久否决 Queue（F2）
  const onApiInterrupted = () => {
    try {
      if (!M.splitBtn && !M.poseBtn && !M.costumeBtn && !M.submitting) return;
      fx.resetSplitBtn();
      M.builtSig = M.pendSig = fx.currentSig();
      M.els.status.textContent = "执行被中断：已复位拆解/注入按钮，可重新提交";
      M.els.status.className = "mps-status err";
      M.posePendingAt = 0;
      M.costumePendingAt = 0;
      M.splitPendingAt = 0;
    } catch (e) { /* noop */ }
  };

  const prevExec = node.onExecuted;
  node.onExecuted = function (info) {
    // 新前端同时会发带 prompt_id 的 api executed 事件；legacy 路径没有 prompt_id，
    // 若让它先跑会绕过提交归属（同 source 的旧结果/完整 Queue 结果可能被误收）。
    // 因此只在没有 api 事件总线时回退到 legacy；prevExec 始终保留给其他扩展。
    if (!M.apiHooked) {
      try { processExecData(info && (info.ui || info)); } catch (e) { /* noop */ }
    }
    if (prevExec) return prevExec.call(this, info);
  };
  const onApiExecuted = (ev) => {
    try {
      const d = ev && ev.detail;
      if (!d) return;
      if (String(d.node) !== String(node.id) && String(d.display_node) !== String(node.id)) return;
      const promptId = d.prompt_id ? String(d.prompt_id) : "";
      // P1：新前端事件带 prompt_id 时，与最近一次手工提交匹配；完整 Queue/旧提交
      // 的结果不能写回本次画布（source 相同也不行），也不解除本次等待。
      const pidMismatch = !!promptId && !!M.lastSubmitId && promptId !== String(M.lastSubmitId);
      if (pidMismatch) {
        console.debug("[MPS] executed prompt_id mismatch", { prompt_id: promptId, lastSubmitId: M.lastSubmitId });
      }
      const out = d.output && (d.output.ui || d.output);
      // 本节点执行完却没带回新草稿（例如 edits 透传 / 输入没变被缓存）：
      // 解除"⏳拆解中/⏳注入中"，别让按钮挂着等一个不会来的结果。
      const ui = out || {};
      if ((M.splitBtn || M.poseBtn || M.costumeBtn) && ui.mps_draft === undefined && !(out && out.images && out.images.length)) {
        // 不是本次手工提交的执行（完整 Queue 透传等）：保持等待，别提前复位
        if (pidMismatch) return;
        fx.resetSplitBtn();
        M.els.status.textContent = "本次执行没有产生新草稿（源可能没变化被缓存）。改一下 source 再试，或点 ⟲重置";
        M.els.status.className = "mps-status err";
        return;
      }
      processExecData(out, { promptId });
    } catch (e) { /* noop */ }
  };
  // 新前端里 nodeCreated 发生得很早，app.api 可能尚未就位：先尝试挂载，轮询里补挂
  M.apiHooked = false;
  M.apiRef = null;
  const tryHookApi = () => {
    editorFlushRegistry.install(app);
    if (M.apiHooked) return true;
    const api = (typeof app !== "undefined" && app.api && typeof app.api.addEventListener === "function")
      ? app.api
      : (typeof window !== "undefined" && window.app && window.app.api && typeof window.app.api.addEventListener === "function" ? window.app.api : null);
    if (api) {
      api.addEventListener("executed", onApiExecuted);
      api.addEventListener("execution_error", onApiError);
      api.addEventListener("execution_interrupted", onApiInterrupted);
      api.addEventListener("status", onApiStatus);
      M.apiHooked = true;
      M.apiRef = api;
      return true;
    }
    return false;
  };
  tryHookApi();

  // source/宽高变化 → 静默 1.4s 后重建（避免边打字边闪）；顺带补挂执行事件总线。
  // 拆解进行中（M.splitBtn 挂起）不要重建：source 刚改过，此刻重建会把画布/布局
  // 瞬间重置回 caption 散文视图，等结果落地后由 rebuildFromEdits 接管。
  M.timer = setInterval(() => {
    tryHookApi();
    ctx.rebindWidgets();
    try { warnApiKeyIfSet(node, fx.showKeyWarn); } catch (e) { /* noop */ }
    try { fx.applyWidthPin(); } catch (e) { /* noop */ }
    const sig = fx.currentSig();
    if (sig === M.pendSig) {
      // source 改变时以 source 为准；edits 只作为 source 为空/加载恢复时的回退
      if (sig !== M.builtSig && !M.splitBtn) { fx.rebuildFromWidgets({ preferEdits: false }); }
    } else {
      M.pendSig = sig;
    }
    // 拆解/注入按钮卡死硬兜底：180s 仍无结果就解除（applyPose 共用 M.splitAt）
    const hasPending = !!(M.splitBtn || M.poseBtn || M.costumeBtn);
    if (shouldResetOnTimeout({ hasPending, elapsedMs: Date.now() - (M.splitAt || 0) })) {
      fx.resetSplitBtn();
      M.builtSig = M.pendSig = fx.currentSig();
      M.els.status.textContent = "拆解/注入超时（180s 无结果）：已复位，请检查 LLM 配置后重试";
      M.els.status.className = "mps-status err";
    }
  }, 700);

  return { processExecData, rememberRun, reportDraftArrived, setRun,
    onApiError, onApiStatus, onApiInterrupted, onApiExecuted, tryHookApi };
}
