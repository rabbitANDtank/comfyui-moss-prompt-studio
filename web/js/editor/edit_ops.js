/**
 * AI 定向改稿：ops 应用器的 JS 镜像（与 py/edit_ops.py 语义一致，双端黄金对拍）。
 *
 * 用途：审批卡（revision_panel）的"部分采纳"与"全部采纳"——两者都经本函数
 * 算出结果草稿（🔒锁文字对两者同样生效）；后端 ui.mps_ops 不携带全集稿。
 *
 * 契约（MOSS-REVISION-RESEARCH-1101.md §6，改动两侧必须同步）：
 * - 指纹 {index,label,head_prefix} 永远对照原始草稿核验，不匹配拒收单条。
 * - 应用顺序：set_summary → item 级非 remove（列出顺序）→ remove（降序）→ add（追加）。
 * - ops ≤ MAX_EDIT_OPS：按列出顺序截断（重排之前）。
 * - 校验失败不抛错：拒收进 rejected（审批前展示）；最终草稿由 validateDraft 门禁。
 */
import { POSE_MARKER, COSTUME_MARKER,
  splitCaptionSegments, joinCaptionSegments, setCaptionSegment } from "./pose.js";
import { validateDraft, OCCL_FRONT, OCCL_BACK } from "./model.js";

export const EDIT_OP_TYPES = ["set_caption", "set_segment", "set_text",
  "move_resize", "add_item", "remove_item", "set_summary"];
export const EDIT_SEGMENT_KEYS = ["pose", "costume"];
export const MAX_EDIT_OPS = 8;
const TARGET_KEYS = ["index", "label", "head_prefix"];
const SUMMARY_KEYS = ["caption", "meta", "canvas", "palette"];
const ITEM_KEYS = ["label", "caption", "bbox", "text", "tags", "palette"];
const VALID_LABELS = ["object", "character", "text"];
const VALID_CANVAS = ["single", "multi_panel", "collage"];
const OP_REQUIRED = {
  set_caption: ["target", "head"],
  set_segment: ["target", "segment", "text"],
  set_text: ["target", "text"],
  move_resize: ["target", "bbox"],
  add_item: ["item"],
  remove_item: ["target"],
  set_summary: ["fields"],
};
const HEAD_FORBIDDEN = [POSE_MARKER, COSTUME_MARKER, OCCL_FRONT, OCCL_BACK];

const isInt = (v) => typeof v === "number" && Number.isInteger(v);
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const headOf = (item) => splitCaptionSegments((item && item.caption) || "").head;

function checkStrList(v) {
  return Array.isArray(v) && v.every((x) => typeof x === "string" && x.trim());
}

function checkBboxValue(b, w, h) {
  if (!Array.isArray(b) || b.length !== 4 || !b.every(isNum)) return false;
  const [x0, y0, x1, y1] = b;
  return 0 <= x0 && x0 < x1 && x1 <= w && 0 <= y0 && y0 < y1 && y1 <= h;
}

function checkTarget(t, origItems, scope) {
  if (!t || typeof t !== "object" || TARGET_KEYS.some((k) => !(k in t))
    || Object.keys(t).some((k) => !TARGET_KEYS.includes(k))) {
    return "target 必须恰含 index/label/head_prefix 三字段";
  }
  if (!isInt(t.index) || t.index < 0 || t.index >= origItems.length) {
    return `index=${String(t.index)} 越界`;
  }
  const it = origItems[t.index];
  const prefix = String(t.head_prefix || "");
  if (prefix.trim().length < 2) return "head_prefix 至少 2 个字符";
  if (t.label !== it.label) return `label 不匹配（目标实为 ${JSON.stringify(it.label)}）`;
  if (!headOf(it).trim().startsWith(prefix.trim())) {
    return "head_prefix 与目标当前描述开头不符（索引漂移防护）";
  }
  if (scope && !scope.indexes.includes(t.index)) {
    return `scope 限定，index=${String(t.index)} 不在选中范围`;
  }
  return "";
}

/** op 级校验：返回拒收原因（空串=通过）。对照原始草稿。 */
export function checkOp(op, origItems, w, h, scope, removed) {
  if (!op || typeof op !== "object") return "op 必须是对象";
  const kind = op.op;
  if (!EDIT_OP_TYPES.includes(kind)) return `未知 op 类型: ${JSON.stringify(kind)}`;
  const required = OP_REQUIRED[kind];
  const unknown = Object.keys(op).filter((k) => k !== "op" && k !== "reason" && !required.includes(k));
  if (unknown.length) return `op 含未知字段: ${JSON.stringify(unknown)}`;
  const missing = required.filter((k) => !(k in op));
  if (missing.length) return `缺少必填字段: ${JSON.stringify(missing)}`;
  if (typeof op.reason !== "string" || !op.reason.trim()) return "reason 必填（审批卡展示用）";

  if (kind === "set_summary") {
    if (scope && !scope.summary) return "scope 限定时不允许修改 summary";
    const f = op.fields;
    if (!f || typeof f !== "object" || Array.isArray(f) || !Object.keys(f).length) {
      return "fields 必须是非空对象";
    }
    const extra = Object.keys(f).filter((k) => !SUMMARY_KEYS.includes(k));
    if (extra.length) return `fields 含未知字段: ${JSON.stringify(extra)}`;
    if ("caption" in f && (typeof f.caption !== "string" || !f.caption.trim())) {
      return "fields.caption 不能为空";
    }
    if ("canvas" in f && !VALID_CANVAS.includes(f.canvas)) {
      return `fields.canvas 必须是 ${JSON.stringify(VALID_CANVAS)} 之一`;
    }
    for (const key of ["meta", "palette"]) {
      if (key in f && !checkStrList(f[key])) return `fields.${key} 必须是字符串数组（元素不能为空）`;
    }
    return "";
  }

  if (kind === "add_item") {
    if (scope) return "scope 限定时不允许新增条目";
    const item = op.item;
    if (!item || typeof item !== "object" || Array.isArray(item)) return "item 必须是对象";
    const extra = Object.keys(item).filter((k) => !ITEM_KEYS.includes(k));
    if (extra.length) return `item 含未知字段: ${JSON.stringify(extra)}`;
    for (const k of ["label", "caption", "bbox"]) {
      if (!(k in item)) return `item 缺少字段: ${JSON.stringify([k])}`;
    }
    if (!VALID_LABELS.includes(item.label)) return `item.label 必须是 ${JSON.stringify(VALID_LABELS)} 之一`;
    if (typeof item.caption !== "string" || !item.caption.trim()) return "item.caption 不能为空";
    if (item.label === "text") {
      if (typeof item.text !== "string" || !item.text.trim()) return "text 条目的 item.text 必填且不能为空";
    } else if ("text" in item && (typeof item.text !== "string" || !item.text.trim())) {
      return "item.text 必须是非空字符串";
    }
    for (const key of ["tags", "palette"]) {
      if (key in item && !checkStrList(item[key])) return `item.${key} 必须是字符串数组（元素不能为空）`;
    }
    if (!checkBboxValue(item.bbox, w, h)) return `item.bbox 越界或非法（画布 ${w}x${h}）`;
    return "";
  }

  const err = checkTarget(op.target, origItems, scope);
  if (err) return err;
  const { index, label } = op.target;

  if (kind === "set_caption") {
    const head = op.head;
    if (typeof head !== "string" || !head.trim()) return "head 不能为空";
    if (HEAD_FORBIDDEN.some((m) => head.includes(m))) {
      return "head 不能含分段标记/遮挡后缀（；姿势：…），分段会被误切";
    }
    return "";
  }
  if (kind === "set_segment") {
    if (!EDIT_SEGMENT_KEYS.includes(op.segment)) return `segment 必须是 ${JSON.stringify(EDIT_SEGMENT_KEYS)} 之一`;
    if (label !== "character") return "只有角色条目能设置姿势/服装分段";
    if (typeof op.text !== "string" || !op.text.trim()) return "text 不能为空";
    return "";
  }
  if (kind === "set_text") {
    if (label !== "text") return "只有文字条目能修改可见文字";
    if (typeof op.text !== "string" || !op.text.trim()) return "text 不能为空";
    return "";
  }
  if (kind === "move_resize") {
    if (!checkBboxValue(op.bbox, w, h)) return `bbox 越界或翻转（画布 ${w}x${h}）`;
    return "";
  }
  if (kind === "remove_item") {
    if (removed.has(index)) return `重复删除同一目标 index=${String(index)}`;
    removed.add(index);
    return "";
  }
  return "";
}

const deepCopy = (v) => (typeof structuredClone === "function" ? structuredClone(v) : JSON.parse(JSON.stringify(v)));

/**
 * 把 ops 应用到草稿副本：→ {draft, applied, rejected, dropped, error}。
 * 与 py/edit_ops.apply_ops 同语义；最终草稿过 validateDraft，失败置 error
 * （py 侧抛 EditApplyError，JS 侧返回字段——审批卡展示用，不写画布）。
 */
export function applyOps(draft, ops, scope) {
  const errs = validateDraft({ kind: "layout", d: draft });
  if (errs && errs.length) return { draft: deepCopy(draft), applied: [], rejected: [], dropped: 0, error: "当前画布草稿未通过官方校验：" + errs[0] };
  if (!Array.isArray(ops)) return { draft: deepCopy(draft), applied: [], rejected: [], dropped: 0, error: "ops 必须是数组" };
  const dropped = Math.max(0, ops.length - MAX_EDIT_OPS);
  const capped = ops.slice(0, MAX_EDIT_OPS);
  const w = draft.width, h = draft.height;
  const origItems = draft.items;
  const work = deepCopy(draft);
  const removed = new Set();
  const applied = [], rejected = [];
  const summaryOps = [], itemOps = [], removeOps = [], addOps = [];

  for (const op of capped) {
    const err = checkOp(op, origItems, w, h, scope, removed);
    if (err) { rejected.push({ op, reason: err }); continue; }
    if (op.op === "set_summary") summaryOps.push(op);
    else if (op.op === "remove_item") removeOps.push(op);
    else if (op.op === "add_item") addOps.push(op);
    else itemOps.push(op);
  }

  for (const op of summaryOps) {
    Object.assign(work.summary, deepCopy(op.fields));
    applied.push(op);
  }
  const items = work.items;
  for (const op of itemOps) {
    const it = items[op.target.index];
    if (op.op === "set_caption") {
      const { segments, tail } = splitCaptionSegments(it.caption || "");
      it.caption = joinCaptionSegments(op.head, segments, tail);
    } else if (op.op === "set_segment") {
      it.caption = setCaptionSegment(it.caption || "", op.segment, op.text);
    } else if (op.op === "set_text") {
      it.text = op.text;
    } else if (op.op === "move_resize") {
      it.bbox = [...op.bbox];
    }
    applied.push(op);
  }
  for (const op of [...removeOps].sort((a, b) => b.target.index - a.target.index)) {
    items.splice(op.target.index, 1);
    applied.push(op);
  }
  for (const op of addOps) {
    items.push(deepCopy(op.item));
    applied.push(op);
  }

  const finalErrs = validateDraft({ kind: "layout", d: work });
  return {
    draft: work, applied, rejected, dropped,
    error: finalErrs && finalErrs.length ? "ops 应用后草稿未通过官方校验：" + finalErrs[0] : "",
  };
}

/**
 * 词级 diff（自研 LCS，审批卡展示用）：把新旧文本切成 CJK 单字 / 拉丁词 / 空白 /
 * 单符号，返回 [{type:"same"|"add"|"del", text}]。输入积超界时兜底整体两段。
 */
export function wordDiff(oldText, newText) {
  const a = tokenize(oldText), b = tokenize(newText);
  if (!a.length) return b.map((t) => ({ type: "add", text: t }));
  if (!b.length) return a.map((t) => ({ type: "del", text: t }));
  if (a.length * b.length > 250000) {
    return [{ type: "del", text: String(oldText) }, { type: "add", text: String(newText) }];
  }
  // LCS DP：输入积 ≤250000 已保证不溢出 Uint16
  const n = a.length, m = b.length;
  const dp = new Uint16Array((n + 1) * (m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * (m + 1) + j] = a[i] === b[j]
        ? dp[(i + 1) * (m + 1) + j + 1] + 1
        : Math.max(dp[(i + 1) * (m + 1) + j], dp[i * (m + 1) + j + 1]);
    }
  }
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ type: "same", text: a[i] }); i++; j++; }
    else if (dp[(i + 1) * (m + 1) + j] >= dp[i * (m + 1) + j + 1]) { out.push({ type: "del", text: a[i] }); i++; }
    else { out.push({ type: "add", text: b[j] }); j++; }
  }
  while (i < n) { out.push({ type: "del", text: a[i] }); i++; }
  while (j < m) { out.push({ type: "add", text: b[j] }); j++; }
  return out;
}

function tokenize(text) {
  const s = String(text ?? "");
  const out = [];
  let i = 0;
  while (i < s.length) {
    const ch = s[i];
    if (/[\u4e00-\u9fff\u3040-\u30ff\u3400-\u4dbf]/.test(ch)) { out.push(ch); i++; continue; }
    if (/[A-Za-z0-9_'-]/.test(ch)) {
      let j = i + 1;
      while (j < s.length && /[A-Za-z0-9_'-]/.test(s[j])) j++;
      out.push(s.slice(i, j)); i = j; continue;
    }
    if (/\s/.test(ch)) {
      let j = i + 1;
      while (j < s.length && /\s/.test(s[j])) j++;
      out.push(s.slice(i, j)); i = j; continue;
    }
    out.push(ch); i++;
  }
  return out;
}
