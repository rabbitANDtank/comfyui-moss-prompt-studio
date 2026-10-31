/**
 * Editor 画布的画布区渲染（自 editor_canvas.js 机械抽出，行为不变）。
 *
 * layoutCanvas / renderBoxes / updateBoxStyle / renderLayers / bindField，
 * 以及 palette 色块编辑器（buildPaletteEditor）与可选数组写回辅助 setOptArr。
 *
 * ctx = { node, M, dom, fx, ... }：M 为 node._mps 状态袋（同一对象）；
 * fx 为跨块函数注册表（手势/图元操作/检查器/写回等仍留在 init 闭包里的函数）。
 */
import { fitCanvasDisplay } from "./model.js";

const COLORS = { object: "#9d86f0", character: "#3ec27f", text: "#5aa9f5" };

export function mountCanvasRender(ctx) {
  const { M, fx } = ctx;
  const { canvas, canvasWrap, capBox, insp, layers } = ctx.dom;

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
        hd.addEventListener("pointerdown", (e) => fx.startGesture(e, i, h));
        b.appendChild(hd);
      }
      b.addEventListener("pointerdown", (e) => fx.startGesture(e, i, null));
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
      mk("↑", () => fx.layerMove(i, -1), "上移一层");
      mk("↓", () => fx.layerMove(i, +1), "下移一层");
      mk("✕", () => fx.delItem(i), "删除");
      row.addEventListener("click", () => fx.select(i));
      layers.appendChild(row);
    });
  }

  function bindField(el, setter) {
    el.addEventListener("focus", () => { M.inspPre = fx.snapshot(); });
    el.addEventListener("input", () => {
      setter(el.value);
      fx.updateStatus();
      fx.writeEditsSoon();
    });
    el.addEventListener("blur", () => {
      // 点 Queue 会先触发 blur：立刻写回，不能等 300ms 防抖
      fx.writeEdits();
      if (M.inspPre != null && fx.snapshot() !== M.inspPre) { fx.pushUndo(M.inspPre); fx.updateToolbar(); }
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
        inp.addEventListener("focus", () => { M.inspPre = fx.snapshot(); });
        inp.addEventListener("input", () => {
          const a = getArr(); a[i] = inp.value; setArr(a);
          fx.afterLightChange();
        });
        inp.addEventListener("blur", () => {
          fx.writeEdits();
          if (M.inspPre != null && fx.snapshot() !== M.inspPre) { fx.pushUndo(M.inspPre); fx.updateToolbar(); }
          M.inspPre = null;
        });
        const x = document.createElement("span");
        x.className = "mps-swx";
        x.textContent = "×";
        x.title = "删除这个颜色";
        x.addEventListener("click", (e) => {
          e.stopPropagation();
          fx.pushUndo(fx.snapshot());
          const a = getArr(); a.splice(i, 1); setArr(a);
          fx.afterChange();
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
      fx.pushUndo(fx.snapshot());
      const a = getArr();
      a.push(a[a.length - 1] || "#888888");
      setArr(a);
      fx.afterChange();
    });
    host.appendChild(add);
    draw();
    return draw;
  }

  // 写回辅助：空数组则删除该可选字段（官方 schema 里 meta/palette/tags 均可省略）
  const setOptArr = (obj, key) => (a) => { if (a.length) obj[key] = a; else delete obj[key]; };

  return { layoutCanvas, renderBoxes, updateBoxStyle, renderLayers, bindField, buildPaletteEditor, setOptArr };
}
