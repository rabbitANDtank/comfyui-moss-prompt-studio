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
  clampDim, clampArea, clampBox, csvParse, csvShow, sourceSignature, fitCanvasDisplay,
} from "./editor/model.js";
import { editorFlushRegistry, mountEditorLifecycle, mountCompilerSync } from "./editor/comfy-hooks.js";
import { parseReverseRecord, reverseDecision, reverseRecord, textHash } from "./editor/reverse.js";
import { editsOwnerSignature, rebuildCandidates } from "./editor/source_guard.js";
import { queueRemaining, shouldResetOnQueueIdle, shouldResetOnTimeout, queueHasNode,
  isDuplicateDraft, resultAttributionDecision } from "./editor/submit_gate.js";
import { commitDraftWriteback } from "./editor/result_writeback.js";
import { flushFocusedField } from "./editor/focus_guard.js";
import { bindPointerDrag } from "./editor/pointer_drag.js";
import { POSE_MARKER, poseMarkedItems, swapPoseSegments, buildSplitPayload,
  normalizePoseMode, normalizeApplyPose, normalizePoseText, resolvePoseSource,
  poseSourceLabel, poseWriteTarget, poseSourceWritable } from "./editor/pose.js";
import { buildFacets, filterPoses, groupByPose, toDescription, poseThumbUrl } from "./editor/pose_picker.js";
import { listTemplates } from "./editor/templates.js";
import { estimateTextFit, estimatePromptTokens, lintTextItems, TOKEN_WARN_LIMIT } from "./editor/text_fit.js";
import { syncLlmConfig, warnApiKeyIfSet, bindApiKeyWarn } from "./editor/llm_config.js";
import { mountLlmOptions } from "./editor/llm_options.js";
import { mountPoseInputs } from "./editor/pose_inputs.js";

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
.mps-layer{display:flex;gap:4px;align-items:flex-start;padding:3px 4px;border-radius:4px;cursor:pointer;}
.mps-layer:hover{background:#2e2e2e;}
.mps-layer.sel{background:#37517a;}
.mps-layer .dot{width:8px;height:8px;border-radius:2px;flex:0 0 8px;margin-top:4px;}
.mps-layer .cap{flex:1;min-width:0;white-space:normal;overflow-wrap:anywhere;word-break:break-word;line-height:1.35;font-size:11px;}
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
    normalizePoseWidgets();
    bindEditsWidget();
    return !!(wSource && wEdits && wCw && wCh);
  };
  // 旧工作流缓存的姿势 widget 可能是 ""/null（widgets_values 错位或新控件缺省）：
  // 服务端会直接拒绝 pose_mode="" 与 apply_pose=""，这里在 configure/轮询时静默修回默认值。
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
  // 两个姿势参考框同样改为双列 DOM 面板，带原生 resize 手柄。
  try { mountPoseInputs(node); } catch (e) { console.warn("[MOSS Studio] 姿势参考面板挂载失败", e); }

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
  // 📥姿势：把 pose_in/pose_in_2 的图库姿势短语本地注入已拆解画布的角色 caption
  // （不调 LLM、免费秒完成；要 LLM 展开的丰富动作描述走 ⚡拆解 路径）
  const btnPose = mkBtn("📥姿势", () => applyPose(btnPose), "把姿势参考短语注入当前画布的角色 caption（本地写入不调 LLM）：选中角色→只注入它；未选中→双参考按左右分配、双人姿势给两人、单姿势给最突出角色。注入后自动过官方 JSON 校验并刷新画布，可 ↶撤销");
  // 🎯姿势①/②：从图库点选姿势（体系/分类/视角筛选 + 关键词搜索），写入 pose_in widget
  mkBtn("🎯姿势①", () => openPosePicker(1), "打开姿势库点选面板（需已安装 comfyui-pose-gallery）：按体系/分类/视角筛选或搜索，点视角按钮选用，写入 pose_in。也可在 widget 里手填或连线");
  mkBtn("🎯姿势②", () => openPosePicker(2), "同 🎯姿势①，写入 pose_in_2（双人分别指定姿势时给另一角色）");
  // 📐模板：内置自建版式（官方 draft schema），载入后走 adoptEdits 写回并保留撤销点
  mkBtn("📐模板", () => openTemplatePanel(), "打开内置版式模板库（自建示例版式）：竖版海报/方形社媒/电商主图/杂志封面/人物介绍卡/四格分镜。载入会写入编辑画布，可 ↶撤销");
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

  // 运行状态行（v1.7.1）：本节点执行状态轮询 + 语义事件（散文已收到/姿势已注入…），
  // 与下方画布状态行（布局/校验）分开显示
  const runStatus = document.createElement("div");
  runStatus.className = "mps-status mps-runstatus";
  runStatus.textContent = "○ 待命：本节点尚未执行";

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
    // 结束清理覆盖 pointerup/pointercancel/lostpointercapture：拖拽中切窗
    // 或指针捕获丢失时也能停掉 pointermove，避免 hover 继续改侧栏宽度（P2）。
    bindPointerDrag(vdiv, { onMove });
  });

  root.appendChild(toolbar);
  main.appendChild(canvasWrap);
  main.appendChild(vdiv);
  main.appendChild(side);
  root.appendChild(main);
  root.appendChild(runStatus);
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
    // 同侧栏分隔条：pointercancel/lostpointercapture 也要结束拖拽（P2）
    bindPointerDrag(sizer, { onMove });
  });
  root.appendChild(sizer);

  // 防止画布交互触发节点拖拽/画布平移
  root.addEventListener("pointerdown", (e) => e.stopPropagation());

  const M = (node._mps = {
    S: null, sel: null, undo: [], redo: [], inspPre: null, canvasH: 660,
    wSource, wEdits, wCw, wCh,
    els: { canvasWrap, canvas, underlay, undChk, capBox, msg, layers, insp, overlap, status, runStatus, snapChk, btnUndo, btnRedo, btnDel, pnlLayers, pnlInsp, pnlOv },
    builtSig: null, pendSig: null, timer: null, wt: null,
    editsDirty: false, editsCb: null, writingEdits: false,
    // source 归属（P0-1）：stateSrcSig=当前画布对应的 source 签名；split/pose/queueSrcSig=
    // 已提交但尚未回来的异步结果对应的 source 签名；lastDraft*=双通道去重。
    stateSrcSig: "", splitSrcSig: "", poseSrcSig: "", queueSrcSig: "",
    lastDraftText: "", lastDraftAt: 0, lastDraftPid: "",
    // F3/F4：提交互斥 / 最近一次拆解-注入的 prompt_id / 资源句柄
    submitting: false, lastSubmitId: "", poseLinkWarned: false,
    queueTimer: null, ro: null,
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
    M.ro = new ResizeObserver(applyResponsive);
    M.ro.observe(root);
  } catch (e) { /* 不支持 ResizeObserver 时维持固定布局 */ M.ro = null; }

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
    const nowSig = currentSig();
    // 画布来自旧 source 时不能把 builtSig 标成当前 source：否则 700ms 轮询会认为
    // “已构建”而不再从新 source 重建，旧画布就被永久当成新 source 的画布。
    // source 为空时 edits 是唯一内容来源，保持“已构建”，否则轮询会每 700ms 重复重建。
    const srcText = (wSource.value || "").trim();
    M.builtSig = (srcText && M.stateSrcSig && M.stateSrcSig !== currentSourceSig()) ? "" : nowSig;
    M.pendSig = nowSig;
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
          M.stateSrcSig = "";
          setSourceSig("");
        }
      } else {
        wEdits.value = serializeDraft(M.S);
        if (wEdits.inputEl) wEdits.inputEl.value = wEdits.value;
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

  function adoptEdits(showErrors = true, opts = {}) {
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
    M.stateSrcSig = currentSourceSig();
    M.sel = null;
    // 手改 edits 默认重开撤销栈；模板载入等调用方已自行压入撤销点时保留历史
    if (!opts.keepUndo) M.undo = [];
    M.redo = [];
    if (pre && !opts.preRecorded) pushUndo(pre);
    syncWidgets(); renderAll(); updateToolbar(); hideMsg();
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
    rebindWidgets();
    // 记录本次 Queue 提交对应的 source：全流程 Queue 也可能返回 mps_draft
    // （ai_draft 散文拆解），结果回来时据此判断 source 是否已变。
    M.queueSrcSig = currentSourceSig();
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
    // 记录这份画布是由哪份 source 生成的；写入 edits 时沿用该签名做归属（P0-1）。
    if ("srcSig" in opts) M.stateSrcSig = String(opts.srcSig || "");
    M.sel = null;
    // 📥姿势 注入保留既有撤销栈（F5）；拆解/换源/反推仍清空
    if (!opts.keepUndo) M.undo = [];
    M.redo = [];
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
    const { editsValid, candidates } = rebuildCandidates({ preferEdits, src, edits, srcSig, storedSig });
    if (preferEdits && !editsValid && edits) showMsg("source 已变化：本次以 source 为准，旧画布编辑已忽略", false);

    if (!candidates.length) {
      showMsg("粘贴源为空 —— 在 source 框里贴入上游产出的中文草稿 JSON", false);
      M.builtSig = M.pendSig = currentSig();
      renderAll();
      if (clearEditsOnEmpty) writeEdits({ clear: true });
      updateToolbar();
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
      wEdits.value = "";
      if (wEdits.inputEl) wEdits.inputEl.value = "";
      setSourceSig("");
    }
    showMsg(String((lastErr && lastErr.message) || lastErr || "无法解析"), true);
    M.builtSig = M.pendSig = currentSig();
    renderAll(); updateToolbar();
  }

  // AI 拆解结果（ui.mps_draft）写回 edits 后从这里重建画布。
  // srcSig：生成这份结果时使用的 source 签名（默认当前 source；迟到结果由调用方传入）。
  // opts.keepUndo：📥姿势 注入时保留既有撤销栈（F5）。
  function rebuildFromEdits(srcSig, opts = {}) {
    try {
      setStateFromText(wEdits.value, {
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
      // prose/draft 实际写 source：same-sig 保护必须按写入目标判断（F7）
      source: wSource ? wSource.value : "",
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
      // 解析失败时保留原画布与归属签名，别把旧画布盖成当前 source（P0-1 同类风险）
      if (!rebuildFromEdits(currentSourceSig())) return { ok: false, error: "parse" };
      // canvas_zh 也是当前 source 对应的画布：记录归属，换源后自动失效
      setSourceSig(currentSourceSig());
    } else {
      return { ok: false, error: "mode" };
    }
    if (!M.S) return { ok: false, error: "parse" };
    // 记录只存 owner/sig/mode + draftHash（不重复保存整份画布文本，控制工作流体积）
    if (wSig) setWidgetInput(wSig, reverseRecord(node.id, p.sig, p.mode, wEdits ? wEdits.value : ""));
    updateStatus();
    rememberRun(`📨 反推结果已写回（${p.mode === "prose" ? "散文→source" : p.mode === "draft" ? "草稿→source·已关自动拆解" : "画布→edits"}），再 Queue 一次完整链路生效`, "ok");
    return { ok: true };
  };

  // ---------- 渲染 ----------
  function layoutCanvas() {
    const isLayout = M.S && M.S.kind === "layout";
    canvas.style.display = isLayout ? "" : "none";
    capBox.style.display = M.S && M.S.kind === "caption" ? "" : "none";
    if (!isLayout) return;
    const Wd = M.S.d.width, Hd = M.S.d.height;
    // 显示高度跟随底部调整条（M.canvasH）与画布区实际可用高度，不再写死 540px；
    // 否则把高度条拖到 3000 只会在画布下面增加空白（P2）。
    const availW = Math.max(160, canvasWrap.clientWidth - 18);
    const availH = Math.max(120, (canvasWrap.clientHeight || Math.max(420, M.canvasH || 660)) - 18);
    const display = fitCanvasDisplay({ availW, availH, W: Wd, H: Hd, canvasH: M.canvasH });
    canvas.style.width = display.width + "px";
    canvas.style.height = display.height + "px";
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
      cap.textContent = `${i}·${it.label} ${it.caption || ""}`;
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
    // P2：重绘会清空 insp，浏览器移除聚焦元素时不一定派发 blur，bindField 的
    // blur 处理（writeEdits + pushUndo）会丢失，300ms 防抖内的编辑可能未落盘。
    // 选中其他框/图层前先主动 flush 当前字段，再重建检查器。
    flushFocusedField(typeof document !== "undefined" ? document.activeElement : null, insp);
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
      // 容量估算小字：输入时实时刷新（只提示，不限制编辑）
      const fitHint = document.createElement("div");
      fitHint.style.cssText = "font-size:11px;margin-top:2px;line-height:1.4;";
      const paintFitHint = () => {
        const fit = estimateTextFit(it);
        if (!fit) { fitHint.textContent = ""; fitHint.style.color = "#8a8a8a"; return; }
        const box = `${Math.round(fit.width)}×${Math.round(fit.height)}px`;
        const base = `约 ${Math.round(fit.fontSize)}px 字号 · ${fit.lines} 行 · 框 ${box}`;
        fitHint.style.color = fit.ok ? "#8a8a8a" : "#ffb0b0";
        fitHint.textContent = fit.ok ? `容量估算：${base}` : `⚠ 容量不足：${base}`;
      };
      paintFitHint();
      bindField(tx, (v) => { it.text = v; paintFitHint(); });
      insp.appendChild(tx);
      insp.appendChild(fitHint);
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
    // 姿势对调：恰好两个带 ；姿势： 标记的角色条目时出现（左右/上下/前后构图通用，
    // 只交换两个 caption 末尾的姿势段，不重拆、不调 LLM）
    if (it.label === "character" && poseMarkedItems(d).length === 2) {
      const sw = document.createElement("button");
      sw.className = "mps-mini"; sw.style.marginTop = "6px";
      sw.textContent = "🔄 对调两人姿势";
      sw.title = "交换两个角色 caption 末尾「" + POSE_MARKER + "…」段（拆解时由姿势参考生成）";
      sw.addEventListener("click", () => {
        pushUndo(snapshot());
        if (swapPoseSegments(d)) afterChange();
      });
      insp.appendChild(sw);
    }
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

  // 文字容量 / token 预算的软提示：只做 warning，不参与官方 validateDraft。
  function statusWarnings() {
    if (!M.S) return [];
    const warns = [];
    if (M.S.kind === "layout") {
      for (const l of lintTextItems(M.S.d)) warns.push(l.message);
    }
    const promptText = M.S.kind === "caption" ? M.S.caption : JSON.stringify(M.S.d);
    const tokens = estimatePromptTokens(promptText);
    if (tokens > TOKEN_WARN_LIMIT) {
      warns.push(`提示词粗估约 ${tokens} tokens（>${TOKEN_WARN_LIMIT}），可能超出预算`);
    }
    return warns;
  }

  function updateStatus() {
    const errs = validateJS(M.S);
    if (!M.S) { status.className = "mps-status"; status.textContent = "—"; return; }
    const head = M.S.kind === "caption"
      ? `📨 散文式提示词已收到（${M.S.caption.length} 字）· 目标 ${+wCw.value || 1024}×${+wCh.value || 1024}` + (wAi && wAi.value ? " · ⚡AI 拆解已启用：Queue 一次生成布局画布" : " · 点 ⚡拆解 生成画布")
      : `布局 ${M.S.d.width}×${M.S.d.height} · ${M.S.d.items.length} 项 · 选中 ${M.sel == null ? "—" : M.sel}`;
    const warns = statusWarnings();
    const warnLine = warns.length
      ? "\n⚠ " + warns[0] + (warns.length > 1 ? `（另有 ${warns.length - 1} 条提示）` : "")
      : "";
    if (errs.length) {
      status.className = "mps-status err";
      status.textContent = `${head}\n⚠ ${errs.length} 个问题：` + errs.slice(0, 3).join("；") + (errs.length > 3 ? " …" : "") + warnLine;
    } else {
      status.className = "mps-status ok";
      status.textContent = `${head} · ✓ 校验通过` + warnLine;
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
    const keys = Object.keys(o || {});
    // caption 快照只有 {"caption":...}；从布局撤销回散文（或反向）时按快照形态恢复，
    // 避免把 caption 快照误当 layout.d 导致 items 缺失。
    if (keys.length === 1 && keys[0] === "caption" && typeof o.caption === "string") {
      M.S = { kind: "caption", caption: o.caption };
    } else {
      M.S = { kind: "layout", d: o };
    }
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
      try {
        node._mpsLlmOptions?.hideWidgets();
        node._mpsLlmOptions?.refresh();
        node._mpsPoseInputs?.hideWidgets();
        node._mpsPoseInputs?.refresh();
      } catch (e) { /* noop */ }
      try { warnApiKeyIfSet(node, showKeyWarn); } catch (e) { /* noop */ }
      // 工作流切换/重新 configure 兜底：清掉可能卡住的提交状态（F2）
      resetSplitBtn();
      M.submitting = false;
      M.posePendingAt = 0;
      M.splitPendingAt = 0;
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
        const curSig = currentSourceSig();
        const pendingSig = M.poseBtn ? M.poseSrcSig : (M.splitBtn ? M.splitSrcSig : M.queueSrcSig);
        const uiSrcSig = Array.isArray(data.mps_src_sig) ? data.mps_src_sig.join("") : data.mps_src_sig;
        const decision = resultAttributionDecision({
          promptId: meta.promptId,
          lastSubmitId: M.lastSubmitId,
          uiSig: uiSrcSig,
          currentSig: curSig,
          pendingSig,
          duplicate: dupDraft,
        });
        // 本次结果是否为 📥姿势 注入：必须在 resetSplitBtn 清掉按钮前判断（F5）
        const isPoseResult = !!M.poseBtn
          || (!!M.posePendingAt && Date.now() - M.posePendingAt < 30000 && !M.splitPendingAt);
        // P1：结果到达（含重复/过期）就结束本次提交等待；否则 M.submitting 会一直
        // 否决 Queue/Ctrl+Enter 直到 180s 兜底。去重只决定是否再次写回画布。
        if (decision.resetPending) resetSplitBtn();
        if (decision.outcome === "duplicate") {
          // 双通道第二次投递：提交状态已复位，不重复写回
        } else if (decision.outcome === "foreign") {
          // prompt_id 不属于最近一次提交：丢弃内容，但保持等待本次提交的结果
          if (decision.notify) {
            rememberRun("⚠ 收到不属于最近一次提交的结果，已丢弃；仍在等待本次提交", "err");
          }
        } else if (decision.outcome === "stale") {
          M.posePendingAt = 0; M.splitPendingAt = 0; M.posePairHint = false;
          // 当前 source 的有效画布不能被丢掉：preferEdits=true 先尝试 owner 匹配的 edits；
          // 只有全部候选解析失败时才按原逻辑清理旧 edits（F8）
          try { rebuildFromWidgets({ preferEdits: true }); } catch (e) { /* noop */ }
          rememberRun("⚠ 拆解/注入结果对应旧 source，已丢弃旧画布；请对新 source 重新拆解", "err");
        } else {
          // P2：先解析/结构校验到临时对象，成功后才写 edits 并重建；失败保留
          // 旧画布与旧 edits（不再把坏 JSON 留在 widget 里）。
          const written = commitDraftWriteback({
            raw: md,
            getEdits: () => wEdits.value,
            setEdits: (v) => {
              wEdits.value = v;
              if (wEdits.inputEl) wEdits.inputEl.value = v;
            },
            rebuild: () => {
              // 📥姿势 结果支持 ↶撤销：先记录注入前画布；拆解/换源仍清空 undo（F5）
              if (isPoseResult && M.S) {
                try { pushUndo(snapshot()); } catch (e) { /* noop */ }
              }
              return rebuildFromEdits(curSig, { keepUndo: isPoseResult });
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
      } else if (M.submitting || M.splitBtn || M.poseBtn) {
        // 空草稿同样是“结果到达”：复位提交状态，避免永久否决 Queue（P1）
        resetSplitBtn();
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

  // ⚡拆解按钮：只提交编辑器单节点（OUTPUT_NODE），结果走事件总线写回
  // （顺带复位 📥姿势 按钮：两者共用 mps_draft 写回与错误/超时解除路径）
  function resetSplitBtn() {
    if (M.splitBtn) { M.splitBtn.disabled = false; M.splitBtn.textContent = "⚡拆解"; M.splitBtn = null; }
    if (M.poseBtn) { M.poseBtn.disabled = false; M.poseBtn.textContent = "📥姿势"; M.poseBtn = null; }
    // 提交已结束（结果到达/失败/超时/队列空）：清掉本次提交的 source 归属记录
    M.splitSrcSig = "";
    M.poseSrcSig = "";
    M.submitting = false;
    // 最近一次手工提交也随本次提交结束而失效：之后到达的完整 Queue 结果没有
    // 匹配对象，回退到 mps_src_sig/pending 判断，不会被 prompt_id 误杀（P1）。
    M.lastSubmitId = "";
  }

  // ---------- 版式模板库（自建示例数据） ----------
  // 模板数据在 editor/templates.js（纯逻辑、Node 测试覆盖）；这里只负责面板 DOM 与
  // 载入路径。载入走 adoptEdits：edits widget 保留 2 空格缩进的模板 JSON，
  // M.S / edits_source_sig / 撤销栈保持一致，source 为空时不会被 700ms 轮询覆盖。
  function closeTemplatePanel() {
    const old = root.querySelector(".mps-templatepicker");
    if (old) old.remove();
  }

  function openTemplatePanel() {
    closePosePicker();
    closeTemplatePanel();
    const panel = document.createElement("div");
    panel.className = "mps-templatepicker";
    panel.style.cssText = "border:1px solid #3a4a5a;border-radius:8px;background:#1a2026;margin:4px 0;padding:8px;";
    const head = document.createElement("div");
    head.style.cssText = "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px;";
    const title = document.createElement("span");
    title.style.cssText = "color:#9bb7d4;font-weight:600;";
    title.textContent = "📐 版式模板（自建示例）";
    const info = document.createElement("span");
    info.style.cssText = "color:#888;font-size:11px;flex:1;";
    info.textContent = "占位文案仅作示例，载入后按需修改；可 ↶撤销";
    const closeBtn = document.createElement("button");
    closeBtn.className = "mps-mini";
    closeBtn.textContent = "✕ 关闭";
    closeBtn.title = "关闭模板面板";
    closeBtn.addEventListener("click", closeTemplatePanel);
    head.append(title, info, closeBtn);
    panel.appendChild(head);
    const list = document.createElement("div");
    list.style.cssText = "display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:6px;";
    panel.appendChild(list);
    for (const t of listTemplates()) {
      const card = document.createElement("div");
      card.style.cssText = "border:1px solid #333;border-radius:6px;padding:6px;background:#20262b;display:flex;flex-direction:column;gap:4px;";
      const nameEl = document.createElement("div");
      nameEl.style.cssText = "color:#ddd;font-weight:600;font-size:12px;";
      nameEl.textContent = t.name;
      const metaEl = document.createElement("div");
      metaEl.style.cssText = "color:#8fb3d9;font-size:11px;";
      metaEl.textContent = `${t.draft.width}×${t.draft.height} · ${t.draft.items.length} 项 · ${t.draft.summary.canvas || "single"}`;
      const descEl = document.createElement("div");
      descEl.style.cssText = "color:#999;font-size:11px;line-height:1.4;";
      descEl.textContent = t.description;
      const useBtn = document.createElement("button");
      useBtn.className = "mps-mini";
      useBtn.textContent = "使用";
      useBtn.style.alignSelf = "flex-start";
      useBtn.title = "载入该版式到编辑画布（会先记录撤销点）";
      useBtn.addEventListener("click", () => applyTemplate(t));
      card.append(nameEl, metaEl, descEl, useBtn);
      list.appendChild(card);
    }
    root.insertBefore(panel, runStatus);
  }

  function applyTemplate(t) {
    if (!t || !t.draft) return;
    if (!rebindWidgets()) {
      status.textContent = "模板载入失败：Editor widget 不完整";
      status.className = "mps-status err";
      return;
    }
    const raw = JSON.stringify(t.draft, null, 2);
    const hadCanvas = !!M.S;
    const same = hadCanvas && M.S.kind === "layout"
      && JSON.stringify(M.S.d) === JSON.stringify(t.draft);
    if (same) {
      status.textContent = `已载入模板：${t.name}（与当前画布相同）`;
      status.className = "mps-status";
      closeTemplatePanel();
      return;
    }
    // 先记录载入前的画布；adoptEdits 会按 preRecorded 跳过重复压栈，保留完整撤销历史
    const prevEdits = wEdits ? wEdits.value : "";
    if (hadCanvas) pushUndo(snapshot());
    setWidgetInput(wEdits, raw);
    const ok = adoptEdits(true, { keepUndo: hadCanvas, preRecorded: true });
    if (!ok) {
      if (hadCanvas && M.undo.length) M.undo.pop();
      setWidgetInput(wEdits, prevEdits);
      status.textContent = "模板载入失败：模板数据无法解析";
      status.className = "mps-status err";
      return;
    }
    status.textContent = `已载入模板：${t.name}${hadCanvas ? "（可 ↶撤销）" : ""}`;
    status.className = "mps-status ok";
    updateToolbar();
    closeTemplatePanel();
  }

  // ---------- 姿势库点选面板（v1.7.1） ----------
  // 数据经 /moss_studio/pose_catalog 代理自已安装的 comfyui-pose-gallery（本插件不
  // 携带其索引数据）；未安装时给出指引。选中即把图库同格式描述串写入 pose_in(_2)。
  let poseCatalogCache = null;
  async function fetchPoseCatalog() {
    if (poseCatalogCache) return poseCatalogCache;
    const a = (typeof app !== "undefined" && app.api) || (window.app && window.app.api);
    const resp = await a.fetchApi("/moss_studio/pose_catalog");
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.message || ("HTTP " + resp.status));
    poseCatalogCache = data;
    return data;
  }

  function closePosePicker() {
    const old = root.querySelector(".mps-posepicker");
    if (old) old.remove();
  }

  // 断开姿势输入的连线（不同前端 API 兼容）；返回是否已断开。
  function disconnectPoseInput(name) {
    const idx = (node.inputs || []).findIndex((i) => i && i.name === name);
    if (idx < 0) return true;
    const inp = node.inputs[idx];
    if (!inp || inp.link == null) return true;
    const lid = inp.link;
    try {
      if (typeof node.disconnectInput === "function") {
        node.disconnectInput(idx);
      } else {
        const g = node.graph || app.graph;
        if (g && typeof g.removeLink === "function") g.removeLink(lid);
      }
    } catch (e) { /* 下面统一回退 */ }
    if (node.inputs[idx] && node.inputs[idx].link != null) {
      try { node.inputs[idx].link = null; } catch (e) { /* noop */ }
      const g = node.graph || app.graph;
      try { if (g && g.links && typeof g.links.delete === "function") g.links.delete(lid); } catch (e) { /* noop */ }
    }
    return !(node.inputs[idx] && node.inputs[idx].link != null);
  }

  // 可写的连线源：PrimitiveString 等常量节点（有字符串 widget）。点选时优先写回
  // 这类源节点并保留连线；Pose Gallery 等计算输出没有可写 widget，返回 null。
  function writablePoseSource(name) {
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
    if (!origin || !Array.isArray(origin.widgets)) return null;
    const outName = origin.outputs && origin.outputs[link.origin_slot] && origin.outputs[link.origin_slot].name;
    const nodeType = origin.type || (origin.constructor && origin.constructor.comfyClass) || origin.comfyClass || "";
    for (const nm of [outName, name, "value", "text", "description"]) {
      if (!nm) continue;
      const ww = (origin.widgets || []).find((x) => x && x.name === nm && typeof x.value === "string");
      if (ww && poseSourceWritable({ nodeType, widgetName: ww.name, outName })) {
        return { node: origin, widget: ww };
      }
    }
    // 不再使用“唯一字符串 widget”兜底：Pose Gallery 的 pose_id 不是可写输出，
    // 写进去会让上游 VALIDATE_INPUTS 抛“未知姿势 ID”，后续所有提交 400。
    return null;
  }

  function openPosePicker(slot) {
    closeTemplatePanel();
    closePosePicker();
    const panel = document.createElement("div");
    panel.className = "mps-posepicker";
    panel.style.cssText = "border:1px solid #3a5a3a;border-radius:8px;background:#1c221c;margin:4px 0;padding:8px;";
    const loading = document.createElement("div");
    loading.style.color = "#9c9";
    loading.textContent = "⏳ 正在读取姿势库索引…";
    panel.appendChild(loading);
    root.insertBefore(panel, runStatus);
    const setRun = (t, cls) => { runStatus.textContent = t; runStatus.className = "mps-status mps-runstatus " + (cls || ""); };
    fetchPoseCatalog().then((data) => {
      buildPosePickerUI(panel, data, slot, (item) => {
        const name = slot === 1 ? "pose_in" : "pose_in_2";
        const w = node.widgets.find((x) => x.name === name);
        if (!w) { setRun("❌ 未找到 pose_in widget", "err"); return; }
        const idx = (node.inputs || []).findIndex((i) => i && i.name === name);
        const hasLink = idx >= 0 && node.inputs[idx].link != null;
        const desc = toDescription(item);
        const src = hasLink ? writablePoseSource(name) : null;
        const target = poseWriteTarget({ linked: hasLink, sourceWritable: !!src });
        if (target === "source") {
          // 连线源是可写的常量节点（如 PrimitiveString）：写源节点、保留连线，
          // 并把节点标题标成“姿势人物①/②”，让画布上能直接看到当前点选结果。
          src.widget.value = desc;
          if (src.widget.inputEl) src.widget.inputEl.value = desc;
          const label = poseSourceLabel(slot);
          const oldTitle = String(src.node.title || "").trim();
          if (!oldTitle || /^(字符串|PrimitiveString)$/i.test(oldTitle) || /^姿势人物/.test(oldTitle)) {
            src.node.title = label;
          }
          try { src.node.setDirtyCanvas(true, true); } catch (e) { /* noop */ }
          try { (node.graph || app.graph).setDirtyCanvas(true, true); } catch (e) { /* noop */ }
          // Editor widget 同步一份镜像：以后断开连线仍保留最近点选值
          w.value = desc;
          if (w.inputEl) w.inputEl.value = desc;
          try { node._mpsPoseInputs?.refresh(); } catch (e) { /* noop */ }
          closePosePicker();
          setRun(`🎯 姿势${slot === 1 ? "①" : "②"}已选择：${item.system}·${item.name}（${item.view}）——已写入 ${label}（连线保留）`, "ok");
          return;
        }
        let disconnected = false;
        if (target === "prompt-disconnect") {
          // 连线源不可写（如 Pose Gallery 计算输出）：只能断开后用 widget 值
          const ok = window.confirm(`${name} 当前有连线，且连线源不是可写的字符串节点。是否断开连线并写入当前点选？`);
          if (!ok) {
            setRun(`已取消：${name} 仍有连线，点选值未写入`, "");
            return;
          }
          if (!disconnectPoseInput(name)) {
            setRun(`❌ 断开 ${name} 连线失败，请手动断开后重试`, "err");
            return;
          }
          disconnected = true;
        }
        w.value = desc;
        if (w.inputEl) w.inputEl.value = w.value;
        try { node._mpsPoseInputs?.refresh(); } catch (e) { /* noop */ }
        closePosePicker();
        setRun(`🎯 姿势${slot === 1 ? "①" : "②"}已选择：${item.system}·${item.name}（${item.view}）——已写入 ${name}${disconnected ? "（已断开覆盖用连线）" : ""}`, "ok");
      });
    }).catch((e) => {
      panel.textContent = "";
      const errEl = document.createElement("div");
      errEl.style.color = "#ff8b8b";
      errEl.style.whiteSpace = "pre-wrap";
      errEl.textContent = "❌ " + (e && e.message ? e.message : e);
      panel.appendChild(errEl);
      setRun("❌ 姿势库不可用：" + (e && e.message ? e.message : e), "err");
    });
  }

  function buildPosePickerUI(panel, data, slot, onPick) {
    const items = data.items || [];
    const apiRef = (typeof app !== "undefined" && app.api) || (window.app && window.app.api);
    panel.textContent = "";
    const head = document.createElement("div");
    head.style.cssText = "display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:6px;";
    const headInfo = document.createElement("span");
    headInfo.style.color = "#9c9";
    headInfo.textContent = `姿势${slot === 1 ? "①" : "②"} · 共 ${items.length} 条 / ${groupByPose(items).length} 组`;
    head.appendChild(headInfo);
    const mkSel = (label) => {
      const s = document.createElement("select");
      s.style.cssText = "background:#232323;color:#ccc;border:1px solid #3a3a3a;border-radius:4px;padding:2px 4px;max-width:150px;";
      s.appendChild(new Option(label + "·全部", ""));
      return s;
    };
    const fSys = mkSel("体系"), fCat = mkSel("分类"), fView = mkSel("视角");
    const facets = buildFacets(items);
    facets.systems.forEach((v) => fSys.appendChild(new Option(v, v)));
    facets.categories.forEach((v) => fCat.appendChild(new Option(v, v)));
    facets.views.forEach((v) => fView.appendChild(new Option(v, v)));
    const search = document.createElement("input");
    search.type = "text"; search.placeholder = "搜索：编号/姿势名关键词…";
    search.style.cssText = "background:#232323;color:#ccc;border:1px solid #3a3a3a;border-radius:4px;padding:2px 6px;width:160px;";
    const closeBtn = document.createElement("button");
    closeBtn.className = "mps-mini"; closeBtn.textContent = "✕ 关闭";
    closeBtn.addEventListener("click", closePosePicker);
    const refreshBtn = document.createElement("button");
    refreshBtn.className = "mps-mini"; refreshBtn.textContent = "↻ 刷新索引";
    refreshBtn.title = "重新读取姿势库索引（安装/更新 pose-gallery 后使用）";
    refreshBtn.addEventListener("click", () => {
      poseCatalogCache = null;
      openPosePicker(slot);
    });
    head.append(fSys, fCat, fView, search, refreshBtn, closeBtn);
    panel.appendChild(head);
    const list = document.createElement("div");
    list.style.cssText = "max-height:300px;overflow:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:6px;";
    panel.appendChild(list);
    const render = () => {
      const rows = filterPoses(items, { system: fSys.value, category: fCat.value, view: fView.value, query: search.value });
      const groups = groupByPose(rows);
      list.textContent = "";
      if (!groups.length) {
        const empty = document.createElement("div");
        empty.style.color = "#888";
        empty.textContent = "无匹配姿势，调整筛选";
        list.appendChild(empty);
        return;
      }
      for (const g of groups.slice(0, 120)) {
        const cell = document.createElement("div");
        cell.style.cssText = "border:1px solid #333;border-radius:6px;padding:6px;background:#202620;display:flex;gap:6px;";
        const thumb = document.createElement("img");
        thumb.loading = "lazy"; thumb.alt = "";
        thumb.style.cssText = "width:44px;height:58px;object-fit:cover;border-radius:4px;flex:none;background:#2a2a2a;";
        if (data.has_thumbs) {
          const first = g.views[0];
          thumb.src = poseThumbUrl(apiRef, first.id);
        }
        const info = document.createElement("div");
        info.style.cssText = "min-width:0;flex:1;";
        const nameEl = document.createElement("div");
        nameEl.style.color = "#ddd";
        nameEl.style.fontSize = "12px";
        nameEl.style.overflow = "hidden";
        nameEl.style.textOverflow = "ellipsis";
        nameEl.style.whiteSpace = "nowrap";
        nameEl.title = `${g.system} / ${g.category} / ${g.name}`;
        nameEl.textContent = g.name;
        const metaEl = document.createElement("div");
        metaEl.style.color = "#888";
        metaEl.style.fontSize = "11px";
        metaEl.textContent = g.system;
        metaEl.appendChild(document.createElement("br"));
        metaEl.appendChild(document.createTextNode(g.category));
        info.append(nameEl, metaEl);
        const views = document.createElement("div");
        views.style.cssText = "display:flex;flex-wrap:wrap;gap:3px;margin-top:3px;";
        for (const v of g.views) {
          const b = document.createElement("button");
          b.className = "mps-mini"; b.textContent = v.view; b.title = toDescription(v);
          b.style.padding = "1px 5px"; b.style.fontSize = "11px";
          b.addEventListener("click", (e) => { e.stopPropagation(); onPick(v); });
          views.appendChild(b);
        }
        info.appendChild(views);
        cell.append(thumb, info);
        list.appendChild(cell);
      }
      if (groups.length > 120) {
        const more = document.createElement("div");
        more.style.cssText = "grid-column:1/-1;color:#888;font-size:12px;";
        more.textContent = `… 仅显示前 120 组（共 ${groups.length} 组），请用筛选/搜索缩小范围`;
        list.appendChild(more);
      }
    };
    for (const s of [fSys, fCat, fView]) s.addEventListener("change", render);
    search.addEventListener("input", render);
    render();
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
  M.posePendingAt = 0;    // 📥注入提交时间戳：区分 mps_draft 是拆解还是注入产物
  M.splitPendingAt = 0;
  function rememberRun(t, cls) { M.lastRunMsg = t; setRun(t, cls); }

  function reportDraftArrived(draft) {
    const chars = (draft.items || []).filter((i) => i && i.label === "character");
    const marked = chars.filter((c) => (c.caption || "").includes(POSE_MARKER));
    const isInject = M.posePendingAt && Date.now() - M.posePendingAt < 30000 && !M.splitPendingAt;
    const pair = marked.length && marked.some((c) => (c.caption.split(POSE_MARKER)[1] || "").length > 0) &&
      chars.length >= 2 && (M.posePairHint || /双人/.test(node.widgets.find((x) => x.name === "pose_in")?.value || ""));
    if (isInject) {
      rememberRun(`✅ 姿势注入完成：${marked.length}/${chars.length} 角色已注入${pair ? "（双人姿势整体）" : ""} · 共 ${draft.items.length} 项 · 校验通过`, "ok");
    } else {
      rememberRun(`✅ AI 拆解完成：${draft.items.length} 项（角色 ${chars.length}）${marked.length ? ` · 姿势已注入 ${marked.length} 人${chars.length >= 2 ? "（当前双人）" : "（当前单人）"}` : " · 未注入姿势"} · 画布已刷新`, "ok");
    }
    M.posePendingAt = 0; M.splitPendingAt = 0; M.posePairHint = false;
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
    M.splitPendingAt = 0;
    M.posePairHint = /双人/.test(String((node.widgets.find((x) => x.name === "pose_in") || {}).value || ""));
    // 记录本次注入提交对应的 source：结果回来时若 source 已变则丢弃旧结果（P0-1）
    M.poseSrcSig = currentSourceSig();
    M.splitSrcSig = "";
    M.els.status.textContent = "⏳ 姿势注入中…（本地写入不调 LLM；"
      + (sel >= 0 ? "注入到选中角色" : "按左右/面积规则自动分配") + "）";
    M.els.status.className = "mps-status";
    try {
      if (typeof app.graphToPrompt !== "function") throw new Error("当前前端不支持 graphToPrompt");
      writeEdits(); // 冲洗画布最新内容到 edits widget，提交的就是眼前这份
      const full = await app.graphToPrompt();
      const all = (full && (full.output || full)) || {};
      const wAi = node.widgets.find((x) => x.name === "ai_draft");
      const inputs = buildSplitPayload(all, node.id, {
        edits: wEdits.value,           // 注入必须保留画布内容（拆解默认是清空重拆）
        ai_draft: !!(wAi && wAi.value),
        force_split: false,
        apply_pose: sel >= 0 ? sel : -2, // -2=自动分配（后端约定），>=0=选中角色下标
      });
      if (!inputs) throw new Error("编辑器被旁路或未注册（检查 Ctrl+B / 节点状态）");
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
      M.lastSubmitId = (j && j.prompt_id) || "";
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
      });
    } catch (e) {
      resetSplitBtn();
      fail("姿势注入提交失败: " + e.message);
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
  function poseLinkOverrideWarning() {
    if (M.poseLinkWarned) return "";
    const hit = [];
    for (const name of ["pose_in", "pose_in_2"]) {
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
    return `⚠ ${hit.join(" / ")} 已连线：本次 ⚡拆解 以连线值为准（widget 里的点选/手填值不会生效）`;
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
    M.submitting = true;
    M.splitBtn = btn;
    M.splitAt = Date.now();
    M.splitPendingAt = Date.now(); // 运行状态：本次 mps_draft 是拆解产物
    M.posePendingAt = 0;
    // 记录本次拆解提交对应的 source：LLM 执行期间用户改 source 时，旧结果晚到
    // 会被丢弃，避免旧画布被盖上当前 source 的签名（P0-1）
    M.splitSrcSig = currentSourceSig();
    M.poseSrcSig = "";
    const linkWarn = poseLinkOverrideWarning();
    M.els.status.textContent = "⏳ AI 拆解中…（LLM 生成布局草稿，通常 10~120 秒，完成后画布自动刷新）"
      + (linkWarn ? "\n" + linkWarn : "");
    M.els.status.className = "mps-status";
    try {
      // 以整个工作流当前状态取数，再裁剪出编辑器及其上游子图：
      // pose_in / pose_in_2 / underlay 等链接输入的上游（如 Pose Gallery）必须
      // 一并提交，否则拆解 LLM 收不到姿势描述。
      if (typeof app.graphToPrompt !== "function") throw new Error("当前前端不支持 graphToPrompt");
      const full = await app.graphToPrompt();
      const all = (full && (full.output || full)) || {};
      const inputs = buildSplitPayload(all, node.id);
      if (!inputs) throw new Error("编辑器被旁路或未注册，无法单独拆解（检查 Ctrl+B / 节点状态）");
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
      M.lastSubmitId = (j && j.prompt_id) || "";
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
      });
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
      rememberRun("❌ 本节点执行失败：" + (d.exception_message || "未知错误"), "err");
      M.posePendingAt = 0; M.splitPendingAt = 0;
    } catch (e) { /* noop */ }
  };

  // 队列跑完却没等到本节点的 executed（结果被缓存跳过 / 事件丢失）→ 解除按钮卡死
  const onApiStatus = (ev) => {
    try {
      if (!M.splitBtn && !M.poseBtn) return;
      const st = ev && ev.detail;
      const hasPending = !!(M.splitBtn || M.poseBtn);
      const elapsedMs = Date.now() - (M.splitAt || 0);
      // 显式解析两种 status 负载；queue_remaining=0 必须命中（F2）
      if (shouldResetOnQueueIdle({ queueRemaining: queueRemaining(st), hasPending, elapsedMs })) {
        resetSplitBtn();
        M.builtSig = M.pendSig = currentSig();
        M.els.status.textContent = "队列已空但未收到结果（可能被缓存跳过）：请再点一次";
        M.els.status.className = "mps-status err";
      }
    } catch (e) { /* noop */ }
  };

  // 执行被用户/系统中断：复位按钮与 M.submitting，避免永久否决 Queue（F2）
  const onApiInterrupted = () => {
    try {
      if (!M.splitBtn && !M.poseBtn && !M.submitting) return;
      resetSplitBtn();
      M.builtSig = M.pendSig = currentSig();
      M.els.status.textContent = "执行被中断：已复位拆解/注入按钮，可重新提交";
      M.els.status.className = "mps-status err";
      M.posePendingAt = 0;
      M.splitPendingAt = 0;
    } catch (e) { /* noop */ }
  };

  // LLM 配置同步反馈走状态条
  const cfgNotify = (t) => { M.els.status.textContent = t; M.els.status.className = "mps-status ok"; };

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
      if ((M.splitBtn || M.poseBtn) && ui.mps_draft === undefined && !(out && out.images && out.images.length)) {
        // 不是本次手工提交的执行（完整 Queue 透传等）：保持等待，别提前复位
        if (pidMismatch) return;
        resetSplitBtn();
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
    // 拆解/注入按钮卡死硬兜底：180s 仍无结果就解除（applyPose 共用 M.splitAt）
    const hasPending = !!(M.splitBtn || M.poseBtn);
    if (shouldResetOnTimeout({ hasPending, elapsedMs: Date.now() - (M.splitAt || 0) })) {
      resetSplitBtn();
      M.builtSig = M.pendSig = currentSig();
      M.els.status.textContent = "拆解/注入超时（180s 无结果）：已复位，请检查 LLM 配置后重试";
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
