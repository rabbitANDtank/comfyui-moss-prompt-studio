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
  VALID_LABELS, VALID_CANVAS, OCCL_FRONT, OCCL_BACK,
  detectKind, reverseCompiled, validateDraft as validateJS,
  parseSourceText, snapshotOf, serializeDraft,
  clampDim, clampArea, clampBox, csvParse, csvShow, sourceSignature,
} from "./editor/model.js";
import { editorFlushRegistry, mountEditorLifecycle, mountCompilerSync } from "./editor/comfy-hooks.js";
import { parseReverseRecord, reverseDecision, reverseRecord } from "./editor/reverse.js";
import { syncLlmConfig, warnApiKeyIfSet, bindApiKeyWarn } from "./editor/llm_config.js";

console.log("[MOSS Studio] Editor 画布扩展已加载 (size-sync v5 / auto-wire)");

const NODE_CLASS = "MOSSEditor";
const COLORS = { object: "#9d86f0", character: "#3ec27f", text: "#5aa9f5" };
// 官方口径：比例任意，只要求宽高为 16 的正整数倍；这里列常用比例，换算后自动对齐 16。
const RATIOS = [
  "1:1",
  "5:4", "4:3", "3:2", "16:10", "16:9", "2:1", "21:9", "3:1",
  "4:5", "3:4", "2:3", "10:16", "9:16", "1:2", "1:3",
];
// 官方口径：支持 0.25M（512×512）到 4M（2K），推荐 1M / 1.5M / 2M。
const MP_OPTS = [["0.25", "0.25M"], ["0.5", "0.5M"], ["1", "1M"], ["1.5", "1.5M"], ["2", "2M"], ["3", "3M"], ["4", "4M"]];
const AGENT_MD_DEFAULT = "(默认 AGENT.md)";

// agent_md 下拉的空值纠正：自动恢复的旧工作流可能存出 ''，后端虽然兼容，
// 但界面统一显示成默认项，避免下拉框出现空值引发困惑
function sanitizeAgentMd(node) {
  try {
    const w = node.widgets && node.widgets.find((x) => x.name === "agent_md");
    if (w && !String(w.value || "").trim()) w.value = AGENT_MD_DEFAULT;
  } catch (e) { /* noop */ }
}

const CSS = `
.mps-root{display:flex;flex-direction:column;gap:6px;font-family:system-ui,'Segoe UI',sans-serif;color:#ddd;font-size:12px;user-select:none;width:100%;}
.mps-toolbar{display:flex;flex-wrap:wrap;gap:5px;align-items:center;background:#232323;border:1px solid #3a3a3a;border-radius:6px;padding:6px;}
.mps-toolbar button{background:#333;color:#eee;border:1px solid #4a4a4a;border-radius:4px;padding:3px 8px;cursor:pointer;font-size:12px;}
.mps-toolbar button:hover:not(:disabled){background:#454545;}
.mps-toolbar button:disabled{opacity:.4;cursor:default;}
.mps-toolbar select{background:#2a2a2a;color:#eee;border:1px solid #4a4a4a;border-radius:4px;padding:2px 4px;}
.mps-toolbar select.mps-size{min-width:88px;flex:0 0 auto;}
.mps-toolbar .mps-chk{display:flex;align-items:center;gap:3px;color:#bbb;}
.mps-toolbar .mps-spacer{flex:1;}
.mps-main{display:flex;gap:6px;align-items:stretch;flex:1 1 auto;min-height:0;}
.mps-canvasWrap{position:relative;flex:1 1 auto;min-width:0;min-height:120px;background:#101010;border:1px solid #3a3a3a;border-radius:6px;display:flex;justify-content:center;align-items:center;padding:8px;outline:none;overflow:hidden;}
.mps-canvasWrap:focus{border-color:#5a7db0;}
.mps-canvas{position:relative;background:repeating-linear-gradient(0deg,#151515 0 31px,#181818 31px 32px),repeating-linear-gradient(90deg,#151515 0 31px,#181818 31px 32px);border:1px solid #444;}
.mps-underlay{position:absolute;inset:0;width:100%;height:100%;object-fit:fill;opacity:.55;pointer-events:none;z-index:0;}
.mps-box{position:absolute;border:1.5px solid;box-sizing:border-box;cursor:move;background:rgba(255,255,255,.03);z-index:1;}
.mps-box.sel{box-shadow:0 0 0 2px #fff inset;background:rgba(255,255,255,.07);}
.mps-chip{position:absolute;top:-10px;left:2px;font-size:10px;line-height:1;padding:1px 4px;border-radius:3px;background:#000c;white-space:nowrap;pointer-events:none;}
.mps-h{position:absolute;width:9px;height:9px;background:#fff;border:1px solid #000;border-radius:2px;z-index:2;}
.mps-h.nw{left:-5px;top:-5px;cursor:nwse-resize;} .mps-h.ne{right:-5px;top:-5px;cursor:nesw-resize;}
.mps-h.sw{left:-5px;bottom:-5px;cursor:nesw-resize;} .mps-h.se{right:-5px;bottom:-5px;cursor:nwse-resize;}
.mps-capbox{width:100%;min-height:300px;background:#1b1b1b;color:#eee;border:1px solid #444;border-radius:4px;padding:8px;font-size:13px;resize:vertical;font-family:inherit;}
.mps-side{width:292px;flex:0 0 292px;display:flex;flex-direction:column;gap:6px;min-height:0;overflow:auto;}
.mps-panel{background:#232323;border:1px solid #3a3a3a;border-radius:6px;padding:6px;}
.mps-panel h4{margin:0 0 5px;font-size:12px;color:#b9b9b9;font-weight:600;}
.mps-row{display:flex;gap:4px;align-items:center;margin:3px 0;}
.mps-row>label{flex:0 0 58px;color:#a5a5a5;font-size:11px;}
.mps-row>input[type=text],.mps-row>select{flex:1;min-width:0;background:#1b1b1b;color:#eee;border:1px solid #444;border-radius:4px;padding:2px 4px;font-size:12px;}
.mps-textarea{width:100%;box-sizing:border-box;background:#1b1b1b;color:#eee;border:1px solid #444;border-radius:4px;padding:4px;font-size:12px;resize:vertical;font-family:inherit;margin:2px 0;}
/* palette 色块编辑：点色块开原生拾色器，× 删除，＋ 添加 */
.mps-swbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:2px 0;}
.mps-sw{position:relative;width:28px;height:22px;border:1px solid #555;border-radius:4px;overflow:hidden;background:#1b1b1b;}
.mps-sw input[type=color]{position:absolute;inset:0;width:100%;height:100%;border:none;padding:0;background:none;cursor:pointer;}
.mps-sw .mps-swx{position:absolute;top:-1px;right:0;width:11px;height:11px;line-height:10px;text-align:center;font-size:9px;color:#fff;background:#000a;border-radius:2px;cursor:pointer;opacity:0;}
.mps-sw:hover .mps-swx{opacity:1;}
.mps-layer{display:flex;gap:4px;align-items:center;padding:2px 4px;border-radius:4px;cursor:pointer;}
.mps-layer:hover{background:#2e2e2e;}
.mps-layer.sel{background:#37517a;}
.mps-layer .dot{width:8px;height:8px;border-radius:2px;flex:0 0 8px;}
.mps-layer .cap{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;}
.mps-mini{background:#333;color:#ddd;border:1px solid #4a4a4a;border-radius:3px;padding:0 5px;font-size:11px;cursor:pointer;}
.mps-mini:hover{background:#454545;}
.mps-overlap-pair{margin:4px 0;padding:4px;background:#1b1b1b;border-radius:4px;font-size:11px;}
.mps-status{padding:4px 8px;border-radius:6px;background:#232323;border:1px solid #3a3a3a;color:#bbb;white-space:pre-wrap;}
.mps-sizer{height:10px;flex:0 0 10px;margin:2px -2px 0;cursor:ns-resize;background:linear-gradient(#2a2a2a,#2a2a2a) center/46px 3px no-repeat;border-radius:3px;}
.mps-sizer:hover{background-color:#3a3a3a;}
.mps-vdivider{flex:0 0 5px;width:5px;cursor:col-resize;background:#2a2a2a;border-radius:2px;}
.mps-vdivider:hover{background:#3a4a6a;}
/* 窄节点自适应：固定 292px 侧栏会把画布挤成一条竖缝（文字逐字竖排），按可用宽度降级 */
.mps-compact .mps-side{width:224px !important;flex-basis:224px !important;}
.mps-narrow .mps-main{flex-direction:column;overflow:hidden;}
.mps-narrow .mps-vdivider{display:none;}
.mps-narrow .mps-side{width:auto !important;flex:0 0 auto !important;max-height:240px;}
.mps-narrow .mps-canvasWrap{min-height:200px;}
.mps-narrow .mps-capbox{min-height:200px;}
.mps-status.err{color:#ff8b8b;border-color:#6b3a3a;}
.mps-status.ok{color:#8fd48f;}
.mps-msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#888;pointer-events:none;padding:20px;text-align:center;}
.mps-msg.err{color:#ff8b8b;white-space:pre-wrap;}
.mps-cfgbar{display:flex;gap:5px;align-items:center;padding:3px 4px;font-family:system-ui,'Segoe UI',sans-serif;font-size:12px;user-select:none;width:100%;}
.mps-cfgbar select{background:#2a2a2a;color:#eee;border:1px solid #4a4a4a;border-radius:4px;padding:2px 4px;max-width:200px;}
.mps-cfgbar button{background:#333;color:#eee;border:1px solid #4a4a4a;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:12px;}
.mps-cfgbar button:hover{background:#454545;}
.mps-cfgmsg{color:#8fd48f;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
`;

function injectStyle() {
  if (document.getElementById("mps-style")) return;
  const st = document.createElement("style");
  st.id = "mps-style";
  st.textContent = CSS;
  document.head.appendChild(st);
}

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
    bindEditsWidget();
    return !!(wSource && wEdits && wCw && wCh);
  };
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

  if (node.size[0] < 860 || node.size[1] < 900) node.size = [Math.max(node.size[0], 860), Math.max(node.size[1], 900)];

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

  // ---------- DOM ----------
  const root = document.createElement("div");
  root.className = "mps-root";

  const toolbar = document.createElement("div");
  toolbar.className = "mps-toolbar";
  const mkBtn = (txt, fn, title) => {
    const b = document.createElement("button");
    b.textContent = txt; if (title) b.title = title;
    b.addEventListener("click", fn);
    toolbar.appendChild(b);
    return b;
  };
  const btnUndo = mkBtn("↩撤销", () => doUndo(), "Ctrl+Z");
  const btnRedo = mkBtn("↪重做", () => doRedo(), "Ctrl+Y");
  const sep = () => { const s = document.createElement("span"); s.textContent = "·"; s.style.opacity = "0.4"; toolbar.appendChild(s); };
  sep();
  mkBtn("+物体", () => addItem("object"));
  mkBtn("+人物", () => addItem("character"));
  mkBtn("+文字", () => addItem("text"));
  sep();
  const btnDel = mkBtn("🗑删除", () => { if (M.sel != null) delItem(M.sel); }, "Delete");
  const selRatio = document.createElement("select");
  selRatio.className = "mps-size";
  selRatio.appendChild(new Option("比例…", ""));
  RATIOS.forEach((r) => selRatio.appendChild(new Option(r, r)));
  selRatio.appendChild(new Option("自定义宽高…", "__custom__"));
  selRatio.title = "换比例，保持当前像素面积（当前值显示在框上）；官方支持任意比例，宽高自动取 16 倍数；也可选“自定义宽高…”直接输入任意 16 倍数尺寸";
  selRatio.addEventListener("change", () => {
    if (selRatio.value === "__custom__") applyCustomSize();
    else applyResize(selRatio.value, null);
    selRatio.value = "";
    refreshSizeSelectors();
  });
  toolbar.appendChild(selRatio);
  const selMP = document.createElement("select");
  selMP.className = "mps-size";
  selMP.appendChild(new Option("像素…", ""));
  MP_OPTS.forEach(([v, t]) => selMP.appendChild(new Option(t, v)));
  selMP.title = "换像素规模（百万像素），保持当前比例（当前值显示在框上）；官方范围 0.25M–4M，推荐 1M/1.5M/2M";
  selMP.addEventListener("change", () => { applyResize(null, selMP.value); selMP.value = ""; refreshSizeSelectors(); });
  toolbar.appendChild(selMP);
  const snapChk = document.createElement("input");
  snapChk.type = "checkbox"; snapChk.checked = true; snapChk.id = "mps-snap-" + node.id;
  const snapLbl = document.createElement("label");
  snapLbl.className = "mps-chk"; snapLbl.htmlFor = snapChk.id; snapLbl.textContent = "16吸附";
  snapLbl.prepend(snapChk);
  toolbar.appendChild(snapLbl);
  const undChk = document.createElement("input");
  undChk.type = "checkbox"; undChk.checked = false;
  const undLbl = document.createElement("label");
  undLbl.className = "mps-chk"; undLbl.textContent = "底图"; undLbl.prepend(undChk);
  toolbar.appendChild(undLbl);
  const spacer = document.createElement("span");
  spacer.className = "mps-spacer";
  toolbar.appendChild(spacer);
  // LLM 配置同步：把当前节点的连接配置复制到另外两个 MOSS LLM 节点（不含 api_key）
  mkBtn("⇄同步", () => {
    const n = syncLlmConfig(node, app.graph, app);
    cfgNotify(`已同步 LLM 连接配置（不含 api_key）到另外 ${n} 个节点`);
  }, "把当前 provider/base_url/model/temperature/thinking/timeout 复制到另外两个 MOSS 节点；api_key 不复制（会随工作流明文保存，建议放 config.json）");
  const btnCopy = mkBtn("⧉复制", async () => {
    try { await navigator.clipboard.writeText(wEdits.value || ""); btnCopy.textContent = "已复制"; setTimeout(() => (btnCopy.textContent = "⧉复制"), 1200); } catch (e) { /* noop */ }
  }, "复制编辑后的草稿 JSON");
  // ⚡拆解：只提交编辑器单节点执行（OUTPUT_NODE），结果经事件总线写回画布
  const btnSplit = mkBtn("⚡拆解", () => runAiSplit(btnSplit), "把 source 散文拆解为布局草稿（只执行本节点，不跑整个工作流）");
  mkBtn("⟲重置", () => { if (confirm("重置为粘贴源并丢弃画布编辑？")) rebuildFromWidgets({ preferEdits: false, clearEditsOnEmpty: true }); },
    "重置为 source 粘贴源：丢弃当前画布编辑并清空 edits（会二次确认；不能用 ↶撤销 恢复）");

  const main = document.createElement("div");
  main.className = "mps-main";

  const canvasWrap = document.createElement("div");
  canvasWrap.className = "mps-canvasWrap";
  canvasWrap.tabIndex = 0;
  const canvas = document.createElement("div");
  canvas.className = "mps-canvas";
  const underlay = document.createElement("img");
  underlay.className = "mps-underlay";
  underlay.style.display = "none";
  underlay.addEventListener("error", () => { underlay.style.display = "none"; });
  const capBox = document.createElement("textarea");
  capBox.className = "mps-capbox";
  capBox.style.display = "none";
  capBox.style.width = "100%";
  const msg = document.createElement("div");
  msg.className = "mps-msg";
  msg.style.display = "none";
  canvasWrap.appendChild(canvas);
  canvas.appendChild(underlay);
  canvasWrap.appendChild(capBox);
  canvasWrap.appendChild(msg);

  const side = document.createElement("div");
  side.className = "mps-side";
  const mkPanel = (title) => {
    const p = document.createElement("div");
    p.className = "mps-panel";
    const h = document.createElement("h4");
    h.textContent = title;
    p.appendChild(h);
    side.appendChild(p);
    return p;
  };
  const pnlLayers = mkPanel("图层（上=前）");
  const layers = document.createElement("div");
  pnlLayers.appendChild(layers);
  const pnlInsp = mkPanel("检查器");
  const insp = document.createElement("div");
  pnlInsp.appendChild(insp);
  const pnlOv = mkPanel("重叠检测");
  const overlap = document.createElement("div");
  pnlOv.appendChild(overlap);

  const status = document.createElement("div");
  status.className = "mps-status";

  // 侧栏宽度分隔条：拖动调整检查器/图层面板宽度
  const vdiv = document.createElement("div");
  vdiv.className = "mps-vdivider";
  vdiv.title = "拖动调整面板宽度";
  vdiv.addEventListener("pointerdown", (e) => {
    e.stopPropagation();
    e.preventDefault();
    vdiv.setPointerCapture(e.pointerId);
    const startX = e.clientX;
    const startW = side.getBoundingClientRect().width;
    const onMove = (ev) => {
      const w = Math.max(200, Math.min(480, startW - (ev.clientX - startX)));
      side.style.width = w + "px";
      side.style.flexBasis = w + "px";
      layoutCanvas();
    };
    const onUp = () => {
      vdiv.removeEventListener("pointermove", onMove);
      vdiv.removeEventListener("pointerup", onUp);
    };
    vdiv.addEventListener("pointermove", onMove);
    vdiv.addEventListener("pointerup", onUp);
  });

  root.appendChild(toolbar);
  main.appendChild(canvasWrap);
  main.appendChild(vdiv);
  main.appendChild(side);
  root.appendChild(main);
  root.appendChild(status);

  // 底部调整条：拖动同时更新画布高度与节点高度（同步增减，避免布局反馈环）
  const sizer = document.createElement("div");
  sizer.className = "mps-sizer";
  sizer.title = "拖动调整画布高度";
  sizer.addEventListener("pointerdown", (e) => {
    e.stopPropagation();
    e.preventDefault();
    sizer.setPointerCapture(e.pointerId);
    const startY = e.clientY;
    const startCanvasH = M.canvasH || 660;
    const startNodeH = node.size[1];
    const onMove = (ev) => {
      M.canvasH = Math.max(420, Math.min(3000, startCanvasH + (ev.clientY - startY)));
      const targetH = Math.max(900, startNodeH + (M.canvasH - startCanvasH));
      if (Math.abs(node.size[1] - targetH) > 0.5) node.setSize([node.size[0], targetH]);
      node.setDirtyCanvas?.(true, true);
      layoutCanvas();
    };
    const onUp = () => {
      sizer.removeEventListener("pointermove", onMove);
      sizer.removeEventListener("pointerup", onUp);
    };
    sizer.addEventListener("pointermove", onMove);
    sizer.addEventListener("pointerup", onUp);
  });
  root.appendChild(sizer);

  // 防止画布交互触发节点拖拽/画布平移
  root.addEventListener("pointerdown", (e) => e.stopPropagation());

  const M = (node._mps = {
    S: null, sel: null, undo: [], redo: [], inspPre: null, canvasH: 660,
    wSource, wEdits, wCw, wCh,
    els: { canvasWrap, canvas, underlay, undChk, capBox, msg, layers, insp, overlap, status, snapChk, btnUndo, btnRedo, btnDel, pnlLayers, pnlInsp, pnlOv },
    builtSig: null, pendSig: null, timer: null, wt: null,
    editsDirty: false, editsCb: null, writingEdits: false,
  });

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

  // 响应式降级：节点被拖窄（或加载了窄节点）时，先收窄侧栏、再改为上下堆叠，
  // 保证画布/文本区始终有可读宽度，不出现"文字逐字竖排"的挤压变形。
  const applyResponsive = () => {
    const w = root.clientWidth;
    if (!w) return;
    root.classList.toggle("mps-compact", w >= 520 && w < 720);
    root.classList.toggle("mps-narrow", w < 520);
    applyWidthPin();
    try { layoutCanvas(); } catch (e) { /* noop */ }
  };

  // 宽度钉子：新前端偶发把 DOM widget 包装层宽度写塌（节点框很宽、内容列只剩 ~230px，
  // 工具栏折成数行、面板堆叠）。检测到实际可用宽度远小于节点逻辑宽度时，
  // 直接把根元素钉在 node.size 对应的宽度上撑住布局；节点宽度变化时重新决策。
  const applyWidthPin = () => {
    const target = Math.max(200, Math.round(node.size[0]) - 20);
    if (M.pinTarget !== target) {
      M.pinTarget = target;
      M.pinned = false;
      root.style.width = "";
    }
    const cw = root.clientWidth || 0;
    if (!M.pinned && cw > 0 && cw < target - 60) {
      root.style.width = target + "px";
      M.pinned = true;
      root.classList.remove("mps-compact", "mps-narrow"); // 钉宽后按满宽布局渲染
    }
  };
  M.pinTarget = 0;
  M.pinned = false;
  applyWidthPin();
  try {
    new ResizeObserver(applyResponsive).observe(root);
  } catch (e) { /* 不支持 ResizeObserver 时维持固定布局 */ }

  undChk.addEventListener("change", () => {
    underlay.style.display = undChk.checked && underlay.src ? "" : "none";
  });

  const domw = node.addDOMWidget("画布", "mps_canvas", root, {
    serialize: false,
    hideOnZoom: false,
  });
  // computeSize 必须返回"稳定"高度：高度若依赖其他控件的实时高度（它们随内容变化），
  // 会形成 litegraph 布局反馈环，导致连线端口错位漂移（Edge 下尤其明显）。
  // 画布高度只由底部调整条（mps-sizer）修改 M.canvasH 驱动。
  domw.computeSize = function (width) {
    return [Math.max(Number(width) || 860, 860), Math.max(420, M.canvasH || 660)];
  };

  // ---------- 状态 ----------
  const snapshot = () => snapshotOf(M.S);
  const currentSig = () => `${wSource.value}\u0000${wCw.value},${wCh.value}`;

  function syncWidgets() {
    if (M.S && M.S.kind === "layout") {
      wCw.value = M.S.d.width;
      wCh.value = M.S.d.height;
      if (wCw.inputEl) wCw.inputEl.value = wCw.value;
      if (wCh.inputEl) wCh.inputEl.value = wCh.value;
    }
    M.builtSig = currentSig();
    M.pendSig = M.builtSig;
  }

  // edits 归属签名：写入画布内容时同时记录当时 source 的签名。后端/加载恢复据此
  // 判断“这份 edits 是否仍对应当前 source”，换源后旧画布自动失效。
  function setSourceSig(value) {
    if (!wSrcSig) return;
    const v = value || "";
    wSrcSig.value = v;
    if (wSrcSig.inputEl) wSrcSig.inputEl.value = v;
  }
  const currentSourceSig = () => sourceSignature((wSource.value || "").trim());

  function writeEdits(opts = {}) {
    // M.S 为空只代表“当前无法解析”，不能顺手清空已保存的 edits；
    // 只有显式重置/清空时才允许传 clear:true。
    M.writingEdits = true;
    try {
      if (!M.S) {
        if (opts.clear) {
          wEdits.value = "";
          if (wEdits.inputEl) wEdits.inputEl.value = "";
          setSourceSig("");
        }
      } else {
        wEdits.value = serializeDraft(M.S);
        if (wEdits.inputEl) wEdits.inputEl.value = wEdits.value;
        // 只要画布内容来自当前 source（或手改后的 edits 被接管），都刷新归属签名
        setSourceSig(currentSourceSig());
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
    if (!wEdits || wEdits.__mpsEditsCb) return;
    const prev = wEdits.callback;
    wEdits.callback = function (...args) {
      const r = prev ? prev.apply(this, args) : undefined;
      if (!M.writingEdits) scheduleAdoptEdits();
      return r;
    };
    wEdits.__mpsEditsCb = true;
  }

  function scheduleAdoptEdits() {
    M.editsDirty = true;
    clearTimeout(M.editsCb);
    M.editsCb = setTimeout(() => {
      try { adoptEdits(true); } catch (e) { /* noop */ }
    }, 350);
  }

  function adoptEdits(showErrors = true) {
    clearTimeout(M.editsCb);
    const raw = (wEdits.value || "").trim();
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
    M.sel = null; M.undo = []; M.redo = [];
    if (pre) pushUndo(pre);
    syncWidgets(); renderAll(); updateToolbar(); hideMsg();
    // 用户手改 edits 是明确意图：把这份内容归属到当前 source，后端继续以 edits 为准
    setSourceSig(currentSourceSig());
    return true;
  }
  const writeEditsSoon = () => {
    clearTimeout(M.wt);
    M.wt = setTimeout(writeEdits, 300);
  };
  const flushEdits = () => {
    clearTimeout(M.wt);
    rebindWidgets();
    try {
      // 用户刚手改过 edits：先接管；有效且 source 未变时保留用户原始文本，不重排覆盖。
      // 不只依赖 widget callback：Queue 前直接对比文本，callback 没触发也能兜住。
      const manualText = (wEdits.value || "").trim();
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
    return parseSourceText(src, +wCw.value || 1024, +wCh.value || 1024);
  }

  function setStateFromText(src, opts = {}) {
    M.S = applySourceText(src);
    M.sel = null; M.undo = []; M.redo = [];
    hideMsg();
    syncWidgets(); renderAll();
    if (opts.write !== false) {
      writeEdits();
    } else {
      M.editsDirty = false;
      clearTimeout(M.editsCb);
    }
    updateToolbar();
  }

  function rebuildFromWidgets(opts = {}) {
    const { preferEdits = false, write = true, clearEditsOnEmpty = false } = opts;
    rebindWidgets();
    M.editsDirty = false;
    clearTimeout(M.editsCb);
    const src = (wSource.value || "").trim();
    let edits = (wEdits.value || "").trim();
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
    const storedSig = wSrcSig ? String(wSrcSig.value || "").trim() : "";
    const editsValid = !!edits && (!src || !storedSig || storedSig === srcSig);

    const candidates = [];
    if (preferEdits) {
      if (editsValid) candidates.push({ text: edits, fromEdits: true });
      if (src) candidates.push({ text: src, fromEdits: false });
      if (!editsValid && edits) showMsg("source 已变化：本次以 source 为准，旧画布编辑已忽略", false);
    } else {
      if (src) candidates.push({ text: src, fromEdits: false });
      else if (edits) candidates.push({ text: edits, fromEdits: true });
    }

    if (!candidates.length) {
      showMsg("粘贴源为空 —— 在 source 框里贴入上游产出的中文草稿 JSON", false);
      M.builtSig = M.pendSig = currentSig();
      renderAll();
      if (clearEditsOnEmpty) writeEdits({ clear: true });
      updateToolbar();
      return;
    }

    // 换源且旧 edits 已失效：先清掉，避免解析失败时继续保留会遮住 source 的旧画布
    if (!editsValid && edits && src && !preferEdits) {
      wEdits.value = "";
      if (wEdits.inputEl) wEdits.inputEl.value = "";
      setSourceSig("");
    }

    let lastErr = null;
    for (const c of candidates) {
      try {
        // 从 edits 恢复时保留原始字符串；回退到 source 时必须写回，否则旧的非法 edits 会一直优先
        setStateFromText(c.text, { write: write !== false || !c.fromEdits });
        // 旧工作流没有归属签名：成功接管后立即补写，之后换源即可正确失效
        if (c.fromEdits) setSourceSig(currentSourceSig());
        return;
      } catch (e) {
        lastErr = e;
      }
    }

    // 解析失败：保留原 edits，不写空，避免把已保存的布局冲掉
    showMsg(String((lastErr && lastErr.message) || lastErr || "无法解析"), true);
    M.builtSig = M.pendSig = currentSig();
    renderAll(); updateToolbar();
  }

  // AI 拆解结果（ui.mps_draft）写回 edits 后从这里重建画布
  function rebuildFromEdits() {
    try {
      setStateFromText(wEdits.value);
    } catch (e) {
      /* edits 内容异常时保持现状 */
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
    if (!wAi) return;
    wAi.value = !!v;
    if (wAi.inputEl) wAi.inputEl.checked = !!v;
  };
  node._mpsApplyReverse = function (p) {
    if (!p || typeof p.text !== "string" || !p.text.trim()) {
      return { ok: false, error: "empty" };
    }
    const rec = parseReverseRecord(wSig ? wSig.value : "");
    const decision = reverseDecision(rec, {
      ownerId: node.id,
      sig: p.sig,
      mode: p.mode,
      replaceEdits: wReplace ? !!wReplace.value : true,
      edits: wEdits ? wEdits.value : "",
    });
    if (decision !== "apply") return { ok: decision === "same", skipped: decision };

    if (p.width) setWidgetInput(wCw, p.width);
    if (p.height) setWidgetInput(wCh, p.height);
    // 反推结果是“已处理好的输入”，三种形态都关掉自动拆解；prose 由用户手动点 ⚡拆解
    setAiDraft(false);
    if (p.mode === "prose" || p.mode === "draft") {
      setWidgetInput(wSource, p.text);
      rebuildFromWidgets({ preferEdits: false, write: true });
    } else if (p.mode === "canvas") {
      setWidgetInput(wEdits, p.text);
      rebuildFromEdits();
      // canvas_zh 也是当前 source 对应的画布：记录归属，换源后自动失效
      setSourceSig(currentSourceSig());
    } else {
      return { ok: false, error: "mode" };
    }
    if (!M.S) return { ok: false, error: "parse" };
    // 记录只存 owner/sig/mode + draftHash（不重复保存整份画布文本，控制工作流体积）
    if (wSig) setWidgetInput(wSig, reverseRecord(node.id, p.sig, p.mode, wEdits ? wEdits.value : ""));
    updateStatus();
    return { ok: true };
  };

  // ---------- 渲染 ----------
  function layoutCanvas() {
    const isLayout = M.S && M.S.kind === "layout";
    canvas.style.display = isLayout ? "" : "none";
    capBox.style.display = M.S && M.S.kind === "caption" ? "" : "none";
    if (!isLayout) return;
    const avail = Math.max(160, canvasWrap.clientWidth - 18);
    const Wd = M.S.d.width, Hd = M.S.d.height;
    let dw = avail, dh = (dw * Hd) / Wd;
    const maxH = 540;
    if (dh > maxH) { dh = maxH; dw = (dh * Wd) / Hd; }
    canvas.style.width = dw + "px";
    canvas.style.height = dh + "px";
  }

  function renderBoxes() {
    canvas.querySelectorAll(".mps-box").forEach((b) => b.remove());
    if (!M.S || M.S.kind !== "layout") return;
    const { width: Wd, height: Hd, items } = M.S.d;
    items.forEach((it, i) => {
      const [x0, y0, x1, y1] = it.bbox;
      const b = document.createElement("div");
      b.className = "mps-box" + (M.sel === i ? " sel" : "");
      b.dataset.i = i;
      // 图层顺序：数组 0 = 面板最上面 = 画面最前，所以 0 的 z-index 最大
      b.style.zIndex = String(items.length - i);
      b.style.left = (x0 / Wd) * 100 + "%";
      b.style.top = (y0 / Hd) * 100 + "%";
      b.style.width = ((x1 - x0) / Wd) * 100 + "%";
      b.style.height = ((y1 - y0) / Hd) * 100 + "%";
      b.style.borderColor = COLORS[it.label] || "#999";
      const chip = document.createElement("div");
      chip.className = "mps-chip";
      chip.textContent = `${i}·${it.label}`;
      chip.style.color = COLORS[it.label] || "#999";
      chip.title = it.caption || "";
      b.appendChild(chip);
      for (const h of ["nw", "ne", "sw", "se"]) {
        const hd = document.createElement("div");
        hd.className = "mps-h " + h;
        hd.addEventListener("pointerdown", (e) => startGesture(e, i, h));
        b.appendChild(hd);
      }
      b.addEventListener("pointerdown", (e) => startGesture(e, i, null));
      b.addEventListener("dblclick", () => { const t = insp.querySelector("textarea[data-f='caption']"); if (t) { t.focus(); t.select(); } });
      canvas.appendChild(b);
    });
  }

  function updateBoxStyle(i) {
    const b = canvas.querySelector(`.mps-box[data-i="${i}"]`);
    if (!b || !M.S || M.S.kind !== "layout") return;
    const { width: Wd, height: Hd } = M.S.d;
    const [x0, y0, x1, y1] = M.S.d.items[i].bbox;
    b.style.left = (x0 / Wd) * 100 + "%";
    b.style.top = (y0 / Hd) * 100 + "%";
    b.style.width = ((x1 - x0) / Wd) * 100 + "%";
    b.style.height = ((y1 - y0) / Hd) * 100 + "%";
  }

  function renderLayers() {
    const isLayout = !!(M.S && M.S.kind === "layout");
    M.els.pnlLayers.style.display = isLayout ? "" : "none";
    layers.innerHTML = "";
    if (!isLayout) return;
    M.S.d.items.forEach((it, i) => {
      const row = document.createElement("div");
      row.className = "mps-layer" + (M.sel === i ? " sel" : "");
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = COLORS[it.label] || "#999";
      const cap = document.createElement("span");
      cap.className = "cap";
      cap.textContent = `${i}·${it.label} ${(it.caption || "").slice(0, 16)}`;
      cap.title = it.caption || "";
      row.appendChild(dot); row.appendChild(cap);
      const mk = (t, fn, title) => {
        const b = document.createElement("button");
        b.className = "mps-mini"; b.textContent = t; b.title = title || "";
        b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
        row.appendChild(b);
      };
      mk("↑", () => layerMove(i, -1), "上移一层");
      mk("↓", () => layerMove(i, +1), "下移一层");
      mk("✕", () => delItem(i), "删除");
      row.addEventListener("click", () => select(i));
      layers.appendChild(row);
    });
  }

  function bindField(el, setter) {
    el.addEventListener("focus", () => { M.inspPre = snapshot(); });
    el.addEventListener("input", () => {
      setter(el.value);
      updateStatus();
      writeEditsSoon();
    });
    el.addEventListener("blur", () => {
      // 点 Queue 会先触发 blur：立刻写回，不能等 300ms 防抖
      writeEdits();
      if (M.inspPre != null && snapshot() !== M.inspPre) { pushUndo(M.inspPre); updateToolbar(); }
      M.inspPre = null;
    });
  }

  // ---------- palette 色块编辑（对齐官方 layout 的 hex 数组语义）----------
  // summary.palette = 整图主配色倾向；items[].palette = 该元素自身配色。
  // 交互：点色块调原生拾色器、× 删除、＋ 添加（复制上一色）；色值即时写回草稿。
  const HEX_RE = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i;
  const normHex = (c) => (HEX_RE.test(String(c || "").trim()) ? String(c).trim() : "#888888");
  function buildPaletteEditor(host, getArr, setArr) {
    const bar = document.createElement("div");
    bar.className = "mps-swbar";
    host.appendChild(bar);
    const draw = () => {
      bar.innerHTML = "";
      const arr = getArr();
      arr.forEach((c, i) => {
        const sw = document.createElement("span");
        sw.className = "mps-sw";
        sw.title = c + "（点击改色）";
        const inp = document.createElement("input");
        inp.type = "color";
        inp.value = normHex(c);
        inp.addEventListener("focus", () => { M.inspPre = snapshot(); });
        inp.addEventListener("input", () => {
          const a = getArr(); a[i] = inp.value; setArr(a);
          afterLightChange();
        });
        inp.addEventListener("blur", () => {
          writeEdits();
          if (M.inspPre != null && snapshot() !== M.inspPre) { pushUndo(M.inspPre); updateToolbar(); }
          M.inspPre = null;
        });
        const x = document.createElement("span");
        x.className = "mps-swx";
        x.textContent = "×";
        x.title = "删除这个颜色";
        x.addEventListener("click", (e) => {
          e.stopPropagation();
          pushUndo(snapshot());
          const a = getArr(); a.splice(i, 1); setArr(a);
          afterChange();
        });
        sw.appendChild(inp);
        sw.appendChild(x);
        bar.appendChild(sw);
      });
      if (!arr.length) {
        const tip = document.createElement("span");
        tip.style.cssText = "color:#777;font-size:11px;";
        tip.textContent = "（无颜色，点＋添加）";
        bar.appendChild(tip);
      }
    };
    const add = document.createElement("button");
    add.className = "mps-mini";
    add.textContent = "＋颜色";
    add.title = "添加一个颜色（从上一个颜色复制）";
    add.addEventListener("click", (e) => {
      e.stopPropagation();
      pushUndo(snapshot());
      const a = getArr();
      a.push(a[a.length - 1] || "#888888");
      setArr(a);
      afterChange();
    });
    host.appendChild(add);
    draw();
    return draw;
  }

  // 写回辅助：空数组则删除该可选字段（官方 schema 里 meta/palette/tags 均可省略）
  const setOptArr = (obj, key) => (a) => { if (a.length) obj[key] = a; else delete obj[key]; };

  function renderInspector() {
    const isLayout = !!(M.S && M.S.kind === "layout");
    M.els.pnlInsp.style.display = M.S ? "" : "none";
    insp.innerHTML = "";
    if (!M.S) return;

    if (M.S.kind === "caption") {
      const lab = document.createElement("div");
      lab.textContent = "caption（画布中央文本框同步编辑）";
      lab.style.color = "#a5a5a5";
      insp.appendChild(lab);
      return;
    }

    const d = M.S.d, s = d.summary;
    const h5 = (t) => { const e = document.createElement("div"); e.style.cssText = "color:#8f8f8f;font-size:11px;margin:5px 0 2px;"; e.textContent = t; return e; };

    insp.appendChild(h5("summary"));
    const sc = document.createElement("textarea");
    sc.className = "mps-textarea"; sc.rows = 3; sc.value = s.caption || ""; sc.dataset.f = "summary.caption";
    bindField(sc, (v) => { s.caption = v; });
    insp.appendChild(sc);
    // meta：体裁标签（字符串数组，自由填写；不存在也能直接添加）
    const row = document.createElement("div"); row.className = "mps-row";
    const l = document.createElement("label"); l.textContent = "meta 体裁";
    const inp = document.createElement("input"); inp.type = "text";
    inp.value = csvShow(s.meta);
    inp.placeholder = "product poster, studio photography";
    inp.title = "整图体裁标签，逗号分隔（原文会作为独立 JSON 字段进入提示词）";
    bindField(inp, (v) => setOptArr(s, "meta")(csvParse(v)));
    row.appendChild(l); row.appendChild(inp); insp.appendChild(row);
    if ("canvas" in s) {
      const row2 = document.createElement("div"); row2.className = "mps-row";
      const l2 = document.createElement("label"); l2.textContent = "canvas";
      const selc = document.createElement("select");
      VALID_CANVAS.forEach((c) => selc.appendChild(new Option(c, c)));
      selc.value = s.canvas;
      selc.addEventListener("change", () => { pushUndo(snapshot()); s.canvas = selc.value; afterChange(); });
      row2.appendChild(l2); row2.appendChild(selc); insp.appendChild(row2);
    }
    // palette：整图主配色（hex 色块编辑，不拼进 caption，作为独立字段进提示词）
    insp.appendChild(h5("palette 主配色（hex）"));
    buildPaletteEditor(insp, () => s.palette || [], setOptArr(s, "palette"));

    insp.appendChild(h5("选中元素"));
    if (M.sel == null) {
      const tip = document.createElement("div");
      tip.style.color = "#777";
      tip.textContent = "点击画布中的框或图层行选中";
      insp.appendChild(tip);
      return;
    }
    const it = d.items[M.sel];
    const rowL = document.createElement("div"); rowL.className = "mps-row";
    const lL = document.createElement("label"); lL.textContent = "label";
    const selL = document.createElement("select");
    VALID_LABELS.forEach((x) => selL.appendChild(new Option(x, x)));
    selL.value = it.label;
    selL.addEventListener("change", () => {
      pushUndo(snapshot());
      it.label = selL.value;
      if (it.label === "text" && !("text" in it)) it.text = "";
      if (it.label !== "text") delete it.text;
      afterChange();
    });
    rowL.appendChild(lL); rowL.appendChild(selL); insp.appendChild(rowL);

    const cap = document.createElement("textarea");
    cap.className = "mps-textarea"; cap.rows = 3; cap.value = it.caption || ""; cap.dataset.f = "caption";
    bindField(cap, (v) => { it.caption = v; });
    insp.appendChild(cap);

    if (it.label === "text") {
      insp.appendChild(h5("text 画面可见文字（保留原文，勿译）"));
      const tx = document.createElement("textarea");
      tx.className = "mps-textarea"; tx.rows = 2; tx.value = it.text || "";
      bindField(tx, (v) => { it.text = v; });
      insp.appendChild(tx);
    }
    // tags：该元素的属性标签（常驻可添加）
    insp.appendChild(h5("tags 元素标签"));
    const rowT = document.createElement("div"); rowT.className = "mps-row";
    const inpT = document.createElement("input"); inpT.type = "text";
    inpT.value = csvShow(it.tags);
    inpT.placeholder = "matte, ceramic, soft shadow";
    inpT.title = "元素属性标签，逗号分隔（作为独立 JSON 字段进入提示词）";
    bindField(inpT, (v) => setOptArr(it, "tags")(csvParse(v)));
    rowT.appendChild(inpT); insp.appendChild(rowT);
    // palette：该元素自身配色（hex 色块编辑）
    insp.appendChild(h5("palette 元素配色（hex）"));
    buildPaletteEditor(insp, () => it.palette || [], setOptArr(it, "palette"));
    const del = document.createElement("button");
    del.className = "mps-mini"; del.style.marginTop = "6px"; del.textContent = "🗑 删除该元素";
    del.addEventListener("click", () => delItem(M.sel));
    insp.appendChild(del);
  }

  function renderOverlap() {
    const isLayout = !!(M.S && M.S.kind === "layout");
    M.els.pnlOv.style.display = isLayout ? "" : "none";
    overlap.innerHTML = "";
    if (!isLayout) return;
    const items = M.S.d.items;
    const pairs = [];
    for (let i = 0; i < items.length; i++) {
      for (let j = i + 1; j < items.length; j++) {
        const a = items[i].bbox, b = items[j].bbox;
        const ox = Math.min(a[2], b[2]) - Math.max(a[0], b[0]);
        const oy = Math.min(a[3], b[3]) - Math.max(a[1], b[1]);
        if (ox > 0 && oy > 0) pairs.push([i, j]);
      }
    }
    if (!pairs.length) {
      overlap.innerHTML = '<span style="color:#8fd48f">无重叠 ✓</span>';
      return;
    }
    for (const [i, j] of pairs) {
      const box = document.createElement("div");
      box.className = "mps-overlap-pair";
      const t = document.createElement("div");
      t.textContent = `${i}·${M.S.d.items[i].label} ↔ ${j}·${M.S.d.items[j].label} 重叠`;
      box.appendChild(t);
      const mk = (txt, fn) => {
        const b = document.createElement("button");
        b.className = "mps-mini"; b.style.margin = "3px 4px 0 0"; b.textContent = txt;
        b.addEventListener("click", fn);
        box.appendChild(b);
      };
      mk(`${i} 在前`, () => occlude(i, j));
      mk(`${j} 在前`, () => occlude(j, i));
      overlap.appendChild(box);
    }
  }

  function updateStatus() {
    const errs = validateJS(M.S);
    if (!M.S) { status.className = "mps-status"; status.textContent = "—"; return; }
    const head = M.S.kind === "caption"
      ? `caption · ${M.S.caption.length} 字 · 目标 ${+wCw.value || 1024}×${+wCh.value || 1024}` + (wAi && wAi.value ? " · ⚡AI 拆解已启用：Queue 一次生成布局画布" : "")
      : `布局 ${M.S.d.width}×${M.S.d.height} · ${M.S.d.items.length} 项 · 选中 ${M.sel == null ? "—" : M.sel}`;
    if (errs.length) {
      status.className = "mps-status err";
      status.textContent = `${head}\n⚠ ${errs.length} 个问题：` + errs.slice(0, 3).join("；") + (errs.length > 3 ? " …" : "");
    } else {
      status.className = "mps-status ok";
      status.textContent = `${head} · ✓ 校验通过`;
    }
  }

  // 比例/像素选择器回显：占位项文本实时显示当前画布的比例与像素规模。
  // 选择动作本身是"一次性换算"，选完回落到占位项，靠这里的文本让用户看见当前生效值。
  function refreshSizeSelectors() {
    const d = M.S && M.S.kind === "layout" ? M.S.d : null;
    const W = d ? +d.width : +wCw.value, H = d ? +d.height : +wCh.value;
    if (!W || !H) {
      selRatio.options[0].text = "比例…";
      selMP.options[0].text = "像素…";
      return;
    }
    const gcd = (a, b) => (b ? gcd(b, a % b) : a);
    const G = gcd(W, H) || 1;
    const ratioStr = `${W / G}:${H / G}`;
    // 16 倍数取整会偏离目标比例（如 16:10 变成 79:49），按 3% 容差回显最接近的预设名。
    const actual = W / H;
    const hitRatio = RATIOS.find((r) => {
      const [rw, rh] = r.split(":").map(Number);
      const target = rw / rh;
      return Math.abs(actual - target) <= target * 0.03;
    });
    selRatio.options[0].text = "比例 " + (hitRatio || (ratioStr.length <= 7 ? ratioStr : "自定义"));
    const mp = (W * H) / 1e6;
    const hit = MP_OPTS.find(([v]) => Math.abs(Number(v) - mp) <= 0.12);
    selMP.options[0].text = "像素 " + (hit ? hit[1] : mp.toFixed(2) + "M");
  }

  function updateToolbar() {
    M.els.btnUndo.disabled = !M.undo.length;
    M.els.btnRedo.disabled = !M.redo.length;
    M.els.btnDel.disabled = !(M.S && M.S.kind === "layout" && M.sel != null);
  }

  function renderAll() {
    layoutCanvas();
    renderBoxes();
    renderLayers();
    renderInspector();
    renderOverlap();
    updateStatus();
    refreshSizeSelectors();
    updateToolbar();
    if (M.S && M.S.kind === "caption" && document.activeElement !== capBox) capBox.value = M.S.caption;
  }

  // caption 模式文本框绑定（只绑一次）
  capBox.addEventListener("focus", () => { M.inspPre = M.S ? snapshot() : null; });
  capBox.addEventListener("input", () => { if (M.S && M.S.kind === "caption") { M.S.caption = capBox.value; updateStatus(); writeEditsSoon(); } });
  capBox.addEventListener("blur", () => {
    writeEdits();
    if (M.inspPre != null && M.S && snapshot() !== M.inspPre) { pushUndo(M.inspPre); updateToolbar(); }
    M.inspPre = null;
  });

  // ---------- 编辑操作 ----------
  function pushUndo(pre) {
    M.undo.push(pre);
    if (M.undo.length > 100) M.undo.shift();
    M.redo = [];
  }
  function commit(pre) { pushUndo(pre); afterChange(); }
  function afterChange() { renderAll(); writeEdits(); }
  function afterLightChange() { updateStatus(); writeEditsSoon(); }

  function restore(s) {
    const o = JSON.parse(s);
    if (M.S && M.S.kind === "caption") M.S.caption = o.caption;
    else M.S = { kind: "layout", d: o };
    if (M.sel != null && M.S.kind === "layout" && M.sel >= M.S.d.items.length) M.sel = null;
    syncWidgets(); renderAll(); writeEdits();
  }
  function doUndo() { if (M.undo.length) { M.redo.push(snapshot()); restore(M.undo.pop()); } }
  function doRedo() { if (M.redo.length) { M.undo.push(snapshot()); restore(M.redo.pop()); } }

  function select(i, light) {
    M.sel = i;
    if (light) {
      canvas.querySelectorAll(".mps-box").forEach((b) => b.classList.toggle("sel", +b.dataset.i === M.sel));
      renderLayers(); renderInspector();
    } else {
      renderBoxes(); renderLayers(); renderInspector();
    }
    updateToolbar();
  }

  function addItem(label) {
    if (!M.S || M.S.kind !== "layout") return;
    pushUndo(snapshot());
    const d = M.S.d;
    const w0 = Math.max(16, Math.round((d.width / 3) / 16) * 16);
    const h0 = Math.max(16, Math.round((d.height / 3) / 16) * 16);
    const x0 = Math.round((d.width - w0) / 2), y0 = Math.round((d.height - h0) / 2);
    const it = { label, caption: "", bbox: [x0, y0, x0 + w0, y0 + h0] };
    if (label === "text") it.text = "";
    d.items.push(it);
    M.sel = d.items.length - 1;
    afterChange();
    const t = insp.querySelector("textarea[data-f='caption']");
    if (t) t.focus();
  }

  function delItem(i) {
    if (!M.S || M.S.kind !== "layout" || M.S.d.items[i] == null) return;
    pushUndo(snapshot());
    M.S.d.items.splice(i, 1);
    M.sel = null;
    afterChange();
  }

  function layerMove(i, delta) {
    if (!M.S || M.S.kind !== "layout") return;
    const items = M.S.d.items;
    const j = i + delta;
    if (j < 0 || j >= items.length) return;
    pushUndo(snapshot());
    const t = items[i]; items[i] = items[j]; items[j] = t;
    M.sel = j;
    afterChange();
  }

  function occlude(front, back) {
    if (!M.S || M.S.kind !== "layout") return;
    const items = M.S.d.items;
    if (items[front] == null || items[back] == null) return;
    pushUndo(snapshot());
    items[front].caption = (items[front].caption || "") + OCCL_FRONT;
    items[back].caption = (items[back].caption || "") + OCCL_BACK;
    afterChange();
  }

  // 比例/像素规模组合换算：只选比例→保持当前面积；只选像素→保持当前比例；16 倍数取整
  function resizeLayoutTo(NW, NH) {
    const d = M.S.d;
    NW = clampDim(NW); NH = clampDim(NH);
    pushUndo(snapshot());
    const sx = NW / d.width, sy = NH / d.height;
    d.items.forEach((it) => {
      const x0 = Math.round(it.bbox[0] * sx), x1 = Math.round(it.bbox[2] * sx);
      const y0 = Math.round(it.bbox[1] * sy), y1 = Math.round(it.bbox[3] * sy);
      it.bbox = clampBox([x0, y0, x1, y1], NW, NH);
    });
    d.width = NW; d.height = NH;
    syncWidgets(); afterChange();
  }

  // 自定义宽高：官方允许任意尺寸，只要宽高是 16 的正整数倍。
  function applyCustomSize() {
    if (!M.S) return;
    const isLayout = M.S.kind === "layout";
    const d = isLayout ? M.S.d : null;
    const curW = d ? d.width : (+wCw.value || 1024);
    const curH = d ? d.height : (+wCh.value || 1024);
    const raw = prompt("输入宽×高（16 的倍数，64–15360），如 1536x1024：", `${curW}x${curH}`);
    if (raw == null || !String(raw).trim()) return;
    const m = String(raw).trim().match(/^(\d+)\s*[x×*,\s]\s*(\d+)$/i);
    if (!m) { showMsg("尺寸格式应为 宽x高，如 1536x1024", true); return; }
    const NW = Number(m[1]), NH = Number(m[2]);
    if (!Number.isInteger(NW) || !Number.isInteger(NH) || NW < 64 || NH < 64
        || NW > 15360 || NH > 15360 || NW % 16 || NH % 16) {
      showMsg("宽高必须是 64–15360 之间、16 的整数倍", true);
      return;
    }
    if (NW === curW && NH === curH) { hideMsg(); return; }
    if (!isLayout) {
      wCw.value = NW; wCh.value = NH;
      syncWidgets(); renderAll(); hideMsg();
      return;
    }
    if (!confirm(`画布调整为 ${NW}×${NH}（≈${(NW * NH / 1000000).toFixed(2)}M）并等比缩放所有框？`)) return;
    resizeLayoutTo(NW, NH);
    hideMsg();
  }

  function applyResize(ratioKey, mpVal) {
    if (!M.S) return;
    if (!ratioKey && !mpVal) return;
    const isLayout = M.S.kind === "layout";
    const d = isLayout ? M.S.d : null;
    const curW = d ? d.width : (+wCw.value || 1024);
    const curH = d ? d.height : (+wCh.value || 1024);
    let ratio = curW / curH;
    if (ratioKey) {
      const [rw, rh] = ratioKey.split(":").map(Number);
      ratio = rw / rh;
    }
    const area = mpVal ? Number(mpVal) * 1000000 : curW * curH;
    const [NW, NH] = clampArea(Math.sqrt(area * ratio), Math.sqrt(area / ratio));
    if (NW === curW && NH === curH) return;
    if (!isLayout) {
      // caption 形态没有框可缩放：直接把目标尺寸写进 canvas_width/height（AI 拆解与 Compiler 都读它）
      wCw.value = NW; wCh.value = NH;
      syncWidgets(); renderAll();
      return;
    }
    if (!confirm(`画布调整为 ${NW}×${NH}（≈${(NW * NH / 1000000).toFixed(2)}M）并等比缩放所有框？`)) return;
    resizeLayoutTo(NW, NH);
  }

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
      if (this.size[0] < 860 || this.size[1] < 900) {
        this.size = [Math.max(this.size[0], 860), Math.max(this.size[1], 900)];
      }
      sanitizeAgentMd(this);
      rebindWidgets();
      try { warnApiKeyIfSet(node, showKeyWarn); } catch (e) { /* noop */ }
      rebuildFromWidgets({ preferEdits: true, write: false });
    },
    onResize: () => {
      try { applyWidthPin(); layoutCanvas(); } catch (e) { /* noop */ }
    },
    onRemoved: () => {
      clearInterval(M.timer);
      clearTimeout(M.wt);
      clearTimeout(M.editsCb);
      clearTimeout(wheelIdle);
      unregisterFlush();
      if (M.apiHooked && M.apiRef && typeof M.apiRef.removeEventListener === "function") {
        M.apiRef.removeEventListener("executed", onApiExecuted);
        M.apiRef.removeEventListener("execution_error", onApiError);
        M.apiRef.removeEventListener("status", onApiStatus);
      }
    },
  });
  // 执行结果处理：底图上屏 + AI 拆解写回。
  // 兼容两种消息形态（ui 字典本体 / 包一层 .ui）与两条投递路径（旧前端 onExecuted / 新前端 api 事件总线）
  let lastImgName = null;
  function processExecData(data) {
    if (!data) return;
    if (data.mps_draft !== undefined) {
      // ComfyUI 4.9 的 ws 会把长字符串拆成单字符数组传输，归一成字符串
      let md = Array.isArray(data.mps_draft) ? data.mps_draft.join("") : data.mps_draft;
      if (typeof md === "string" && md.trim()) {
        resetSplitBtn();
        wEdits.value = md;
        rebuildFromEdits();
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

  // ⚡拆解按钮：只提交编辑器单节点（OUTPUT_NODE），结果走事件总线写回
  function resetSplitBtn() {
    if (M.splitBtn) { M.splitBtn.disabled = false; M.splitBtn.textContent = "⚡拆解"; M.splitBtn = null; }
  }
  async function runAiSplit(btn) {
    const api = (typeof app !== "undefined" && app.api) || (window.app && window.app.api);
    if (!api || !api.clientId) { alert("前端尚未就绪，稍后再试"); return; }
    // 判断"要不要拆解"以 source 为准（用户改了散文就要能重新拆解），
    // edits 只是上一次的结果写回，不能因为它含 items 就拦截新散文的拆解。
    const src = (wSource.value || "").trim();
    const cur = src || (wEdits.value || "").trim();
    const looksJson = cur.startsWith("{") || cur.startsWith("[");
    if (!cur) {
      M.els.status.textContent = "source 为空：先在 source 里贴入散文提示词";
      M.els.status.className = "mps-status err";
      return;
    }
    if (looksJson) {
      // 锁定 sig，防止 1.4s 防抖重建立刻覆盖这条提示
      M.builtSig = M.pendSig = currentSig();
      M.els.status.textContent = "源已是布局草稿/JSON，无需拆解（想重拆：把 source 换成散文）";
      M.els.status.className = "mps-status";
      return;
    }
    btn.disabled = true;
    btn.textContent = "⏳拆解中…";
    M.splitBtn = btn;
    M.splitAt = Date.now();
    M.els.status.textContent = "⏳ AI 拆解中…（LLM 生成布局草稿，通常 10~120 秒，完成后画布自动刷新）";
    M.els.status.className = "mps-status";
    const inputs = {};
    for (const nm of ["source", "canvas_width", "canvas_height", "edits", "provider", "base_url", "model", "api_key", "temperature", "thinking", "timeout", "ai_prompt", "agent_md"]) {
      const w = node.widgets.find(x => x.name === nm);
      if (w) inputs[nm] = w.value;
    }
    inputs.ai_draft = true; // 按钮语义就是拆解
    inputs.force_split = true; // 强制重拆：绕过“同输入缓存”，普通 Queue 不会设置
    // 关键：必须清空 edits 再提交。后端 edits 非空且对应当前 source 时优先，
    // 带着上一次的旧草稿提交，服务端只会透传旧布局、不触发 AI 拆解。
    inputs.edits = "";
    try {
      const r = await api.fetchApi("/prompt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: { [String(node.id)]: { class_type: "MOSSEditor", inputs } }, client_id: api.clientId }),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error((j.error && j.error.message) || ("HTTP " + r.status));
      }
    } catch (e) {
      resetSplitBtn();
      M.els.status.textContent = "拆解提交失败: " + e.message;
      M.els.status.className = "mps-status err";
    }
  }

  const onApiError = (ev) => {
    try {
      const d = ev && ev.detail;
      if (!d || String(d.node_id) !== String(node.id)) return;
      resetSplitBtn();
      // 把 sig 标记为已构建：阻止轮询里挂起的 rebuildFromWidgets 立刻覆盖掉这条错误提示
      M.builtSig = M.pendSig = currentSig();
      M.els.status.textContent = "拆解失败: " + (d.exception_message || "未知错误");
      M.els.status.className = "mps-status err";
    } catch (e) { /* noop */ }
  };

  // 队列跑完却没等到本节点的 executed（结果被缓存跳过 / 事件丢失）→ 解除按钮卡死
  const onApiStatus = (ev) => {
    try {
      if (!M.splitBtn) return;
      const st = ev && ev.detail;
      // 兼容两种事件负载：{exec_info:{queue_remaining}} 与 {status:{exec_info:{...}}}
      const q = st && ((st.exec_info && st.exec_info.queue_remaining)
        || (st.status && st.status.exec_info && st.status.exec_info.queue_remaining));
      if (q === 0 && Date.now() - (M.splitAt || 0) > 4000) {
        resetSplitBtn();
        M.builtSig = M.pendSig = currentSig();
        M.els.status.textContent = "队列已空但未收到拆解结果（可能被缓存跳过）：请再点一次 ⚡拆解";
        M.els.status.className = "mps-status err";
      }
    } catch (e) { /* noop */ }
  };

  // LLM 配置同步反馈走状态条
  const cfgNotify = (t) => { M.els.status.textContent = t; M.els.status.className = "mps-status ok"; };

  const prevExec = node.onExecuted;
  node.onExecuted = function (info) {
    try { processExecData(info && (info.ui || info)); } catch (e) { /* noop */ }
    if (prevExec) return prevExec.call(this, info);
  };
  const onApiExecuted = (ev) => {
    try {
      const d = ev && ev.detail;
      if (!d) return;
      if (String(d.node) !== String(node.id) && String(d.display_node) !== String(node.id)) return;
      const out = d.output && (d.output.ui || d.output);
      // 本节点执行完却没带回新草稿（例如 edits 透传 / 输入没变被缓存）：
      // 解除"⏳拆解中"，别让按钮挂着等一个不会来的结果。
      const ui = out || {};
      if (M.splitBtn && ui.mps_draft === undefined && !(out && out.images && out.images.length)) {
        resetSplitBtn();
        M.els.status.textContent = "本次执行没有产生新草稿（源可能没变化被缓存）。改一下 source 再试，或点 ⟲重置";
        M.els.status.className = "mps-status err";
        return;
      }
      processExecData(out);
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
    rebindWidgets();
    try { warnApiKeyIfSet(node, showKeyWarn); } catch (e) { /* noop */ }
    try { applyWidthPin(); } catch (e) { /* noop */ }
    const sig = currentSig();
    if (sig === M.pendSig) {
      // source 改变时以 source 为准；edits 只作为 source 为空/加载恢复时的回退
      if (sig !== M.builtSig && !M.splitBtn) { rebuildFromWidgets({ preferEdits: false }); }
    } else {
      M.pendSig = sig;
    }
    // 拆解按钮卡死硬兜底：180s 仍无结果就解除，避免永远停在"⏳拆解中…"
    if (M.splitBtn && Date.now() - (M.splitAt || 0) > 180000) {
      resetSplitBtn();
      M.builtSig = M.pendSig = currentSig();
      M.els.status.textContent = "拆解超时（180s 无结果）：请检查 LLM 配置后重试 ⚡拆解";
      M.els.status.className = "mps-status err";
    }
  }, 700);

  rebuildFromWidgets({ preferEdits: true, write: false });
}

// 翻译器节点：挂一条 LLM 配置栏（⇄同步到另外两个 MOSS 节点）
function initTranslator(node) {
  if (node._mpsCfgInit) return;
  node._mpsCfgInit = true;
  injectStyle();
  if (!(node.widgets || []).some(w => w.name === "provider")) return;
  if (typeof node.addDOMWidget !== "function") return;
  sanitizeAgentMd(node);
  const prevCfg = node.onConfigure;
  node.onConfigure = function (...a) {
    const r = prevCfg ? prevCfg.apply(this, a) : undefined;
    sanitizeAgentMd(this);
    return r;
  };

  const bar = document.createElement("div");
  bar.className = "mps-cfgbar";
  const msg = document.createElement("span");
  msg.className = "mps-cfgmsg";
  const notify = (t) => {
    msg.textContent = t;
    setTimeout(() => { if (msg.textContent === t) msg.textContent = ""; }, 2500);
  };
  const btn = document.createElement("button");
  btn.textContent = "⇄同步";
  btn.title = "把当前 LLM 连接配置（不含 api_key）复制到另外两个 MOSS 节点；key 建议放 config.json";
  btn.addEventListener("click", () => {
    const n = syncLlmConfig(node, app.graph, app);
    notify(`已同步连接配置（不含 api_key）到另外 ${n} 个节点`);
  });
  bar.append(btn, msg);

  // 明文 key 一次性警告（工作流/执行历史会保存 widget 的 api_key；同 key 只提示一次）
  const showKeyWarn = (t) => {
    try {
      app.extensionManager?.toast?.add({ severity: "warn", summary: "MOSS Studio", detail: t, life: 6000 });
    } catch (e) { /* toast 可选 */ }
    notify("⚠ " + t);
  };
  warnApiKeyIfSet(node, showKeyWarn);
  bindApiKeyWarn(node, showKeyWarn);

  const domw = node.addDOMWidget("LLM配置", "mps_cfg", bar, { serialize: false, hideOnZoom: false });
  domw.computeSize = function (width) {
    return [Math.max(Number(width) || 300, 300), 44];
  };
  bar.addEventListener("pointerdown", (e) => e.stopPropagation());
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
