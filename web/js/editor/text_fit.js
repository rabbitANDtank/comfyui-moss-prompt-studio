/**
 * 文字容量 / 可读性粗估（纯逻辑，无 DOM，node --test 覆盖）。
 *
 * 仅用于前端 warning：官方校验仍以 model.js / compile_prompt 的 validateDraft 为准，
 * 这里的估计不会阻止保存或提交。宽度单位按经验值近似：
 *   CJK（含全角标点/假名/谚文）≈ 1em，ASCII ≈ 0.55em，空格 ≈ 0.35em，其他 ≈ 0.7em。
 */

export const TOKEN_WARN_LIMIT = 4000;

const CJK_RE = /[\u1100-\u11FF\u2E80-\u303F\u3040-\u30FF\u3100-\u312F\u3130-\u318F\u31A0-\u31BF\u3400-\u4DBF\u4E00-\u9FFF\uA960-\uA97F\uAC00-\uD7AF\uF900-\uFAFF\uFE30-\uFE4F\uFF00-\uFFEF]/;

const isNum = (v) => typeof v === "number" && Number.isFinite(v);

function isCjk(ch) {
  return CJK_RE.test(ch);
}

/** 单字符宽度（em）：CJK 1、ASCII 0.55、空格 0.35，其他文字/符号按 0.7 粗估。 */
function charWidthEm(ch) {
  if (ch === " " || ch === "\t" || ch === "\u3000") return 0.35;
  if (ch.charCodeAt(0) < 128) return 0.55;
  if (isCjk(ch)) return 1;
  return 0.7;
}

/**
 * 估算一个 text item 的字号 / 行数 / 是否溢出。
 * 只处理 label="text" 且 text 非空、bbox 合法的条目；其余返回 null。
 *
 * fontSize = max(12, min(bbox高 / 1.2, maxFontSize))；
 * 按 bbox 宽逐字换行后，若某行超宽或 lines × fontSize × 1.2 > bbox 高 → ok:false。
 *
 * @returns {null | {
 *   ok: boolean, fontSize: number, lineHeight: number, lines: number,
 *   maxLineWidth: number, width: number, height: number,
 *   overflowX: boolean, overflowY: boolean,
 * }}
 */
export function estimateTextFit(item, { maxFontSize = 72 } = {}) {
  if (!item || item.label !== "text") return null;
  const text = typeof item.text === "string" ? item.text : "";
  if (!text.trim()) return null;
  const bbox = item.bbox;
  if (!Array.isArray(bbox) || bbox.length !== 4 || !bbox.every(isNum)) return null;
  const width = bbox[2] - bbox[0];
  const height = bbox[3] - bbox[1];
  if (!(width > 0) || !(height > 0)) return null;

  const cap = isNum(maxFontSize) && maxFontSize > 0 ? maxFontSize : 72;
  const fontSize = Math.max(12, Math.min(height / 1.2, cap));
  const lineHeight = fontSize * 1.2;

  const rows = [];
  let curWidth = 0;
  let curChars = 0;
  const pushRow = () => {
    rows.push(curWidth);
    curWidth = 0;
    curChars = 0;
  };
  for (const ch of text) {
    if (ch === "\n") {
      pushRow();
      continue;
    }
    const cw = charWidthEm(ch) * fontSize;
    if (curChars > 0 && curWidth + cw > width) pushRow();
    curWidth += cw;
    curChars += 1;
  }
  if (curChars > 0 || !rows.length) pushRow();

  const lines = rows.length;
  const maxLineWidth = Math.max(...rows);
  const overflowX = maxLineWidth > width + 1e-6;
  const overflowY = lines * lineHeight > height + 1e-6;
  return {
    ok: !overflowX && !overflowY,
    fontSize,
    lineHeight,
    lines,
    maxLineWidth,
    width,
    height,
    overflowX,
    overflowY,
  };
}

/**
 * 检查草稿里所有 text item；只报告容量不足的条目（空 text 跳过）。
 * @returns {{index:number, message:string}[]}
 */
export function lintTextItems(draft) {
  const out = [];
  const items = draft && Array.isArray(draft.items) ? draft.items : [];
  items.forEach((it, index) => {
    if (!it || it.label !== "text") return;
    if (typeof it.text !== "string" || !it.text.trim()) return;
    const fit = estimateTextFit(it);
    if (!fit || fit.ok) return;
    const parts = [];
    if (fit.overflowX) {
      parts.push(`单行超宽（最宽约 ${Math.round(fit.maxLineWidth)}px > 框宽 ${Math.round(fit.width)}px）`);
    }
    if (fit.overflowY) {
      const size = Math.round(fit.fontSize * 10) / 10;
      parts.push(`估计 ${fit.lines} 行 × ${size}px ≈ ${Math.round(fit.lines * fit.lineHeight)}px > 框高 ${Math.round(fit.height)}px`);
    }
    out.push({
      index,
      message: `items[${index}] 文字容量不足：${parts.join("；") || "超出文字框"}`,
    });
  });
  return out;
}

/** 粗估 token：CJK 1 token/字，其余字符约 4 字符/token。返回整数。 */
export function estimatePromptTokens(text) {
  const s = String(text ?? "");
  let cjk = 0;
  let other = 0;
  for (const ch of s) {
    if (isCjk(ch)) cjk += 1;
    else other += 1;
  }
  return cjk + Math.ceil(other / 4);
}
