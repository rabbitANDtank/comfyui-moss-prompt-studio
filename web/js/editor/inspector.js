/**
 * Editor 画布的检查器与状态行（自 editor_canvas.js 机械抽出，行为不变）。
 *
 * renderInspector（summary/meta/palette/选中元素面板）、renderOverlap（重叠检测）、
 * statusWarnings/updateStatus（校验与软提示）、refreshSizeSelectors（比例/像素回显）、
 * updateToolbar，以及全局汇聚点 renderAll 与 caption 文本框绑定。
 *
 * ctx = { node, M, dom, fx, get wCw(){...}, get wCh(){...}, get wAi(){...} }：
 * M 为 node._mps 状态袋（同一对象）；fx 为跨块函数注册表（画布渲染/图元操作/
 * 写回等仍留在 init 闭包或 canvas_render/items 模块中的函数）；widget 通过 getter
 * 注入以保持 rebindWidgets 换绑后的实时引用。
 */
import { VALID_LABELS, VALID_CANVAS, csvParse, csvShow, validateDraft as validateJS } from "./model.js";
import { estimateTextFit, estimatePromptTokens, lintTextItems, TOKEN_WARN_LIMIT } from "./text_fit.js";
import { flushFocusedField } from "./focus_guard.js";
import { POSE_MARKER, COSTUME_MARKER, poseMarkedItems, costumeMarkedItems,
  swapPoseSegments, swapCostumeSegments } from "./pose.js";
import { RATIOS, MP_OPTS } from "./dom_shell.js";

export function mountInspector(ctx) {
  const { M, fx } = ctx;
  const { insp, overlap, status, capBox, selRatio, selMP } = ctx.dom;

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
    fx.bindField(sc, (v) => { s.caption = v; });
    insp.appendChild(sc);
    // meta：体裁标签（字符串数组，自由填写；不存在也能直接添加）
    const row = document.createElement("div"); row.className = "mps-row";
    const l = document.createElement("label"); l.textContent = "meta 体裁";
    const inp = document.createElement("input"); inp.type = "text";
    inp.value = csvShow(s.meta);
    inp.placeholder = "product poster, studio photography";
    inp.title = "整图体裁标签，逗号分隔（原文会作为独立 JSON 字段进入提示词）";
    fx.bindField(inp, (v) => fx.setOptArr(s, "meta")(csvParse(v)));
    row.appendChild(l); row.appendChild(inp); insp.appendChild(row);
    if ("canvas" in s) {
      const row2 = document.createElement("div"); row2.className = "mps-row";
      const l2 = document.createElement("label"); l2.textContent = "canvas";
      const selc = document.createElement("select");
      VALID_CANVAS.forEach((c) => selc.appendChild(new Option(c, c)));
      selc.value = s.canvas;
      selc.addEventListener("change", () => { fx.pushUndo(fx.snapshot()); s.canvas = selc.value; fx.afterChange(); });
      row2.appendChild(l2); row2.appendChild(selc); insp.appendChild(row2);
    }
    // palette：整图主配色（hex 色块编辑，不拼进 caption，作为独立字段进提示词）
    insp.appendChild(h5("palette 主配色（hex）"));
    fx.buildPaletteEditor(insp, () => s.palette || [], fx.setOptArr(s, "palette"));

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
      fx.pushUndo(fx.snapshot());
      it.label = selL.value;
      if (it.label === "text" && !("text" in it)) it.text = "";
      if (it.label !== "text") delete it.text;
      fx.afterChange();
    });
    rowL.appendChild(lL); rowL.appendChild(selL); insp.appendChild(rowL);

    const cap = document.createElement("textarea");
    cap.className = "mps-textarea"; cap.rows = 3; cap.value = it.caption || ""; cap.dataset.f = "caption";
    fx.bindField(cap, (v) => { it.caption = v; });
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
      fx.bindField(tx, (v) => { it.text = v; paintFitHint(); });
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
    fx.bindField(inpT, (v) => fx.setOptArr(it, "tags")(csvParse(v)));
    rowT.appendChild(inpT); insp.appendChild(rowT);
    // palette：该元素自身配色（hex 色块编辑）
    insp.appendChild(h5("palette 元素配色（hex）"));
    fx.buildPaletteEditor(insp, () => it.palette || [], fx.setOptArr(it, "palette"));
    // 姿势对调：恰好两个带 ；姿势： 标记的角色条目时出现（左右/上下/前后构图通用，
    // 只交换两个 caption 末尾的姿势段，不重拆、不调 LLM）
    if (it.label === "character" && poseMarkedItems(d).length === 2) {
      const sw = document.createElement("button");
      sw.className = "mps-mini"; sw.style.marginTop = "6px";
      sw.textContent = "🔄 对调两人姿势";
      sw.title = "交换两个角色 caption 末尾「" + POSE_MARKER + "…」段（拆解时由姿势参考生成）；服装段保持不动";
      sw.addEventListener("click", () => {
        fx.pushUndo(fx.snapshot());
        if (swapPoseSegments(d)) fx.afterChange();
      });
      insp.appendChild(sw);
    }
    if (it.label === "character" && costumeMarkedItems(d).length === 2) {
      const sw = document.createElement("button");
      sw.className = "mps-mini"; sw.style.marginTop = "6px";
      sw.textContent = "🔄 对调两人服装";
      sw.title = "交换两个角色 caption 末尾「" + COSTUME_MARKER + "…」段（拆解/📥服装时由服装参考生成）；姿势段保持不动";
      sw.addEventListener("click", () => {
        fx.pushUndo(fx.snapshot());
        if (swapCostumeSegments(d)) fx.afterChange();
      });
      insp.appendChild(sw);
    }
    const del = document.createElement("button");
    del.className = "mps-mini"; del.style.marginTop = "6px"; del.textContent = "🗑 删除该元素";
    del.addEventListener("click", () => fx.delItem(M.sel));
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
      mk(`${i} 在前`, () => fx.occlude(i, j));
      mk(`${j} 在前`, () => fx.occlude(j, i));
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
      ? `📨 散文式提示词已收到（${M.S.caption.length} 字）· 目标 ${+ctx.wCw.value || 1024}×${+ctx.wCh.value || 1024}` + (ctx.wAi && ctx.wAi.value ? " · ⚡AI 拆解已启用：Queue 一次生成布局画布" : " · 点 ⚡拆解 生成画布")
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
    const W = d ? +d.width : +ctx.wCw.value, H = d ? +d.height : +ctx.wCh.value;
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
    fx.layoutCanvas();
    fx.renderBoxes();
    fx.renderLayers();
    renderInspector();
    renderOverlap();
    updateStatus();
    refreshSizeSelectors();
    updateToolbar();
    if (M.S && M.S.kind === "caption" && document.activeElement !== capBox) capBox.value = M.S.caption;
  }

  // caption 模式文本框绑定（只绑一次）
  capBox.addEventListener("focus", () => { M.inspPre = M.S ? fx.snapshot() : null; });
  capBox.addEventListener("input", () => { if (M.S && M.S.kind === "caption") { M.S.caption = capBox.value; updateStatus(); fx.writeEditsSoon(); } });
  capBox.addEventListener("blur", () => {
    fx.writeEdits();
    if (M.inspPre != null && M.S && fx.snapshot() !== M.inspPre) { fx.pushUndo(M.inspPre); updateToolbar(); }
    M.inspPre = null;
  });

  return { renderInspector, renderOverlap, statusWarnings, updateStatus, refreshSizeSelectors, updateToolbar, renderAll };
}
