/**
 * MOSS Prompt Studio — Editor 画布前端扩展
 *
 * 移植/改编自 swan7-py/json-prompt-visualizer 的 canvas.py 交互画布（MIT 友好借鉴），
 * 适配 ComfyUI DOM widget：粘贴源(只读) → 交互编辑 → 编辑结果写回 edits widget。
 *
 * 功能：拖拽 / 四角缩放 / 撤销重做(100步) / 删除 / 方向键微调 / 图层面板 /
 *       检查器(caption/text/tags/palette/summary) / 重叠检测+遮挡措辞 /
 *       比例预设+16吸附 / 底图对比(回接生成图) / 实时校验 / 复制 draft JSON。
 */
import { app } from "/scripts/app.js";
import {
  detectKind, reverseCompiled,
  clampBox,
  normalizeIntWidget,
} from "./editor/model.js";
import { editorFlushRegistry, mountEditorLifecycle, mountCompilerSync } from "./editor/comfy-hooks.js";
import { buildDomShell, mountDomShellChrome } from "./editor/dom_shell.js";
import { mountCanvasRender } from "./editor/canvas_render.js";
import { mountItems } from "./editor/items.js";
import { mountInspector } from "./editor/inspector.js";
import { mountEditsSync } from "./editor/edits_sync.js";
import { mountTemplatePanel } from "./editor/template_panel.js";
import { mountPosePanel } from "./editor/pose_panel.js";
import { mountInjectFlows } from "./editor/inject_flows.js";
import { mountRunStatus } from "./editor/run_status.js";
import { initTranslator, sanitizeAgentMd } from "./editor/translator_panel.js";
import { normalizePoseMode, normalizeApplyPose, normalizePoseText,
  normalizeCostumeMode, normalizeApplyCostume, normalizeCostumeText } from "./editor/pose.js";
import { warnApiKeyIfSet, bindApiKeyWarn } from "./editor/llm_config.js";
import { mountLlmOptions } from "./editor/llm_options.js";
import { mountPoseInputs, mountCostumeInputs } from "./editor/pose_inputs.js";
import { injectStyle } from "./editor/styles.js";

console.log("[MOSS Studio] Editor 画布扩展已加载 (size-sync v5 / auto-wire)");

const NODE_CLASS = "MOSSEditor";

editorFlushRegistry.install(app);

function init(node) {
  if (node._mpsInit) return;
  node._mpsInit = true;
  injectStyle();

  const W = (name) => node.widgets.find((w) => w.name === name);
  let wSource = W("source"), wEdits = W("edits"), wCw = W("canvas_width"), wCh = W("canvas_height"), wAi = W("ai_draft");
  let wReplace = W("replace_edits"), wSig = W("last_applied_sig"), wSrcSig = W("edits_source_sig");
  if (!wSource || !wEdits || !wCw || !wCh) { console.warn("[MOSS Studio] Editor 节点 widget 不完整，画布未启用"); return; }
  // 新版前端在 configure 时可能替换 widget 对象；每次生命周期/轮询都按名字重新绑定，
  // 避免闭包里拿着旧 widget 读写，导致“界面改了但 edits 没变”。
  const rebindWidgets = () => {
    const s = W("source"), e = W("edits"), cw = W("canvas_width"), ch = W("canvas_height"), ai = W("ai_draft");
    const r = W("replace_edits"), sig = W("last_applied_sig"), ss = W("edits_source_sig");
    if (s) wSource = s;
    if (e) wEdits = e;
    if (cw) wCw = cw;
    if (ch) wCh = ch;
    if (ai) wAi = ai;
    if (r) wReplace = r;
    if (sig) wSig = sig;
    if (ss) wSrcSig = ss;
    normalizeReplaceEdits();
    normalizePoseWidgets();
    exposeReferenceSockets();
    bindEditsWidget();
    return !!(wSource && wEdits && wCw && wCh);
  };
  // issue #1：参考输入的原生 widget 被隐藏（换成节点内双列面板）后，ComfyUI 不会把
  // 对应 input 槽渲染成左侧 socket，用户无法把 Costume/Pose Gallery.description 连进来。
  // 解除槽与 hidden widget 的绑定即可恢复渲染；widgets 数组不动——无连线时前端
  // graphToPrompt 仍按 widget 值提交，有连线时连线值优先，取值语义不变。
  // 序列化/configure 会重建绑定，故挂在每次 rebindWidgets（幂等）里。
  function exposeReferenceSockets() {
    for (const nm of ["pose_in", "pose_in_2", "costume_in", "costume_in_2"]) {
      const inp = (node.inputs || []).find((i) => i && i.name === nm);
      if (!inp || !inp.widget) continue;
      inp.widget = null;
      if ("_widgetRef" in inp) inp._widgetRef = null;
    }
  }
  // 旧工作流缓存的姿势/服装 widget 可能是 ""/null（widgets_values 错位或新控件缺省）：
  // 服务端会直接拒绝 pose_mode=""、apply_pose="" 等，这里在 configure/轮询时静默修回默认值。
  function normalizePoseWidgets() {
    const fix = (w, v) => {
      if (!w || w.value === v) return;
      w.value = v;
      if (w.inputEl) w.inputEl.value = v;
    };
    const wMode = W("pose_mode"), wApply = W("apply_pose");
    const wPose = W("pose_in"), wPose2 = W("pose_in_2");
    if (wMode) fix(wMode, normalizePoseMode(wMode.value));
    if (wApply) fix(wApply, normalizeApplyPose(wApply.value));
    if (wPose) fix(wPose, normalizePoseText(wPose.value));
    if (wPose2) fix(wPose2, normalizePoseText(wPose2.value));
    const wCMode = W("costume_mode"), wCApply = W("apply_costume");
    const wCost = W("costume_in"), wCost2 = W("costume_in_2");
    if (wCMode) fix(wCMode, normalizeCostumeMode(wCMode.value));
    if (wCApply) fix(wCApply, normalizeApplyCostume(wCApply.value));
    if (wCost) fix(wCost, normalizeCostumeText(wCost.value));
    if (wCost2) fix(wCost2, normalizeCostumeText(wCost2.value));
    // 画布宽高同理：≤v1.7.2 工作流错位可能把 INT 读成字符串，服务端会拒整次提交
    for (const nm of ["canvas_width", "canvas_height"]) {
      const w = W(nm);
      if (w) fix(w, normalizeIntWidget(w.value));
    }
  }
  // 旧工作流缓存的新增 widget 可能是 ""，不是布尔；按“未设置”处理为默认开
  function normalizeReplaceEdits() {
    if (!wReplace) return;
    if (wReplace.value !== true && wReplace.value !== false) {
      wReplace.value = true;
      if (wReplace.inputEl && "checked" in wReplace.inputEl) wReplace.inputEl.checked = true;
    }
  }
  if (typeof node.addDOMWidget !== "function") { console.warn("[MOSS Studio] 当前前端不支持 addDOMWidget，画布未启用"); return; }
  sanitizeAgentMd(node);
  // LLM/AI 配置改成独立双列 DOM 面板；原 widget 隐藏但仍参与序列化。
  try { mountLlmOptions(node); } catch (e) { console.warn("[MOSS Studio] LLM 选项面板挂载失败", e); }
  // 两个姿势参考框同样改为双列 DOM 面板，带原生 resize 手柄；服装参考面板紧随其后。
  try { mountPoseInputs(node); } catch (e) { console.warn("[MOSS Studio] 姿势参考面板挂载失败", e); }
  try { mountCostumeInputs(node); } catch (e) { console.warn("[MOSS Studio] 服装参考面板挂载失败", e); }

  // 姿势 + 服装两组参考面板各约 96px：最小高度比 v1.7.2 增加 100px
  if (node.size[0] < 860 || node.size[1] < 1000) node.size = [Math.max(node.size[0], 860), Math.max(node.size[1], 1000)];

  // 文本框：不锁高度，允许用户拖右下角自由调整；手动调整后的高度优先于前端自动高度
  const userH = {};
  const makeResizable = (widget, key, minH) => {
    const el = widget.inputEl;
    if (!el) return;
    el.style.minHeight = minH + "px";
    // ComfyUI 前端对 multiline 输入有 resize:none 规则，用 important 反制；不支持的浏览器自动降级为随内容增高
    el.style.setProperty("resize", "vertical", "important");
    el.style.setProperty("overflow-y", "auto", "important");
    el.addEventListener("pointerup", () => {
      const h = Math.round(el.getBoundingClientRect().height);
      if (h >= minH) userH[key] = h;
    });
    // 在 ComfyUI 自动高度之后再把用户高度压回去
    el.addEventListener("input", () => {
      if (userH[key]) el.style.height = userH[key] + "px";
    });
  };
  makeResizable(wSource, "source", 90);
  makeResizable(wEdits, "edits", 44);
  if (wEdits.inputEl) wEdits.inputEl.style.opacity = "0.7";
  try { wEdits.label = "edits (自动·可手改)"; } catch (e) { /* 前端差异，忽略 */ }

  // ---------- DOM（骨架已拆到 ./editor/dom_shell.js，行为不变） ----------
  // fx：跨块函数注册表（按钮回调/分隔条手势 → 仍留在本闭包的编辑操作/渲染/提交流程）。
  // ctx：node / M / dom / widget getter 注入；widget 由 rebindWidgets 动态换绑，getter 保持实时引用。
  const fx = {};
  const M = (node._mps = {
    S: null, sel: null, undo: [], redo: [], inspPre: null, canvasH: 660,
    wSource, wEdits, wCw, wCh,
    els: null, // DOM 引用由 buildDomShell 创建后回填（同一批元素）
    builtSig: null, pendSig: null, timer: null, wt: null,
    editsDirty: false, editsCb: null, writingEdits: false,
    // source 归属（P0-1）：stateSrcSig=当前画布对应的 source 签名；split/pose/costume/queueSrcSig=
    // 已提交但尚未回来的异步结果对应的 source 签名；lastDraft*=双通道去重。
    stateSrcSig: "", splitSrcSig: "", poseSrcSig: "", costumeSrcSig: "", queueSrcSig: "",
    lastDraftText: "", lastDraftAt: 0, lastDraftPid: "",
    // F3/F4：提交互斥 / 最近一次拆解-注入的 prompt_id / 资源句柄。
    // submitSeq/submitPrevId：本地注入结果可能先于 /prompt 响应到达（见 submit_gate）。
    submitting: false, lastSubmitId: "", submitPrevId: "", submitSeq: 0, poseLinkWarned: false,
    queueTimer: null, ro: null,
  });
  const ctx = {
    node, M, fx, rebindWidgets,
    get wSource() { return wSource; },
    get wEdits() { return wEdits; },
    get wCw() { return wCw; },
    get wCh() { return wCh; },
    get wAi() { return wAi; },
    get wReplace() { return wReplace; },
    get wSig() { return wSig; },
    get wSrcSig() { return wSrcSig; },
  };
  const dom = buildDomShell(ctx);
  ctx.dom = dom;
  M.els = { canvasWrap: dom.canvasWrap, canvas: dom.canvas, underlay: dom.underlay, undChk: dom.undChk, capBox: dom.capBox, msg: dom.msg, layers: dom.layers, insp: dom.insp, overlap: dom.overlap, status: dom.status, runStatus: dom.runStatus, snapChk: dom.snapChk, btnUndo: dom.btnUndo, btnRedo: dom.btnRedo, btnDel: dom.btnDel, pnlLayers: dom.pnlLayers, pnlInsp: dom.pnlInsp, pnlOv: dom.pnlOv };
  // 手势/键盘/滚轮仍留在本闭包，直接引用画布容器（其余容器经 ctx.dom 供各模块使用）
  const { canvas, canvasWrap } = dom;

  // 明文 key 一次性警告（工作流/执行历史会保存 widget 的 api_key；同 key 只提示一次）
  const showKeyWarn = (t) => {
    try {
      app.extensionManager?.toast?.add({ severity: "warn", summary: "MOSS Studio", detail: t, life: 6000 });
    } catch (e) { /* toast 可选 */ }
    try {
      M.els.status.textContent = "⚠ " + t;
      M.els.status.className = "mps-status";
    } catch (e) { /* noop */ }
  };
  warnApiKeyIfSet(node, showKeyWarn);
  bindApiKeyWarn(node, showKeyWarn);
  // 跨块互调：700ms 轮询（run_status）里复用同一 key 警告回调
  Object.assign(fx, { showKeyWarn });

  // 宽度钉子 + 响应式降级 + ResizeObserver + addDOMWidget 挂载（./editor/dom_shell.js）
  const { applyWidthPin } = mountDomShellChrome(ctx);
  // 跨块互调：700ms 轮询（run_status）里复用宽度钉子
  Object.assign(fx, { applyWidthPin });

  // ---------- 渲染（layoutCanvas/renderBoxes/updateBoxStyle/renderLayers/bindField/palette 编辑器） ----------
  const { layoutCanvas, renderBoxes, updateBoxStyle, renderLayers, bindField, buildPaletteEditor, setOptArr } = mountCanvasRender(ctx);
  Object.assign(fx, { layoutCanvas, renderBoxes, renderLayers, bindField, buildPaletteEditor, setOptArr });
  const { pushUndo, commit, afterChange, afterLightChange, doUndo, doRedo, select, addItem, delItem, layerMove, occlude, applyCustomSize, applyResize } = mountItems(ctx);
  Object.assign(fx, { pushUndo, afterChange, afterLightChange, doUndo, doRedo, select, addItem, delItem, layerMove, occlude, applyCustomSize, applyResize });
  const { renderAll, updateStatus, refreshSizeSelectors, updateToolbar, renderInspector } = mountInspector(ctx);
  Object.assign(fx, { renderAll, updateStatus, refreshSizeSelectors, updateToolbar, renderInspector });

  // ---------- 状态同步与 edits 写回协议 + 重建（已拆到 ./editor/edits_sync.js，行为不变） ----------
  const { snapshot, currentSig, currentSourceSig, syncWidgets, writeEdits, writeEditsSoon,
    showMsg, hideMsg, adoptEdits, rebuildFromWidgets, rebuildFromEdits, setWidgetInput,
    bindEditsWidget, unregisterFlush } = mountEditsSync(ctx);
  // 跨块互调（供模板/姿势/注入/运行状态等模块经 fx 调用；注册先于任何事件/定时器触发）
  Object.assign(fx, { currentSig, currentSourceSig, adoptEdits, rebuildFromEdits, setWidgetInput });

  // ---------- 手势 ----------
  function applyGesture(i, handle, orig, dx, dy) {
    const d = M.S.d, it = d.items[i];
    const snap = M.els.snapChk.checked;
    const sn = (v) => (snap ? Math.round(v / 16) * 16 : Math.round(v));
    const MIN = 8;
    let [x0, y0, x1, y1] = orig;
    if (!handle) {
      const w = x1 - x0, h = y1 - y0;
      const nx0 = Math.min(Math.max(sn(x0 + dx), 0), d.width - w);
      const ny0 = Math.min(Math.max(sn(y0 + dy), 0), d.height - h);
      it.bbox = [nx0, ny0, nx0 + w, ny0 + h];
    } else {
      if (handle.includes("w")) x0 = Math.min(Math.max(sn(x0 + dx), 0), x1 - MIN);
      if (handle.includes("e")) x1 = Math.max(Math.min(sn(x1 + dx), d.width), x0 + MIN);
      if (handle.includes("n")) y0 = Math.min(Math.max(sn(y0 + dy), 0), y1 - MIN);
      if (handle.includes("s")) y1 = Math.max(Math.min(sn(y1 + dy), d.height), y0 + MIN);
      it.bbox = clampBox([x0, y0, x1, y1], d.width, d.height);
    }
  }

  function startGesture(e, i, handle) {
    if (!M.S || M.S.kind !== "layout") return;
    e.stopPropagation();
    e.preventDefault();
    select(i, true); // light：不重建 box 元素，保住 pointer capture
    const el = e.currentTarget;
    try { el.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    const rect = canvas.getBoundingClientRect();
    const scale = M.S.d.width / Math.max(1, rect.width);
    const pre = snapshot();
    const orig = [...M.S.d.items[i].bbox];
    const sx = e.clientX, sy = e.clientY;
    let moved = false;
    const onMove = (ev) => {
      if (Math.abs(ev.clientX - sx) + Math.abs(ev.clientY - sy) > 2) moved = true;
      applyGesture(i, handle, orig, (ev.clientX - sx) * scale, (ev.clientY - sy) * scale);
      updateBoxStyle(i);
    };
    const onUp = () => {
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerup", onUp);
      el.removeEventListener("pointercancel", onUp);
      if (moved) commit(pre);
    };
    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerup", onUp);
    el.addEventListener("pointercancel", onUp);
  }

  // 空白处点击取消选中并聚焦（拿键盘事件）
  canvas.addEventListener("pointerdown", (e) => {
    if (e.target === canvas) {
      e.stopPropagation();
      select(null, true);
      canvasWrap.focus();
    }
  });

  canvasWrap.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target && e.target.tagName) || "")) return;
    if (!M.S) return;
    const ctrl = e.ctrlKey || e.metaKey;
    if (ctrl && e.key.toLowerCase() === "z") { e.preventDefault(); e.stopPropagation(); doUndo(); return; }
    if (ctrl && e.key.toLowerCase() === "y") { e.preventDefault(); e.stopPropagation(); doRedo(); return; }
    if (M.S.kind === "layout" && M.sel != null) {
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault(); e.stopPropagation(); delItem(M.sel); return;
      }
      const step = e.shiftKey ? 10 : 1;
      let dx = 0, dy = 0;
      if (e.key === "ArrowLeft") dx = -step;
      else if (e.key === "ArrowRight") dx = step;
      else if (e.key === "ArrowUp") dy = -step;
      else if (e.key === "ArrowDown") dy = step;
      if (dx || dy) {
        e.preventDefault(); e.stopPropagation();
        const d = M.S.d, it = d.items[M.sel];
        const pre = snapshot();
        const w = it.bbox[2] - it.bbox[0], h = it.bbox[3] - it.bbox[1];
        const nx0 = Math.min(Math.max(it.bbox[0] + dx, 0), d.width - w);
        const ny0 = Math.min(Math.max(it.bbox[1] + dy, 0), d.height - h);
        it.bbox = [nx0, ny0, nx0 + w, ny0 + h];
        commit(pre);
        return;
      }
    }
  });

  // 选中框后滚轮缩放：以框中心为锚点等比缩放（每格 ≈±6%，Ctrl/Shift 更精细）。
  // 会话内用连续浮点几何累计（吸附会话开始时抓一次），只在落地时做 16 吸附——
  // 否则中心落在半格点上时，逐档缩放会被四舍五入弹回原值，宽度卡死不动。
  // 停止滚动 500ms 后提交撤销快照 + 重渲染（重叠检测等）。
  canvasWrap.title = "点击框选中 · 拖拽移动 · 四角拉伸 · 选中后滚轮缩放";
  let wheelUndoPre = null, wheelIdle = null, wheelGeom = null, wheelIdx = null;
  canvasWrap.addEventListener("wheel", (e) => {
    if (!M.S || M.S.kind !== "layout") return; // caption 模式下滚轮行为交还页面
    // 目标框：悬停在某个框上 → 该框；空白处 → 当前选中框
    const boxEl = e.target && e.target.closest ? e.target.closest(".mps-box") : null;
    const i = boxEl ? +boxEl.dataset.i : M.sel;
    if (i == null || !M.S.d.items[i]) return;
    e.preventDefault();
    e.stopPropagation(); // 别让 ComfyUI 画布跟着缩放
    // 换到另一个框时先结束上一段缩放，否则会把上一个框的中心/尺寸套到新框上
    if (wheelIdx !== null && wheelIdx !== i) {
      if (wheelUndoPre != null) { commit(wheelUndoPre); wheelUndoPre = null; }
      wheelGeom = null;
    }
    wheelIdx = i;
    if (M.sel !== i) select(i, true);
    const d = M.S.d, it = d.items[i];
    const snap = M.els.snapChk.checked;
    const sn = (v) => (snap ? Math.round(v / 16) * 16 : Math.round(v));
    const MIN = 8;
    const dyRaw = e.deltaMode === 1 ? (e.deltaY || 0) * 33 : (e.deltaY || 0);
    const fine = (e.ctrlKey || e.metaKey || e.shiftKey) ? 0.25 : 1;
    const factor = Math.exp(-dyRaw * 0.0006 * fine);
    if (wheelUndoPre == null) {
      wheelUndoPre = snapshot();
      const [x0, y0, x1, y1] = it.bbox;
      wheelGeom = { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, w: x1 - x0, h: y1 - y0 };
    }
    clearTimeout(wheelIdle);
    // 连续域等比缩放，并按"中心两侧可用空间"限幅，保证不越画布
    const g = wheelGeom;
    g.w *= factor; g.h *= factor;
    g.w = Math.min(Math.max(g.w, MIN), 2 * Math.min(g.cx, d.width - g.cx));
    g.h = Math.min(Math.max(g.h, MIN), 2 * Math.min(g.cy, d.height - g.cy));
    let nx0 = sn(g.cx - g.w / 2), nx1 = sn(g.cx + g.w / 2);
    let ny0 = sn(g.cy - g.h / 2), ny1 = sn(g.cy + g.h / 2);
    if (nx1 - nx0 < MIN) nx1 = nx0 + MIN;
    if (ny1 - ny0 < MIN) ny1 = ny0 + MIN;
    nx0 = Math.min(Math.max(nx0, 0), d.width - MIN);
    nx1 = Math.max(Math.min(nx1, d.width), nx0 + MIN);
    ny0 = Math.min(Math.max(ny0, 0), d.height - MIN);
    ny1 = Math.max(Math.min(ny1, d.height), ny0 + MIN);
    it.bbox = [nx0, ny0, nx1, ny1];
    updateBoxStyle(i);
    writeEditsSoon();
    wheelIdle = setTimeout(() => {
      wheelGeom = null;
      wheelIdx = null;
      if (wheelUndoPre != null) { commit(wheelUndoPre); wheelUndoPre = null; }
    }, 500);
  }, { passive: false });

  // ---------- 生命周期（由 comfy-hooks 统一链式挂载） ----------
  mountEditorLifecycle(node, {
    // 工作流恢复时 configure 先还原 widget 值再调 onConfigure：这里必须从 edits 重建，
    // 否则 700ms 轮询会用 source 覆盖掉保存的布局（AI 拆解/画布编辑丢失）。
    onConfigure: function () {
      if (this.size[0] < 860 || this.size[1] < 1000) {
        this.size = [Math.max(this.size[0], 860), Math.max(this.size[1], 1000)];
      }
      sanitizeAgentMd(this);
      rebindWidgets();
      try {
        node._mpsLlmOptions?.hideWidgets();
        node._mpsLlmOptions?.refresh();
        node._mpsPoseInputs?.hideWidgets();
        node._mpsPoseInputs?.refresh();
        node._mpsCostumeInputs?.hideWidgets();
        node._mpsCostumeInputs?.refresh();
      } catch (e) { /* noop */ }
      try { warnApiKeyIfSet(node, showKeyWarn); } catch (e) { /* noop */ }
      // 工作流切换/重新 configure 兜底：清掉可能卡住的提交状态（F2）
      resetSplitBtn();
      M.submitting = false;
      M.posePendingAt = 0;
      M.costumePendingAt = 0;
      M.splitPendingAt = 0;
      M.submitPrevId = "";
      M.queueSrcSig = "";
      rebuildFromWidgets({ preferEdits: true, write: false });
    },
    onResize: () => {
      try { applyWidthPin(); layoutCanvas(); } catch (e) { /* noop */ }
    },
    onRemoved: () => {
      clearInterval(M.timer);
      clearInterval(M.queueTimer);
      if (M.ro && typeof M.ro.disconnect === "function") M.ro.disconnect();
      clearTimeout(M.wt);
      clearTimeout(M.editsCb);
      clearTimeout(wheelIdle);
      unregisterFlush();
      if (M.apiHooked && M.apiRef && typeof M.apiRef.removeEventListener === "function") {
        M.apiRef.removeEventListener("executed", onApiExecuted);
        M.apiRef.removeEventListener("execution_error", onApiError);
        M.apiRef.removeEventListener("execution_interrupted", onApiInterrupted);
        M.apiRef.removeEventListener("status", onApiStatus);
      }
    },
  });

  // ---------- 版式模板面板 + 拆解/注入按钮复位（已拆到 ./editor/template_panel.js，行为不变） ----------
  const { resetSplitBtn, openTemplatePanel, closeTemplatePanel } = mountTemplatePanel(ctx);
  // 跨块互调（openPosePicker 关闭模板面板；processExecData/注入/轮询复位提交状态）
  Object.assign(fx, { resetSplitBtn, closeTemplatePanel });

  // ---------- 姿势库点选面板（已拆到 ./editor/pose_panel.js，行为不变） ----------
  const { openPosePicker, closePosePicker } = mountPosePanel(ctx);
  Object.assign(fx, { closePosePicker });

  // ---------- 运行状态：结果处理 + 事件 + 轮询 + 700ms 重建定时器（已拆到 ./editor/run_status.js，行为不变） ----------
  // onApi* 仍以 init 作用域常量解构：onRemoved 生命周期回调据此解绑事件监听
  const { rememberRun, onApiError, onApiStatus, onApiInterrupted, onApiExecuted } = mountRunStatus(ctx);
  // 跨块互调（edits_sync 的 _mpsApplyReverse 反推写回提示）
  Object.assign(fx, { rememberRun });

  // ---------- 提交流程（已拆到 ./editor/inject_flows.js，行为不变） ----------
  const { applyPose, applyCostume, runAiSplit } = mountInjectFlows(ctx);
  // LLM 配置同步反馈走状态条
  const cfgNotify = (t) => { M.els.status.textContent = t; M.els.status.className = "mps-status ok"; };

  // 仍留在本闭包的函数注册进 fx（供已拆出的 DOM/渲染/图元/检查器模块回调）。
  // 放在所有定义之后、首次同步重建之前；模块侧对这些名字只有事件/异步调用。
  Object.assign(fx, {
    snapshot, syncWidgets, writeEdits, writeEditsSoon, showMsg, hideMsg,
    startGesture, cfgNotify, runAiSplit, applyPose, applyCostume,
    openPosePicker, openTemplatePanel, rebuildFromWidgets,
  });

  rebuildFromWidgets({ preferEdits: true, write: false });
}

app.registerExtension({
  name: "MOSSPromptStudio.EditorCanvas",
  async nodeCreated(node) {
    const cls = (node.constructor && node.constructor.comfyClass) || node.comfyClass;
    if (cls === "MOSSEditor") init(node);
    else if (cls === "MOSSTranslator") initTranslator(node);
    else if (cls === "MOSSCompiler") mountCompilerSync(node);
  },
});
