/**
 * Editor 画布的状态同步与 edits 写回协议（自 editor_canvas.js 机械抽出，行为不变）。
 *
 * snapshot/currentSig/currentSourceSig（签名）、syncWidgets（宽高与 builtSig 维护）、
 * writeEdits/writeEditsSoon（写回 + 300ms 防抖）、bindEditsWidget/scheduleAdoptEdits/
 * adoptEdits（手改 edits 接管）、flushEdits（Queue 门禁：拆解/注入在飞时否决提交）、
 * setStateFromText/rebuildFromWidgets/rebuildFromEdits（重建路径）、消息条 showMsg/hideMsg，
 * 以及反推节点写回挂载 node._mpsApplyReverse。
 *
 * ctx = { node, M, fx, dom, rebindWidgets, get wSource(){...}, get wEdits(){...},
 *   get wCw(){...}, get wCh(){...}, get wAi(){...}, get wReplace(){...},
 *   get wSig(){...}, get wSrcSig(){...} }：M 为 node._mps 状态袋（同一对象）；
 * fx 为跨块函数注册表（pushUndo/renderAll/updateToolbar/updateStatus 来自 items/
 * inspector，rememberRun 来自 run_status，均为事件/异步时才调用）；widget 通过
 * getter 注入以保持 rebindWidgets 换绑后的实时引用。
 */
import { parseSourceText, snapshotOf, serializeDraft, sourceSignature } from "./model.js";
import { editorFlushRegistry } from "./comfy-hooks.js";
import { parseReverseRecord, reverseDecision, reverseRecord } from "./reverse.js";
import { editsOwnerSignature, rebuildCandidates } from "./source_guard.js";

export function mountEditsSync(ctx) {
  const { node, M, fx } = ctx;
  const { msg } = ctx.dom;

  // ---------- 状态 ----------
  const snapshot = () => snapshotOf(M.S);
  const currentSig = () => `${ctx.wSource.value}\u0000${ctx.wCw.value},${ctx.wCh.value}`;

  function syncWidgets() {
    if (M.S && M.S.kind === "layout") {
      ctx.wCw.value = M.S.d.width;
      ctx.wCh.value = M.S.d.height;
      if (ctx.wCw.inputEl) ctx.wCw.inputEl.value = ctx.wCw.value;
      if (ctx.wCh.inputEl) ctx.wCh.inputEl.value = ctx.wCh.value;
    }
    const nowSig = currentSig();
    // 画布来自旧 source 时不能把 builtSig 标成当前 source：否则 700ms 轮询会认为
    // “已构建”而不再从新 source 重建，旧画布就被永久当成新 source 的画布。
    // source 为空时 edits 是唯一内容来源，保持“已构建”，否则轮询会每 700ms 重复重建。
    const srcText = (ctx.wSource.value || "").trim();
    M.builtSig = (srcText && M.stateSrcSig && M.stateSrcSig !== currentSourceSig()) ? "" : nowSig;
    M.pendSig = nowSig;
  }

  // edits 归属签名：写入画布内容时同时记录当时 source 的签名。后端/加载恢复据此
  // 判断“这份 edits 是否仍对应当前 source”，换源后旧画布自动失效。
  function setSourceSig(value) {
    if (!ctx.wSrcSig) return;
    const v = value || "";
    ctx.wSrcSig.value = v;
    if (ctx.wSrcSig.inputEl) ctx.wSrcSig.inputEl.value = v;
  }
  const currentSourceSig = () => sourceSignature((ctx.wSource.value || "").trim());

  function writeEdits(opts = {}) {
    // M.S 为空只代表“当前无法解析”，不能顺手清空已保存的 edits；
    // 只有显式重置/清空时才允许传 clear:true。
    M.writingEdits = true;
    try {
      if (!M.S) {
        if (opts.clear) {
          ctx.wEdits.value = "";
          if (ctx.wEdits.inputEl) ctx.wEdits.inputEl.value = "";
          M.stateSrcSig = "";
          setSourceSig("");
        }
      } else {
        ctx.wEdits.value = serializeDraft(M.S);
        if (ctx.wEdits.inputEl) ctx.wEdits.inputEl.value = ctx.wEdits.value;
        // 只要画布内容来自当前 source（或手改后的 edits 被接管），都刷新归属签名。
        // 关键（P0-1）：M.S 可能是旧 source 的残留画布，此时必须沿用生成它的
        // source 签名（M.stateSrcSig），不能盖成 currentSourceSig()，否则后端会把
        // 旧画布当成新 source 的当前画布优先使用。
        setSourceSig(editsOwnerSignature(M.stateSrcSig, currentSourceSig()));
      }
      M.editsDirty = false;
      clearTimeout(M.editsCb);
    } finally {
      M.writingEdits = false;
    }
  }

  // ---------- 手改 edits（文档承诺） ----------
  // ComfyUI 的 edits widget 是用户可直接编辑的；用户输入后按 debounce 解析并接管画布，
  // 而不是等下一次画布 flush 把它覆盖回旧状态。
  function bindEditsWidget() {
    if (!ctx.wEdits || ctx.wEdits.__mpsEditsCb) return;
    const prev = ctx.wEdits.callback;
    ctx.wEdits.callback = function (...args) {
      const r = prev ? prev.apply(this, args) : undefined;
      if (!M.writingEdits) scheduleAdoptEdits();
      return r;
    };
    ctx.wEdits.__mpsEditsCb = true;
  }

  function scheduleAdoptEdits() {
    M.editsDirty = true;
    clearTimeout(M.editsCb);
    M.editsCb = setTimeout(() => {
      try { adoptEdits(true); } catch (e) { /* noop */ }
    }, 350);
  }

  function adoptEdits(showErrors = true, opts = {}) {
    clearTimeout(M.editsCb);
    const raw = (ctx.wEdits.value || "").trim();
    M.editsDirty = false;
    if (!raw) {
      if (showErrors) showMsg("edits 为空：请填写草稿 JSON 或使用画布编辑", true);
      return false;
    }
    let next;
    try {
      next = applySourceText(raw);
    } catch (e) {
      if (showErrors) showMsg("edits 不是合法草稿: " + (e.message || e), true);
      return false;
    }
    if (M.S && JSON.stringify(next) === JSON.stringify(M.S)) return true;
    const pre = M.S ? snapshot() : null;
    M.S = next;
    M.stateSrcSig = currentSourceSig();
    M.sel = null;
    // 手改 edits 默认重开撤销栈；模板载入等调用方已自行压入撤销点时保留历史
    if (!opts.keepUndo) M.undo = [];
    M.redo = [];
    if (pre && !opts.preRecorded) fx.pushUndo(pre);
    syncWidgets(); fx.renderAll(); fx.updateToolbar(); hideMsg();
    // 用户手改 edits 是明确意图：把这份内容归属到当前 source，后端继续以 edits 为准
    setSourceSig(M.stateSrcSig);
    return true;
  }
  const writeEditsSoon = () => {
    clearTimeout(M.wt);
    M.wt = setTimeout(writeEdits, 300);
  };
  const flushEdits = () => {
    // F3：拆解/注入 POST 在飞时拒绝 Queue（含 Ctrl+Enter），避免用拆解前画布提交
    if (M.submitting) {
      M.els.status.textContent = "已有拆解/注入任务在飞，请等待完成";
      M.els.status.className = "mps-status err";
      return false;
    }
    clearTimeout(M.wt);
    ctx.rebindWidgets();
    // 记录本次 Queue 提交对应的 source：全流程 Queue 也可能返回 mps_draft
    // （ai_draft 散文拆解），结果回来时据此判断 source 是否已变。
    M.queueSrcSig = currentSourceSig();
    try {
      // 用户刚手改过 edits：先接管；有效且 source 未变时保留用户原始文本，不重排覆盖。
      // 不只依赖 widget callback：Queue 前直接对比文本，callback 没触发也能兜住。
      const manualText = (ctx.wEdits.value || "").trim();
      const serialized = M.S ? serializeDraft(M.S).trim() : "";
      const hadManual = M.editsDirty || (manualText && manualText !== serialized);
      if (hadManual) {
        adoptEdits(false);
        if (currentSig() === M.builtSig) return;
      }
      // source 改了但还没被 700ms 轮询接管 → 提交前同步重建，避免 Queue 用旧 edits
      if (currentSig() !== M.builtSig) {
        rebuildFromWidgets({ preferEdits: false, write: false });
        if (M.S) writeEdits();
        return;
      }
      if (!M.S) {
        rebuildFromWidgets({ preferEdits: true, write: false });
        if (M.S) writeEdits();
        return;
      }
      writeEdits();
    } catch (e) { /* noop */ }
  };
  const unregisterFlush = editorFlushRegistry.register(flushEdits);

  function showMsg(text, isErr) {
    msg.className = "mps-msg" + (isErr ? " err" : "");
    msg.textContent = text;
    msg.style.display = "";
  }
  const hideMsg = () => { msg.style.display = "none"; };

  // 文本 → S 状态（纯解析在 model.js；这里只补当前画布尺寸）
  function applySourceText(src) {
    return parseSourceText(src, +ctx.wCw.value || 1024, +ctx.wCh.value || 1024);
  }

  function setStateFromText(src, opts = {}) {
    M.S = applySourceText(src);
    // 记录这份画布是由哪份 source 生成的；写入 edits 时沿用该签名做归属（P0-1）。
    if ("srcSig" in opts) M.stateSrcSig = String(opts.srcSig || "");
    M.sel = null;
    // 📥姿势 注入保留既有撤销栈（F5）；拆解/换源/反推仍清空
    if (!opts.keepUndo) M.undo = [];
    M.redo = [];
    hideMsg();
    syncWidgets(); fx.renderAll();
    if (opts.write !== false) {
      writeEdits();
    } else {
      M.editsDirty = false;
      clearTimeout(M.editsCb);
    }
    fx.updateToolbar();
  }

  function rebuildFromWidgets(opts = {}) {
    const { preferEdits = false, write = true, clearEditsOnEmpty = false } = opts;
    ctx.rebindWidgets();
    M.editsDirty = false;
    clearTimeout(M.editsCb);
    const src = (ctx.wSource.value || "").trim();
    let edits = (ctx.wEdits.value || "").trim();
    M.S = null; M.sel = null; M.undo = []; M.redo = [];
    hideMsg();

    // ⟲重置：显式清空 edits（旧实现 source 为空时会回退 edits，等于没重置）
    if (clearEditsOnEmpty && !src) {
      edits = "";
      writeEdits({ clear: true });
    }

    // edits 只对“写入时的那份 source”有效。签名缺失（旧工作流）先按有效处理，
    // 下面成功接管后会立即补写签名；签名不匹配则视为换源，以 source 为准。
    const srcSig = src ? sourceSignature(src) : "";
    const storedSig = ctx.wSrcSig ? String(ctx.wSrcSig.value || "").trim() : "";
    const { editsValid, candidates } = rebuildCandidates({ preferEdits, src, edits, srcSig, storedSig });
    if (preferEdits && !editsValid && edits) showMsg("source 已变化：本次以 source 为准，旧画布编辑已忽略", false);

    if (!candidates.length) {
      showMsg("粘贴源为空 —— 在 source 框里贴入上游产出的中文草稿 JSON", false);
      M.builtSig = M.pendSig = currentSig();
      fx.renderAll();
      if (clearEditsOnEmpty) writeEdits({ clear: true });
      fx.updateToolbar();
      return;
    }

    let lastErr = null;
    for (const c of candidates) {
      try {
        // 这份画布由哪份 source 生成：fromEdits 沿用保存的归属签名（旧工作流没有
        // 签名时按当前 source 处理），fromSource 则对应当前 source（P0-1）。
        const candSig = c.fromEdits ? (storedSig || currentSourceSig()) : currentSourceSig();
        // 从 edits 恢复时保留原始字符串；回退到 source 时必须写回，否则旧的非法 edits 会一直优先
        setStateFromText(c.text, { write: write !== false || !c.fromEdits, srcSig: candSig });
        // 旧工作流没有归属签名：成功接管后立即补写，之后换源即可正确失效
        if (c.fromEdits) setSourceSig(candSig);
        return;
      } catch (e) {
        lastErr = e;
      }
    }

    // 解析失败：保留原 edits，不写空，避免把已保存的布局冲掉。
    // 只有旧 edits 已明确不属于当前 source、且没有任何候选可接管时，才按原行为
    // 清掉，避免它继续遮住 source（F8：清空动作必须在候选解析失败之后）。
    if (!editsValid && edits && src && !preferEdits) {
      ctx.wEdits.value = "";
      if (ctx.wEdits.inputEl) ctx.wEdits.inputEl.value = "";
      setSourceSig("");
    }
    showMsg(String((lastErr && lastErr.message) || lastErr || "无法解析"), true);
    M.builtSig = M.pendSig = currentSig();
    fx.renderAll(); fx.updateToolbar();
  }

  // AI 拆解结果（ui.mps_draft）写回 edits 后从这里重建画布。
  // srcSig：生成这份结果时使用的 source 签名（默认当前 source；迟到结果由调用方传入）。
  // opts.keepUndo：📥姿势 注入时保留既有撤销栈（F5）。
  function rebuildFromEdits(srcSig, opts = {}) {
    try {
      setStateFromText(ctx.wEdits.value, {
        srcSig: srcSig !== undefined ? srcSig : currentSourceSig(),
        keepUndo: !!opts.keepUndo,
      });
      return true;
    } catch (e) {
      /* edits 内容异常时保持现状 */
      return false;
    }
  }

  // 反推节点写回：prose/draft → source；canvas → edits。签名存隐藏 widget，
  // 同一结果被缓存重放时不覆盖用户手改。
  const setWidgetInput = (w, v) => {
    if (!w) return;
    w.value = v;
    if (w.inputEl) w.inputEl.value = v;
  };
  const setAiDraft = (v) => {
    if (!ctx.wAi) return;
    ctx.wAi.value = !!v;
    if (ctx.wAi.inputEl) ctx.wAi.inputEl.checked = !!v;
  };
  node._mpsApplyReverse = function (p) {
    if (!p || typeof p.text !== "string" || !p.text.trim()) {
      return { ok: false, error: "empty" };
    }
    const rec = parseReverseRecord(ctx.wSig ? ctx.wSig.value : "");
    const decision = reverseDecision(rec, {
      ownerId: node.id,
      sig: p.sig,
      mode: p.mode,
      replaceEdits: ctx.wReplace ? !!ctx.wReplace.value : true,
      edits: ctx.wEdits ? ctx.wEdits.value : "",
      // prose/draft 实际写 source：same-sig 保护必须按写入目标判断（F7）
      source: ctx.wSource ? ctx.wSource.value : "",
    });
    if (decision !== "apply") return { ok: decision === "same", skipped: decision };

    if (p.width) setWidgetInput(ctx.wCw, p.width);
    if (p.height) setWidgetInput(ctx.wCh, p.height);
    // 反推结果的 ai_draft 联动（v1.9.0-beta0 按用户反馈调整）：
    // - prose（散文）：自动开启 AI 拆解——连 prose_zh 到 reverse_in 的意图就是
    //   “反推散文 → 拆解 → 翻译”全链；普通 Queue 有输入哈希缓存，不重复计费。
    // - draft/canvas（layout JSON）：保持关闭——已是 layout，无需拆解。
    // 旧行为三种形态都强制关，导致散文写回后普通 Queue 不拆解（易踩坑）。
    if (p.mode === "prose") {
      setAiDraft(true);
      setWidgetInput(ctx.wSource, p.text);
      rebuildFromWidgets({ preferEdits: false, write: true });
      if (M.els && M.els.status) {
        M.els.status.textContent = "📨 反推散文已写回 source，AI 拆解已自动开启：Queue 即完成 拆解→翻译 全链";
        M.els.status.className = "mps-status ok";
      }
    } else if (p.mode === "draft") {
      setAiDraft(false);
      setWidgetInput(ctx.wSource, p.text);
      rebuildFromWidgets({ preferEdits: false, write: true });
    } else if (p.mode === "canvas") {
      setAiDraft(false);
      setWidgetInput(ctx.wEdits, p.text);
      // 解析失败时保留原画布与归属签名，别把旧画布盖成当前 source（P0-1 同类风险）
      if (!rebuildFromEdits(currentSourceSig())) return { ok: false, error: "parse" };
      // canvas_zh 也是当前 source 对应的画布：记录归属，换源后自动失效
      setSourceSig(currentSourceSig());
    } else {
      return { ok: false, error: "mode" };
    }
    if (!M.S) return { ok: false, error: "parse" };
    // 记录只存 owner/sig/mode + draftHash（不重复保存整份画布文本，控制工作流体积）
    if (ctx.wSig) setWidgetInput(ctx.wSig, reverseRecord(node.id, p.sig, p.mode, ctx.wEdits ? ctx.wEdits.value : ""));
    fx.updateStatus();
    fx.rememberRun(`📨 反推结果已写回（${p.mode === "prose" ? "散文→source" : p.mode === "draft" ? "草稿→source·已关自动拆解" : "画布→edits"}），再 Queue 一次完整链路生效`, "ok");
    return { ok: true };
  };

  return {
    snapshot, currentSig, currentSourceSig, syncWidgets, writeEdits, writeEditsSoon,
    showMsg, hideMsg, adoptEdits, setStateFromText, rebuildFromWidgets, rebuildFromEdits,
    setWidgetInput, setAiDraft, bindEditsWidget, flushEdits, unregisterFlush,
  };
}
