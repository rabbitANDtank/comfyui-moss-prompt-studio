/**
 * Editor 画布扩展的 DOM 骨架（自 editor_canvas.js 机械抽出，行为不变）。
 *
 * buildDomShell：两行工具栏 / 主区-侧栏-状态行骨架 / 侧栏宽度分隔条 / 底部高度调整条。
 * mountDomShellChrome：窄节点响应式降级、宽度钉子、ResizeObserver、addDOMWidget 挂载。
 *
 * ctx = { node, M, fx, dom, get wEdits(){...}, ... }：M 为 node._mps 状态袋（同一对象），
 * fx 为跨块函数注册表（按钮回调引用编辑操作/渲染/提交流程等仍留在 init 闭包里的函数），
 * widget 通过 getter 注入以保持 rebindWidgets 换绑后的实时引用。
 */
import { app } from "/scripts/app.js";
import { syncLlmConfig } from "./llm_config.js";
import { bindPointerDrag } from "./pointer_drag.js";

// 官方口径：比例任意，只要求宽高为 16 的正整数倍；这里列常用比例，换算后自动对齐 16。
export const RATIOS = [
  "1:1",
  "5:4", "4:3", "3:2", "16:10", "16:9", "2:1", "21:9", "3:1",
  "4:5", "3:4", "2:3", "10:16", "9:16", "1:2", "1:3",
];
// 官方口径：支持 0.25M（512×512）到 4M（2K），推荐 1M / 1.5M / 2M。
export const MP_OPTS = [["0.25", "0.25M"], ["0.5", "0.5M"], ["1", "1M"], ["1.5", "1.5M"], ["2", "2M"], ["3", "3M"], ["4", "4M"]];

export function buildDomShell(ctx) {
  const { node, M, fx } = ctx;
  const root = document.createElement("div");
  root.className = "mps-root";

  const toolbar = document.createElement("div");
  toolbar.className = "mps-toolbar";
  // 两行工具栏：第一行 编辑工具 + 右端 ⇄同步/⧉复制；第二行 注入/模板 + 右端 ⚡拆解
  // （拆解正好在 同步/复制 正下方；两行各自内部换行，互不错位）
  const tbLine1 = document.createElement("div");
  tbLine1.className = "mps-tb-line";
  const tbLine2 = document.createElement("div");
  tbLine2.className = "mps-tb-line";
  toolbar.append(tbLine1, tbLine2);
  const mkBtn = (txt, fn, title, parent = tbLine1) => {
    const b = document.createElement("button");
    b.textContent = txt; if (title) b.title = title;
    b.addEventListener("click", fn);
    parent.appendChild(b);
    return b;
  };
  const btnUndo = mkBtn("↩撤销", () => fx.doUndo(), "Ctrl+Z");
  const btnRedo = mkBtn("↪重做", () => fx.doRedo(), "Ctrl+Y");
  const sep = (parent = tbLine1) => { const s = document.createElement("span"); s.textContent = "·"; s.style.opacity = "0.4"; parent.appendChild(s); };
  sep();
  mkBtn("+物体", () => fx.addItem("object"));
  mkBtn("+人物", () => fx.addItem("character"));
  mkBtn("+文字", () => fx.addItem("text"));
  sep();
  const btnDel = mkBtn("🗑删除", () => { if (M.sel != null) fx.delItem(M.sel); }, "Delete");
  const selRatio = document.createElement("select");
  selRatio.className = "mps-size";
  selRatio.appendChild(new Option("比例…", ""));
  RATIOS.forEach((r) => selRatio.appendChild(new Option(r, r)));
  selRatio.appendChild(new Option("自定义宽高…", "__custom__"));
  selRatio.title = "换比例，保持当前像素面积（当前值显示在框上）；官方支持任意比例，宽高自动取 16 倍数；也可选“自定义宽高…”直接输入任意 16 倍数尺寸";
  selRatio.addEventListener("change", () => {
    if (selRatio.value === "__custom__") fx.applyCustomSize();
    else fx.applyResize(selRatio.value, null);
    selRatio.value = "";
    fx.refreshSizeSelectors();
  });
  tbLine1.appendChild(selRatio);
  const selMP = document.createElement("select");
  selMP.className = "mps-size";
  selMP.appendChild(new Option("像素…", ""));
  MP_OPTS.forEach(([v, t]) => selMP.appendChild(new Option(t, v)));
  selMP.title = "换像素规模（百万像素），保持当前比例（当前值显示在框上）；官方范围 0.25M–4M，推荐 1M/1.5M/2M";
  selMP.addEventListener("change", () => { fx.applyResize(null, selMP.value); selMP.value = ""; fx.refreshSizeSelectors(); });
  tbLine1.appendChild(selMP);
  const snapChk = document.createElement("input");
  snapChk.type = "checkbox"; snapChk.checked = true; snapChk.id = "mps-snap-" + node.id;
  const snapLbl = document.createElement("label");
  snapLbl.className = "mps-chk"; snapLbl.htmlFor = snapChk.id; snapLbl.textContent = "16吸附";
  snapLbl.prepend(snapChk);
  tbLine1.appendChild(snapLbl);
  const undChk = document.createElement("input");
  undChk.type = "checkbox"; undChk.checked = false;
  const undLbl = document.createElement("label");
  undLbl.className = "mps-chk"; undLbl.textContent = "底图"; undLbl.prepend(undChk);
  tbLine1.appendChild(undLbl);
  // LLM 配置同步：把当前节点的连接配置复制到另外两个 MOSS LLM 节点（不含 api_key）
  const btnSync = mkBtn("⇄同步", () => {
    const n = syncLlmConfig(node, app.graph, app);
    fx.cfgNotify(`已同步 LLM 连接配置（不含 api_key）到另外 ${n} 个节点`);
  }, "把当前 provider/base_url/model/temperature/thinking/timeout 复制到另外两个 MOSS 节点；api_key 不复制（会随工作流明文保存，建议放 config.json）");
  const btnCopy = mkBtn("⧉复制", async () => {
    try { await navigator.clipboard.writeText(ctx.wEdits.value || ""); btnCopy.textContent = "已复制"; setTimeout(() => (btnCopy.textContent = "⧉复制"), 1200); } catch (e) { /* noop */ }
  }, "复制编辑后的草稿 JSON");
  // ⚡拆解：只提交编辑器单节点执行（OUTPUT_NODE），结果经事件总线写回画布。
  // 最常用入口：主色（降饱和蓝）+加大，排在第二行最右、⇄同步/⧉复制 正下方。
  const btnSplit = mkBtn("⚡拆解", () => fx.runAiSplit(btnSplit), "把 source 散文拆解为布局草稿（只执行本节点，不跑整个工作流）");
  btnSplit.classList.add("mps-primary");
  // 第一行右端：⇄同步 / ⧉复制（与 ↩撤销/+物体/+人物 那一排横向对齐）
  const tbUtil = document.createElement("div");
  tbUtil.className = "mps-tb-end";
  tbUtil.append(btnSync, btnCopy);
  tbLine1.appendChild(tbUtil);
  // 📥姿势：把 pose_in/pose_in_2 的图库姿势短语本地注入已拆解画布的角色 caption
  // （不调 LLM、免费秒完成；要 LLM 展开的丰富动作描述走 ⚡拆解 路径）
  const btnPose = mkBtn("📥姿势", () => fx.applyPose(btnPose), "把姿势参考短语注入当前画布的角色 caption（本地写入不调 LLM）：选中角色→只注入它；未选中→双参考按左右分配、双人姿势给两人、单姿势给最突出角色。注入后自动过官方 JSON 校验并刷新画布，可 ↶撤销", tbLine2);
  // 📥服装：把 costume_in/costume_in_2 的服装短语本地注入角色 caption（不调 LLM）
  const btnCostume = mkBtn("📥服装", () => fx.applyCostume(btnCostume), "把服装参考短语注入当前画布的角色 caption（本地写入不调 LLM）：选中角色→只注入它；未选中→双参考按左右分配、单参考给最突出角色。注入后自动过官方 JSON 校验并刷新画布，可 ↶撤销", tbLine2);
  // 🎯姿势①/②：从图库点选姿势（体系/分类/视角筛选 + 关键词搜索），写入 pose_in widget
  mkBtn("🎯姿势①", () => fx.openPosePicker(1), "打开姿势库点选面板（需已安装 comfyui-pose-gallery）：按体系/分类/视角筛选或搜索，点视角按钮选用，写入 pose_in。也可在 widget 里手填或连线", tbLine2);
  mkBtn("🎯姿势②", () => fx.openPosePicker(2), "同 🎯姿势①，写入 pose_in_2（双人分别指定姿势时给另一角色）", tbLine2);
  // 📐模板：内置自建版式（官方 draft schema），载入后走 adoptEdits 写回并保留撤销点
  mkBtn("📐模板", () => fx.openTemplatePanel(), "打开内置版式模板库（自建示例版式）：竖版海报/方形社媒/电商主图/杂志封面/人物介绍卡/四格分镜。载入会写入编辑画布，可 ↶撤销", tbLine2);
  mkBtn("⟲重置", () => { if (confirm("重置为粘贴源并丢弃画布编辑？")) fx.rebuildFromWidgets({ preferEdits: false, clearEditsOnEmpty: true }); },
    "重置为 source 粘贴源：丢弃当前画布编辑并清空 edits（会二次确认；不能用 ↶撤销 恢复）", tbLine2);
  // 第二行右端：⚡拆解（在 ⇄同步/⧉复制 正下方、右对齐）
  const tbSplit = document.createElement("div");
  tbSplit.className = "mps-tb-end";
  tbSplit.appendChild(btnSplit);
  tbLine2.appendChild(tbSplit);

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
      fx.layoutCanvas();
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
      fx.layoutCanvas();
    };
    // 同侧栏分隔条：pointercancel/lostpointercapture 也要结束拖拽（P2）
    bindPointerDrag(sizer, { onMove });
  });
  root.appendChild(sizer);

  // 防止画布交互触发节点拖拽/画布平移
  root.addEventListener("pointerdown", (e) => e.stopPropagation());

  return {
    root, toolbar, tbLine1, tbLine2, btnUndo, btnRedo, btnDel, selRatio, selMP,
    snapChk, undChk, btnSync, btnCopy, btnSplit, btnPose, btnCostume, tbUtil, tbSplit,
    main, canvasWrap, canvas, underlay, capBox, msg, side, pnlLayers, layers, pnlInsp,
    insp, pnlOv, overlap, runStatus, status, vdiv, sizer,
  };
}

export function mountDomShellChrome(ctx) {
  const { node, M, fx } = ctx;
  const { root, undChk, underlay } = ctx.dom;

  // 响应式降级：节点被拖窄（或加载了窄节点）时，先收窄侧栏、再改为上下堆叠，
  // 保证画布/文本区始终有可读宽度，不出现"文字逐字竖排"的挤压变形。
  const applyResponsive = () => {
    const w = root.clientWidth;
    if (!w) return;
    root.classList.toggle("mps-compact", w >= 520 && w < 720);
    root.classList.toggle("mps-narrow", w < 520);
    applyWidthPin();
    try { fx.layoutCanvas(); } catch (e) { /* noop */ }
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

  return { applyWidthPin, applyResponsive, domw };
}
