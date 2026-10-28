/**
 * MOSS Prompt Studio — Editor 纯数据层
 *
 * 这里只做解析 / 校验 / 序列化 / 纯几何计算；不依赖 DOM、ComfyUI、app 或 widget。
 * 这样可以用 Node 直接测试，View 和 comfy-hooks 只负责展示与生命周期。
 */

export const VALID_LABELS = ["object", "character", "text"];
export const VALID_CANVAS = ["single", "multi_panel", "collage"];
export const OCCL_FRONT = "，处于最前层，部分遮挡相邻元素";
export const OCCL_BACK = "，被前方元素部分遮挡";

const TOP_KEYS = ["width", "height", "bbox_coord_space", "bbox_format", "summary", "items"];
const SUMMARY_KEYS = ["caption", "meta", "canvas", "palette"];
const ITEM_KEYS = ["label", "caption", "bbox", "text", "tags", "palette"];

const isNum = (v) => typeof v === "number" && Number.isFinite(v);

export function detectKind(o) {
  if (!o || typeof o !== "object" || Array.isArray(o)) return null;
  const ks = Object.keys(o);
  if (ks.length === 1 && ks[0] === "caption" && typeof o.caption === "string") return "caption";
  if ("items" in o || "summary" in o) {
    if (o.bbox_coord_space === "abs" && o.bbox_format === "xyxy") return "draft";
    // 官方已编译口径只有 yxyx；ycyx 等变体不再静默按 yxyx 反解
    if (o.bbox_coord_space === "abs_centralized" && o.bbox_format === "yxyx") return "compiled";
  }
  return null;
}

export function reverseCompiled(o, W, H) {
  if (!o || typeof o !== "object" || Array.isArray(o)) throw new Error("已编译提示词必须是 JSON 对象");
  if (!o.summary || typeof o.summary !== "object" || Array.isArray(o.summary)) {
    throw new Error("已编译提示词缺少 summary 对象");
  }
  if (!Array.isArray(o.items)) throw new Error("已编译提示词的 items 必须是数组");
  const items = o.items.map((it, i) => {
    if (!it || typeof it !== "object" || Array.isArray(it)) throw new Error(`items[${i}] 必须是对象`);
    const b = it.bbox;
    if (!Array.isArray(b) || b.length !== 4 || !b.every(isNum)) {
      throw new Error(`items[${i}].bbox 必须是四个数字（yxyx 中心原点）`);
    }
    const [y0, x0, y1, x1] = b;
    return {
      ...it,
      bbox: [
        Math.round(x0 + W / 2), Math.round(y0 + H / 2),
        Math.round(x1 + W / 2), Math.round(y1 + H / 2),
      ],
    };
  });
  return { width: W, height: H, bbox_coord_space: "abs", bbox_format: "xyxy", summary: { ...o.summary }, items };
}

export function parseSourceText(text, width = 1024, height = 1024) {
  const s = (text || "").trim();
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
  if (kind === "compiled") return { kind: "layout", d: reverseCompiled(obj, width, height) };
  if (obj && obj.bbox_coord_space === "abs_centralized" && obj.bbox_format && obj.bbox_format !== "yxyx") {
    throw new Error(`已编译提示词只支持 bbox_format="yxyx"（当前 ${obj.bbox_format}）`);
  }
  throw new Error('无法识别形态：需要 {"caption":...} 或带 summary/items 的布局提示词');
}

function checkStrList(v, name, errs) {
  if (!Array.isArray(v) || !v.length || !v.every((x) => typeof x === "string" && x.trim())) {
    errs.push(`${name} 必须是非空字符串数组`);
  }
}

/** 与 py/compile_prompt.validate_draft 对齐的前端校验；返回错误字符串数组。 */
export function validateDraft(S) {
  const errs = [];
  if (!S) return errs;
  if (S.kind === "caption") {
    if (typeof S.caption !== "string" || !S.caption.trim()) errs.push("caption 为空");
    return errs;
  }
  const d = S.d;
  if (!d || typeof d !== "object" || Array.isArray(d)) {
    errs.push("草稿必须是对象");
    return errs;
  }

  for (const k of Object.keys(d)) if (!TOP_KEYS.includes(k)) errs.push(`未知的顶层字段: ${k}`);
  for (const k of TOP_KEYS) if (!(k in d)) errs.push(`缺少顶层字段: ${k}`);

  if (!Number.isInteger(d.width) || d.width <= 0 || d.width % 16 !== 0) {
    errs.push(`width=${d.width} 不是 16 的正倍数`);
  }
  if (!Number.isInteger(d.height) || d.height <= 0 || d.height % 16 !== 0) {
    errs.push(`height=${d.height} 不是 16 的正倍数`);
  }
  if (d.bbox_coord_space !== "abs") errs.push('bbox_coord_space 必须是 "abs"');
  if (d.bbox_format !== "xyxy") errs.push('bbox_format 必须是 "xyxy"');

  const s = d.summary;
  if (!s || typeof s !== "object" || Array.isArray(s)) {
    errs.push("summary 必须是对象");
  } else {
    for (const k of Object.keys(s)) if (!SUMMARY_KEYS.includes(k)) errs.push(`summary 中有未知字段: ${k}`);
    if (typeof s.caption !== "string" || !s.caption.trim()) errs.push("summary.caption 为空");
    if (s.canvas && !VALID_CANVAS.includes(s.canvas)) errs.push(`summary.canvas=${s.canvas} 非法`);
    if ("meta" in s) checkStrList(s.meta, "summary.meta", errs);
    if ("palette" in s) checkStrList(s.palette, "summary.palette", errs);
  }

  if (!Array.isArray(d.items) || !d.items.length) {
    errs.push("items 必须是非空数组");
    return errs;
  }
  d.items.forEach((it, i) => {
    const pre = `items[${i}]`;
    if (!it || typeof it !== "object" || Array.isArray(it)) {
      errs.push(`${pre} 必须是对象`);
      return;
    }
    for (const k of Object.keys(it)) if (!ITEM_KEYS.includes(k)) errs.push(`${pre} 中有未知字段: ${k}`);
    for (const k of ["label", "caption", "bbox"]) if (!(k in it)) errs.push(`${pre} 缺少字段: ${k}`);
    if (!VALID_LABELS.includes(it.label)) errs.push(`${pre}.label=${it.label} 非法`);
    if (typeof it.caption !== "string" || !it.caption.trim()) errs.push(`${pre}.caption 为空`);
    const b = it.bbox;
    if (!Array.isArray(b) || b.length !== 4 || !b.every(isNum)) {
      errs.push(`${pre}.bbox 必须是四个数字`);
    } else if (Number.isInteger(d.width) && Number.isInteger(d.height) && d.width > 0 && d.height > 0) {
      const [x0, y0, x1, y1] = b;
      if (!(x0 >= 0 && x0 < x1 && x1 <= d.width)) errs.push(`${pre}.bbox x 越界/翻转 ${JSON.stringify(b)}`);
      if (!(y0 >= 0 && y0 < y1 && y1 <= d.height)) errs.push(`${pre}.bbox y 越界/翻转 ${JSON.stringify(b)}`);
    }
    if (it.label === "text" && (typeof it.text !== "string" || !it.text.trim())) {
      errs.push(`${pre} 是 text 元素但 text 为空`);
    }
    if (it.label !== "text" && "text" in it) {
      errs.push(`${pre} 不是 text 元素不应有 text`);
    }
    if ("tags" in it) checkStrList(it.tags, `${pre}.tags`, errs);
    if ("palette" in it) checkStrList(it.palette, `${pre}.palette`, errs);
  });
  return errs;
}

export function snapshotOf(S) {
  if (!S) return "";
  return S.kind === "caption" ? JSON.stringify({ caption: S.caption }) : JSON.stringify(S.d);
}

export function serializeDraft(S) {
  if (!S) return "";
  return S.kind === "caption"
    ? JSON.stringify({ caption: S.caption }, null, 1)
    : JSON.stringify(S.d, null, 1);
}

export const clampDim = (v) => Math.max(64, Math.min(15360, Math.round(v / 16) * 16));
export const snap16 = (v) => clampDim(v);

export function clampBox(b, W, H) {
  let [x0, y0, x1, y1] = b.map(Number);
  x0 = Math.max(0, Math.min(Math.round(x0), W - 1));
  x1 = Math.max(0, Math.min(Math.round(x1), W));
  y0 = Math.max(0, Math.min(Math.round(y0), H - 1));
  y1 = Math.max(0, Math.min(Math.round(y1), H));
  if (x1 <= x0) x1 = Math.min(W, x0 + 8);
  if (y1 <= y0) y1 = Math.min(H, y0 + 8);
  return [x0, y0, x1, y1];
}

export function resizeDraft(d, NW, NH) {
  NW = clampDim(NW);
  NH = clampDim(NH);
  const sx = NW / d.width, sy = NH / d.height;
  return {
    ...d,
    width: NW,
    height: NH,
    items: (d.items || []).map((it) => ({
      ...it,
      bbox: clampBox([
        Math.round(it.bbox[0] * sx), Math.round(it.bbox[1] * sy),
        Math.round(it.bbox[2] * sx), Math.round(it.bbox[3] * sy),
      ], NW, NH),
    })),
  };
}

export const csvParse = (v) => String(v || "").split(",").map((x) => x.trim()).filter(Boolean);
export const csvShow = (v) => (Array.isArray(v) ? v : []).join(", ");
