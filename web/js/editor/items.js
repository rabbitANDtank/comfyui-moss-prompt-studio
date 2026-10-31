/**
 * Editor 画布的编辑操作（自 editor_canvas.js 机械抽出，行为不变）。
 *
 * 撤销重做（pushUndo/commit/restore/doUndo/doRedo）、图元操作（select/addItem/
 * delItem/layerMove）、遮挡措辞（occlude）与尺寸换算（resizeLayoutTo/
 * applyCustomSize/applyResize），以及落地辅助 afterChange/afterLightChange。
 *
 * ctx = { node, M, dom, fx, get wCw(){...}, get wCh(){...} }：M 为 node._mps 状态袋
 * （同一对象）；fx 为跨块函数注册表（渲染/检查器/写回等仍留在 init 闭包的函数）；
 * widget 通过 getter 注入以保持 rebindWidgets 换绑后的实时引用。
 */
import { clampDim, clampArea, clampBox, OCCL_FRONT, OCCL_BACK } from "./model.js";

export function mountItems(ctx) {
  const { M, fx } = ctx;
  const { canvas, insp } = ctx.dom;

  function pushUndo(pre) {
    M.undo.push(pre);
    if (M.undo.length > 100) M.undo.shift();
    M.redo = [];
  }
  function commit(pre) { pushUndo(pre); afterChange(); }
  function afterChange() { fx.renderAll(); fx.writeEdits(); }
  function afterLightChange() { fx.updateStatus(); fx.writeEditsSoon(); }

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
    fx.syncWidgets(); fx.renderAll(); fx.writeEdits();
  }
  function doUndo() { if (M.undo.length) { M.redo.push(fx.snapshot()); restore(M.undo.pop()); } }
  function doRedo() { if (M.redo.length) { M.undo.push(fx.snapshot()); restore(M.redo.pop()); } }

  function select(i, light) {
    M.sel = i;
    if (light) {
      canvas.querySelectorAll(".mps-box").forEach((b) => b.classList.toggle("sel", +b.dataset.i === M.sel));
      fx.renderLayers(); fx.renderInspector();
    } else {
      fx.renderBoxes(); fx.renderLayers(); fx.renderInspector();
    }
    fx.updateToolbar();
  }

  function addItem(label) {
    if (!M.S || M.S.kind !== "layout") return;
    pushUndo(fx.snapshot());
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
    pushUndo(fx.snapshot());
    M.S.d.items.splice(i, 1);
    M.sel = null;
    afterChange();
  }

  function layerMove(i, delta) {
    if (!M.S || M.S.kind !== "layout") return;
    const items = M.S.d.items;
    const j = i + delta;
    if (j < 0 || j >= items.length) return;
    pushUndo(fx.snapshot());
    const t = items[i]; items[i] = items[j]; items[j] = t;
    M.sel = j;
    afterChange();
  }

  function occlude(front, back) {
    if (!M.S || M.S.kind !== "layout") return;
    const items = M.S.d.items;
    if (items[front] == null || items[back] == null) return;
    pushUndo(fx.snapshot());
    items[front].caption = (items[front].caption || "") + OCCL_FRONT;
    items[back].caption = (items[back].caption || "") + OCCL_BACK;
    afterChange();
  }

  // 比例/像素规模组合换算：只选比例→保持当前面积；只选像素→保持当前比例；16 倍数取整
  function resizeLayoutTo(NW, NH) {
    const d = M.S.d;
    NW = clampDim(NW); NH = clampDim(NH);
    pushUndo(fx.snapshot());
    const sx = NW / d.width, sy = NH / d.height;
    d.items.forEach((it) => {
      const x0 = Math.round(it.bbox[0] * sx), x1 = Math.round(it.bbox[2] * sx);
      const y0 = Math.round(it.bbox[1] * sy), y1 = Math.round(it.bbox[3] * sy);
      it.bbox = clampBox([x0, y0, x1, y1], NW, NH);
    });
    d.width = NW; d.height = NH;
    fx.syncWidgets(); afterChange();
  }

  // 自定义宽高：官方允许任意尺寸，只要宽高是 16 的正整数倍。
  function applyCustomSize() {
    if (!M.S) return;
    const isLayout = M.S.kind === "layout";
    const d = isLayout ? M.S.d : null;
    const curW = d ? d.width : (+ctx.wCw.value || 1024);
    const curH = d ? d.height : (+ctx.wCh.value || 1024);
    const raw = prompt("输入宽×高（16 的倍数，64–15360），如 1536x1024：", `${curW}x${curH}`);
    if (raw == null || !String(raw).trim()) return;
    const m = String(raw).trim().match(/^(\d+)\s*[x×*,\s]\s*(\d+)$/i);
    if (!m) { fx.showMsg("尺寸格式应为 宽x高，如 1536x1024", true); return; }
    const NW = Number(m[1]), NH = Number(m[2]);
    if (!Number.isInteger(NW) || !Number.isInteger(NH) || NW < 64 || NH < 64
        || NW > 15360 || NH > 15360 || NW % 16 || NH % 16) {
      fx.showMsg("宽高必须是 64–15360 之间、16 的整数倍", true);
      return;
    }
    if (NW === curW && NH === curH) { fx.hideMsg(); return; }
    if (!isLayout) {
      ctx.wCw.value = NW; ctx.wCh.value = NH;
      fx.syncWidgets(); fx.renderAll(); fx.hideMsg();
      return;
    }
    if (!confirm(`画布调整为 ${NW}×${NH}（≈${(NW * NH / 1000000).toFixed(2)}M）并等比缩放所有框？`)) return;
    resizeLayoutTo(NW, NH);
    fx.hideMsg();
  }

  function applyResize(ratioKey, mpVal) {
    if (!M.S) return;
    if (!ratioKey && !mpVal) return;
    const isLayout = M.S.kind === "layout";
    const d = isLayout ? M.S.d : null;
    const curW = d ? d.width : (+ctx.wCw.value || 1024);
    const curH = d ? d.height : (+ctx.wCh.value || 1024);
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
      ctx.wCw.value = NW; ctx.wCh.value = NH;
      fx.syncWidgets(); fx.renderAll();
      return;
    }
    if (!confirm(`画布调整为 ${NW}×${NH}（≈${(NW * NH / 1000000).toFixed(2)}M）并等比缩放所有框？`)) return;
    resizeLayoutTo(NW, NH);
  }

  return {
    pushUndo, commit, afterChange, afterLightChange, restore, doUndo, doRedo,
    select, addItem, delItem, layerMove, occlude, resizeLayoutTo, applyCustomSize, applyResize,
  };
}
