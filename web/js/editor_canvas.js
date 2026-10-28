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

console.log("[MOSS Studio] Editor 画布扩展已加载");

const NODE_CLASS = "MOSSEditor";
const COLORS = { object: "#9d86f0", character: "#3ec27f", text: "#5aa9f5" };
const RATIOS = ["1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9", "2:1", "1:2"];
// 像素规模预设：对齐 MOSS 生态常用档（swan7 canvas 的 --mp 同款）
const MP_OPTS = [["1", "1M"], ["1.5", "1.5M"], ["2", "2M"], ["4", "4M"]];
const OCCL_FRONT = "，处于最前层，部分遮挡相邻元素";
const OCCL_BACK = "，被前方元素部分遮挡";
const VALID_LABELS = ["object", "character", "text"];
const VALID_CANVAS = ["single", "multi_panel", "collage"];
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

// ---------- 模型配置预设（编辑器画布工具栏与翻译器配置栏共享同一份库） ----------
const PRESET_KEYS = ["provider", "base_url", "model", "api_key", "temperature", "thinking", "timeout"];
const UD_PATH = "/userdata/moss_studio_llm_presets.json";
let PRESETS = null;
const presetSubs = new Set();

const getApi = () => (typeof app !== "undefined" && app.api) || (typeof window !== "undefined" && window.app && window.app.api);

function broadcastPresets() {
  const list = PRESETS || [];
  for (const fn of presetSubs) { try { fn(list); } catch (e) { /* noop */ } }
}
async function persistPresetsShared() {
  const body = JSON.stringify(PRESETS || []);
  try {
    const api = getApi();
    if (api && api.fetchApi) {
      const r = await api.fetchApi(UD_PATH + "?overwrite=true", { method: "POST", body, headers: { "Content-Type": "application/json" } });
      if (r.ok) return;
    }
  } catch (e) { /* 落到本地 */ }
  try { localStorage.setItem("mps_llm_presets", body); } catch (e) { /* noop */ }
}
async function ensurePresets() {
  if (PRESETS) return PRESETS;
  let list = null;
  try {
    const api = getApi();
    if (api && api.fetchApi) {
      const r = await api.fetchApi(UD_PATH);
      if (r.ok) list = await r.json();
    }
  } catch (e) { /* 落到本地 */ }
  if (!Array.isArray(list)) {
    try { list = JSON.parse(localStorage.getItem("mps_llm_presets") || "[]"); } catch (e) { list = []; }
  }
  PRESETS = (Array.isArray(list) ? list : []).filter(x => x && x.name);
  broadcastPresets();
  return PRESETS;
}
function fillPresetSelect(sel, chosen) {
  if (!sel) return;
  sel.innerHTML = "";
  sel.appendChild(new Option("配置…", ""));
  for (const p of PRESETS || []) sel.appendChild(new Option(p.name, p.name));
  if (chosen) sel.value = chosen;
}
function applyPresetTo(node, name, notify) {
  const p = (PRESETS || []).find(x => x.name === name);
  if (!p) return;
  for (const k of PRESET_KEYS) {
    const w = (node.widgets || []).find(x => x.name === k);
    if (!w || p[k] === undefined) continue;
    w.value = p[k];
    if (w.inputEl) {
      w.inputEl.value = p[k];
      w.inputEl.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }
  if (notify) notify(`已套用配置「${name}」`);
}
async function savePresetFrom(node, sel, notify) {
  const def = (sel && sel.value) || String(((node.widgets || []).find(x => x.name === "provider") || {}).value || "");
  const name = prompt("配置名称（如：deepseek线上 / 内网mimo）：", def);
  if (!name) return;
  const p = { name };
  for (const k of PRESET_KEYS) {
    const w = (node.widgets || []).find(x => x.name === k);
    if (w) p[k] = w.value;
  }
  await ensurePresets();
  const i = PRESETS.findIndex(x => x.name === name);
  if (i >= 0) PRESETS[i] = p; else PRESETS.push(p);
  await persistPresetsShared();
  broadcastPresets();
  if (sel) sel.value = name;
  if (notify) notify(`配置「${name}」已保存`);
}
function deletePresetFrom(sel, notify) {
  const name = (sel && sel.value) || prompt("要删除的配置名称：");
  if (!name) return;
  const i = (PRESETS || []).findIndex(x => x.name === name);
  if (i < 0) { alert(`没有找到配置「${name}」`); return; }
  if (!confirm(`删除配置「${name}」？`)) return;
  PRESETS.splice(i, 1);
  persistPresetsShared();
  broadcastPresets();
  if (notify) notify(`配置「${name}」已删除`);
}

function detectKind(o) {
  if (!o || typeof o !== "object" || Array.isArray(o)) return null;
  const ks = Object.keys(o);
  if (ks.length === 1 && ks[0] === "caption" && typeof o.caption === "string") return "caption";
  if ("items" in o || "summary" in o) {
    if (o.bbox_coord_space === "abs" && o.bbox_format === "xyxy") return "draft";
    if (o.bbox_coord_space === "abs_centralized" && o.bbox_format === "ycyx") return "compiled";
    if (o.bbox_coord_space === "abs_centralized" && o.bbox_format === "yxyx") return "compiled";
  }
  return null;
}

function reverseCompiled(o, W, H) {
  const items = o.items.map((it) => {
    const [y0, x0, y1, x1] = it.bbox;
    return {
      ...it,
      bbox: [Math.round(x0 + W / 2), Math.round(y0 + H / 2), Math.round(x1 + W / 2), Math.round(y1 + H / 2)],
    };
  });
  return { width: W, height: H, bbox_coord_space: "abs", bbox_format: "xyxy", summary: { ...o.summary }, items };
}

function isNum(v) { return typeof v === "number" && isFinite(v); }

function validateJS(S) {
  const errs = [];
  if (!S) return errs;
  if (S.kind === "caption") {
    if (!S.caption.trim()) errs.push("caption 为空");
    return errs;
  }
  const d = S.d;
  if (!Number.isInteger(d.width) || d.width % 16 !== 0 || d.width <= 0) errs.push(`width=${d.width} 不是 16 的正倍数`);
  if (!Number.isInteger(d.height) || d.height % 16 !== 0 || d.height <= 0) errs.push(`height=${d.height} 不是 16 的正倍数`);
  const s = d.summary || {};
  if (!s.caption || !s.caption.trim()) errs.push("summary.caption 为空");
  if (s.canvas && !VALID_CANVAS.includes(s.canvas)) errs.push(`summary.canvas=${s.canvas} 非法`);
  (d.items || []).forEach((it, i) => {
    const pre = `items[${i}]`;
    if (!VALID_LABELS.includes(it.label)) errs.push(`${pre}.label=${it.label} 非法`);
    if (!it.caption || !it.caption.trim()) errs.push(`${pre}.caption 为空`);
    const b = it.bbox;
    if (!Array.isArray(b) || b.length !== 4 || !b.every(isNum)) errs.push(`${pre}.bbox 不是四个数字`);
    else if (d.width > 0 && d.height > 0) {
      const [x0, y0, x1, y1] = b;
      if (!(x0 >= 0 && x0 < x1 && x1 <= d.width)) errs.push(`${pre}.bbox x 越界/翻转 ${JSON.stringify(b)}`);
      if (!(y0 >= 0 && y0 < y1 && y1 <= d.height)) errs.push(`${pre}.bbox y 越界/翻转 ${JSON.stringify(b)}`);
    }
    if (it.label === "text" && (!it.text || !it.text.trim())) errs.push(`${pre} 是 text 元素但 text 为空`);
    if (it.label !== "text" && "text" in it) errs.push(`${pre} 不是 text 元素不应有 text`);
  });
  if (!Array.isArray(d.items) || !d.items.length) errs.push("items 为空");
  return errs;
}

function init(node) {
  if (node._mpsInit) return;
  node._mpsInit = true;
  injectStyle();

  const W = (name) => node.widgets.find((w) => w.name === name);
  const wSource = W("source"), wEdits = W("edits"), wCw = W("canvas_width"), wCh = W("canvas_height"), wAi = W("ai_draft");
  if (!wSource || !wEdits || !wCw || !wCh) { console.warn("[MOSS Studio] Editor 节点 widget 不完整，画布未启用"); return; }
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
  selRatio.title = "换比例，保持当前像素面积（当前值显示在框上）";
  selRatio.addEventListener("change", () => { applyResize(selRatio.value, null); selRatio.value = ""; refreshSizeSelectors(); });
  toolbar.appendChild(selRatio);
  const selMP = document.createElement("select");
  selMP.className = "mps-size";
  selMP.appendChild(new Option("像素…", ""));
  MP_OPTS.forEach(([v, t]) => selMP.appendChild(new Option(t, v)));
  selMP.title = "换像素规模（百万像素），保持当前比例（当前值显示在框上）";
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
  // 模型配置预设：快速切换 mimo / deepseek 等多套连接配置（共享库，模块级实现）
  const selPreset = document.createElement("select");
  selPreset.appendChild(new Option("配置…", ""));
  selPreset.title = "已保存的模型配置，选择即套用到本节点";
  selPreset.addEventListener("change", () => { if (selPreset.value) applyPresetTo(node, selPreset.value, presetNotify); });
  toolbar.appendChild(selPreset);
  mkBtn("💾", () => savePresetFrom(node, selPreset, presetNotify), "把当前 provider/base_url/model/api_key/temperature/thinking/timeout 保存为预设");
  mkBtn("🗑", () => deletePresetFrom(selPreset, presetNotify), "删除当前选中的预设");
  const btnCopy = mkBtn("⧉复制", async () => {
    try { await navigator.clipboard.writeText(wEdits.value || ""); btnCopy.textContent = "已复制"; setTimeout(() => (btnCopy.textContent = "⧉复制"), 1200); } catch (e) { /* noop */ }
  }, "复制编辑后的草稿 JSON");
  // ⚡拆解：只提交编辑器单节点执行（OUTPUT_NODE），结果经事件总线写回画布
  const btnSplit = mkBtn("⚡拆解", () => runAiSplit(btnSplit), "把 source 散文拆解为布局草稿（只执行本节点，不跑整个工作流）");
  mkBtn("⟲重置", () => { if (confirm("重置为粘贴源并丢弃画布编辑？")) rebuildFromWidgets(); });

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
  });

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
  const snapshot = () => (M.S.kind === "caption" ? JSON.stringify({ caption: M.S.caption }) : JSON.stringify(M.S.d));
  const currentSig = () => `${wSource.value}\u0000${wCw.value},${wCh.value}`;

  function syncWidgets() {
    if (M.S && M.S.kind === "layout") {
      wCw.value = M.S.d.width;
      wCh.value = M.S.d.height;
    }
    M.builtSig = currentSig();
    M.pendSig = M.builtSig;
  }

  function writeEdits() {
    if (!M.S) { wEdits.value = ""; return; }
    wEdits.value = M.S.kind === "caption"
      ? JSON.stringify({ caption: M.S.caption }, null, 1)
      : JSON.stringify(M.S.d, null, 1);
  }
  const writeEditsSoon = () => {
    clearTimeout(M.wt);
    M.wt = setTimeout(writeEdits, 300);
  };

  function showMsg(text, isErr) {
    msg.className = "mps-msg" + (isErr ? " err" : "");
    msg.textContent = text;
    msg.style.display = "";
  }
  const hideMsg = () => { msg.style.display = "none"; };

  // 文本 → S 状态（共用：source 粘贴与 AI 拆解结果写回走同一条路）
  function applySourceText(src) {
    const s = (src || "").trim();
    if (!s) throw new Error("内容为空");
    let obj;
    try {
      obj = JSON.parse(s);
    } catch (e) {
      if (s && !s.startsWith("{") && !s.startsWith("[")) {
        obj = { caption: s }; // 纯文本散文 → caption 形态（与后端 sniff 行为一致）
      } else {
        throw new Error("JSON 解析失败: " + e.message);
      }
    }
    const kind = detectKind(obj);
    if (kind === "caption") {
      if (typeof obj.caption !== "string" || !obj.caption.trim()) throw new Error("caption 为空");
      return { kind: "caption", caption: obj.caption };
    }
    if (kind === "draft") return { kind: "layout", d: obj };
    if (kind === "compiled") return { kind: "layout", d: reverseCompiled(obj, +wCw.value || 1024, +wCh.value || 1024) };
    throw new Error('无法识别形态：需要 {"caption":...} 或带 summary/items 的布局提示词');
  }

  function setStateFromText(src) {
    M.S = applySourceText(src);
    M.sel = null; M.undo = []; M.redo = [];
    hideMsg();
    syncWidgets(); renderAll(); writeEdits(); updateToolbar();
  }

  function rebuildFromWidgets() {
    const src = (wSource.value || "").trim();
    M.S = null; M.sel = null; M.undo = []; M.redo = [];
    hideMsg();
    if (!src) {
      showMsg("粘贴源为空 —— 在 source 框里贴入上游产出的中文草稿 JSON", false);
      renderAll(); writeEdits(); updateToolbar();
      return;
    }
    try {
      setStateFromText(src);
    } catch (e) {
      showMsg(String(e.message || e), true);
      renderAll(); writeEdits(); updateToolbar();
    }
  }

  // AI 拆解结果（ui.mps_draft）写回 edits 后从这里重建画布
  function rebuildFromEdits() {
    try {
      setStateFromText(wEdits.value);
    } catch (e) {
      /* edits 内容异常时保持现状 */
    }
  }

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
      if (M.inspPre != null && snapshot() !== M.inspPre) { pushUndo(M.inspPre); updateToolbar(); }
      M.inspPre = null;
    });
  }
  const csvParse = (v) => v.split(",").map((x) => x.trim()).filter(Boolean);
  const csvShow = (v) => (v || []).join(", ");

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
    if ("meta" in s) {
      const row = document.createElement("div"); row.className = "mps-row";
      const l = document.createElement("label"); l.textContent = "meta";
      const inp = document.createElement("input"); inp.type = "text"; inp.value = csvShow(s.meta);
      bindField(inp, (v) => { s.meta = csvParse(v); });
      row.appendChild(l); row.appendChild(inp); insp.appendChild(row);
    }
    if ("canvas" in s) {
      const row = document.createElement("div"); row.className = "mps-row";
      const l = document.createElement("label"); l.textContent = "canvas";
      const selc = document.createElement("select");
      VALID_CANVAS.forEach((c) => selc.appendChild(new Option(c, c)));
      selc.value = s.canvas;
      selc.addEventListener("change", () => { pushUndo(snapshot()); s.canvas = selc.value; afterChange(); });
      row.appendChild(l); row.appendChild(selc); insp.appendChild(row);
    }

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
    for (const key of ["tags", "palette"]) {
      if (key in it) {
        const row = document.createElement("div"); row.className = "mps-row";
        const l = document.createElement("label"); l.textContent = key;
        const inp = document.createElement("input"); inp.type = "text"; inp.value = csvShow(it[key]);
        bindField(inp, (v) => { it[key] = csvParse(v); });
        row.appendChild(l); row.appendChild(inp); insp.appendChild(row);
      }
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

  function updateStatus() {
    const errs = validateJS(M.S);
    if (!M.S) { status.className = "mps-status"; status.textContent = "—"; return; }
    const head = M.S.kind === "caption"
      ? `caption · ${M.S.caption.length} 字` + (wAi && wAi.value ? ` · ⚡AI 拆解已启用（目标 ${+wCw.value || 1024}×${+wCh.value || 1024}）：Queue 一次生成布局画布` : "")
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
    selRatio.options[0].text = "比例 " + (RATIOS.includes(ratioStr) || ratioStr.length <= 7 ? ratioStr : "自定义");
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
  const snap16 = (v) => Math.max(16, Math.round(v / 16) * 16);
  function applyResize(ratioKey, mpVal) {
    if (!M.S || M.S.kind !== "layout") return;
    if (!ratioKey && !mpVal) return;
    const d = M.S.d;
    let ratio = d.width / d.height;
    if (ratioKey) {
      const [rw, rh] = ratioKey.split(":").map(Number);
      ratio = rw / rh;
    }
    const area = mpVal ? Number(mpVal) * 1000000 : d.width * d.height;
    const NW = snap16(Math.sqrt(area * ratio));
    const NH = snap16(Math.sqrt(area / ratio));
    if (NW === d.width && NH === d.height) return;
    if (!confirm(`画布调整为 ${NW}×${NH}（≈${(NW * NH / 1000000).toFixed(2)}M）并等比缩放所有框？`)) return;
    pushUndo(snapshot());
    const sx = NW / d.width, sy = NH / d.height;
    d.items.forEach((it) => {
      const x0 = Math.round(it.bbox[0] * sx), x1 = Math.round(it.bbox[2] * sx);
      const y0 = Math.round(it.bbox[1] * sy), y1 = Math.round(it.bbox[3] * sy);
      it.bbox = [x0, y0, x1 <= x0 ? x0 + 8 : x1, y1 <= y0 ? y0 + 8 : y1];
    });
    d.width = NW; d.height = NH;
    syncWidgets(); afterChange();
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
      it.bbox = [x0, y0, x1, y1];
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
  let wheelUndoPre = null, wheelIdle = null, wheelGeom = null;
  canvasWrap.addEventListener("wheel", (e) => {
    if (!M.S || M.S.kind !== "layout") return; // caption 模式下滚轮行为交还页面
    // 目标框：悬停在某个框上 → 该框；空白处 → 当前选中框
    const boxEl = e.target && e.target.closest ? e.target.closest(".mps-box") : null;
    const i = boxEl ? +boxEl.dataset.i : M.sel;
    if (i == null || !M.S.d.items[i]) return;
    e.preventDefault();
    e.stopPropagation(); // 别让 ComfyUI 画布跟着缩放
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
      if (wheelUndoPre != null) { commit(wheelUndoPre); wheelUndoPre = null; }
    }, 500);
  }, { passive: false });

  // ---------- 生命周期钩子（链式保留原行为） ----------
  // 工作流加载/恢复时 configure 会用保存的旧尺寸覆盖 init 设的高度，这里再兜一次底
  const prevConfigure = node.onConfigure;
  node.onConfigure = function (...a) {
    const r = prevConfigure ? prevConfigure.apply(this, a) : undefined;
    if (this.size[0] < 860 || this.size[1] < 900) {
      this.size = [Math.max(this.size[0], 860), Math.max(this.size[1], 900)];
    }
    sanitizeAgentMd(this);
    return r;
  };
  const prevResize = node.onResize;
  node.onResize = function (...a) {
    try { applyWidthPin(); layoutCanvas(); } catch (e) { /* noop */ }
    if (prevResize) return prevResize.apply(this, a);
  };
  const prevRemove = node.onRemoved;
  node.onRemoved = function (...a) {
    clearInterval(M.timer);
    clearTimeout(M.wt);
    presetSubs.delete(onPresetsChanged);
    if (M.apiHooked && M.apiRef && typeof M.apiRef.removeEventListener === "function") {
      M.apiRef.removeEventListener("executed", onApiExecuted);
      M.apiRef.removeEventListener("execution_error", onApiError);
      M.apiRef.removeEventListener("status", onApiStatus);
    }
    if (prevRemove) return prevRemove.apply(this, a);
  };
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
      underlay.src = `/view?filename=${encodeURIComponent(im.filename)}&subfolder=${encodeURIComponent(im.subfolder || "")}&type=${encodeURIComponent(im.type || "temp")}&r=${Date.now()}`;
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
    // 关键：必须清空 edits 再提交。后端 edits 非空即优先——带着上一次的旧草稿提交，
    // 服务端只会透传旧布局、不触发 AI 拆解，前端也就永远收不到 mps_draft。
    inputs.edits = "";
    try {
      const r = await fetch("/prompt", {
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

  // ---------- 模型配置预设：编辑器侧反馈走状态条，列表变化时刷新下拉 ----------
  const presetNotify = (t) => { M.els.status.textContent = t; M.els.status.className = "mps-status ok"; };
  const onPresetsChanged = () => fillPresetSelect(selPreset);
  presetSubs.add(onPresetsChanged);

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
    try { applyWidthPin(); } catch (e) { /* noop */ }
    const sig = currentSig();
    if (sig === M.pendSig) {
      if (sig !== M.builtSig && !M.splitBtn) { rebuildFromWidgets(); }
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

  rebuildFromWidgets();
  ensurePresets().then(() => fillPresetSelect(selPreset)).catch(() => {}); // 库已缓存时立即填充
}

// 翻译器节点：挂一条 LLM 配置栏（下拉套用 / 保存 / 删除，与编辑器共享预设库）
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
  const sel = document.createElement("select");
  sel.title = "已保存的模型配置，选择即套用到翻译节点";
  const msg = document.createElement("span");
  msg.className = "mps-cfgmsg";
  const notify = (t) => {
    msg.textContent = t;
    setTimeout(() => { if (msg.textContent === t) msg.textContent = ""; }, 2500);
  };
  const mkBtn = (txt, title, fn) => {
    const b = document.createElement("button");
    b.textContent = txt; b.title = title;
    b.addEventListener("click", fn);
    bar.appendChild(b);
  };
  sel.addEventListener("change", () => { if (sel.value) { applyPresetTo(node, sel.value, notify); } });
  bar.appendChild(sel);
  mkBtn("💾", "把当前 LLM 连接配置保存为预设（与编辑器共享）", () => savePresetFrom(node, sel, notify));
  mkBtn("🗑", "删除当前选中的预设", () => deletePresetFrom(sel, notify));
  bar.appendChild(msg);

  const domw = node.addDOMWidget("LLM配置", "mps_cfg", bar, { serialize: false, hideOnZoom: false });
  domw.computeSize = function (width) {
    return [Math.max(Number(width) || 300, 300), 44];
  };
  bar.addEventListener("pointerdown", (e) => e.stopPropagation());

  const onPresetsChanged = () => fillPresetSelect(sel);
  presetSubs.add(onPresetsChanged);
  ensurePresets().then(() => fillPresetSelect(sel)).catch(() => {});

  const prevRemove = node.onRemoved;
  node.onRemoved = function (...a) {
    presetSubs.delete(onPresetsChanged);
    if (prevRemove) return prevRemove.apply(this, a);
  };
}

app.registerExtension({
  name: "MOSSPromptStudio.EditorCanvas",
  async nodeCreated(node) {
    const cls = (node.constructor && node.constructor.comfyClass) || node.comfyClass;
    if (cls === "MOSSEditor") init(node);
    else if (cls === "MOSSTranslator") initTranslator(node);
  },
});
