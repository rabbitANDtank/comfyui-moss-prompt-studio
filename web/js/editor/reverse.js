/** 反推节点 ↔ Editor 的纯逻辑（无 DOM，便于 node --test 覆盖）。 */

export const REVERSE_FORMS = ["prose", "draft", "canvas"];

export function parseReversePayload(raw) {
  if (!raw) return null;
  const text = Array.isArray(raw) ? raw.join("") : (typeof raw === "object" ? raw : String(raw));
  if (typeof text === "object") return text;
  try {
    const o = JSON.parse(text);
    return o && typeof o === "object" ? o : null;
  } catch (e) {
    return null;
  }
}

export function pickReverseForm(payload, slot) {
  if (!payload) return null;
  const form = REVERSE_FORMS[slot];
  if (!form) return null;
  const text = form === "canvas" ? (payload.canvas ?? payload.draft) : payload[form];
  if (typeof text !== "string" || !text.trim()) return null;
  return { mode: form, text };
}

export function parseReverseRecord(raw) {
  if (!raw) return null;
  if (typeof raw === "object") return raw;
  try {
    const o = JSON.parse(String(raw));
    return o && typeof o === "object" ? o : null;
  } catch (e) {
    return null;
  }
}

/** 稳定文本哈希（FNV-1a 32 位，按 UTF-16 code unit）。只用于前端比较，不跨语言。 */
export function textHash(text) {
  const s = String(text ?? "");
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i) & 0xffff;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0");
}

function norm(s) {
  return String(s ?? "").trim();
}

function recordDraftHash(record) {
  if (!record) return "";
  if (typeof record.draftHash === "string" && record.draftHash) return record.draftHash;
  // 兼容旧记录（存了完整 draft 文本）
  return typeof record.draft === "string" ? textHash(record.draft) : "";
}

/**
 * 反推写回决策：
 * - "apply"  写入
 * - "same"   同一结果、内容未变：重复事件去重
 * - "edited" 画布已有手改/内容，且 replace_edits=false：保留并提示
 *
 * 规则收紧点：
 * 1. 首次接反推（record 为空）且画布已有内容、replace_edits=false → "edited"，
 *    不再直接覆盖手改。
 * 2. same-sig 不再永久阻止：replace_edits=true 允许重放；画布被清空也允许恢复。
 * 3. 只记录 draftHash，不在工作流里重复保存整份画布文本。
 */
export function reverseDecision(record, { ownerId, sig, mode, replaceEdits, edits }) {
  if (!sig) return "apply";
  const sameOwner = !!record && String(record.owner) === String(ownerId);
  const recHash = recordDraftHash(record);
  const curText = norm(edits);
  const curHash = curText ? textHash(curText) : "";
  const sameSig = sameOwner && record.sig === sig;

  if (sameSig) {
    if (replaceEdits) return "apply";     // 显式允许覆盖时，重放同一结果
    if (!curHash) return "apply";         // 画布被清空：恢复同一结果
    return curHash === recHash ? "same" : "edited";
  }

  if (mode === "canvas" && !replaceEdits) {
    if (!curHash) return "apply";         // 画布为空：首次写入
    if (sameOwner && record && (record.mode === "canvas" || record.mode === "draft")
        && recHash && curHash === recHash) {
      return "apply";                     // 画布仍是上次自动写入的内容，可安全换图
    }
    return "edited";                      // 首轮已有内容或已手改：保留
  }
  return "apply";
}

export function reverseRecord(ownerId, sig, mode, draft) {
  return JSON.stringify({
    owner: ownerId,
    sig: sig || "",
    mode: mode || "",
    draftHash: textHash(draft || ""),
  });
}

/**
 * 从 graphToPrompt() 的完整 prompt 中裁剪出以 rootId 为根的节点子图。
 * 用于 ⚡反推 按钮：只提交反推节点及其上游（LoadImage 等），不跑整个工作流。
 */
export function collectPromptSubset(prompt, rootId) {
  const out = {};
  const seen = new Set();
  const walk = (id) => {
    const key = String(id);
    if (seen.has(key)) return;
    if (!prompt || typeof prompt !== "object") return;
    const entry = prompt[key];
    if (!entry || typeof entry !== "object") return;
    seen.add(key);
    out[key] = entry;
    const inputs = entry.inputs || {};
    for (const v of Object.values(inputs)) {
      if (Array.isArray(v) && v.length === 2
          && (typeof v[0] === "string" || typeof v[0] === "number")) {
        walk(v[0]);
      }
    }
  };
  walk(rootId);
  return out;
}
