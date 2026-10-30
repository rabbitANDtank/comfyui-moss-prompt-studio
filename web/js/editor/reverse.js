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
 * - "same"   同一结果（同图同参）：跳过写回
 * - "edited" 画布已有手改/内容，且 replace_edits=false：保留并提示
 *
 * 规则收紧点：
 * 1. 首次接反推（record 为空）且画布已有内容、replace_edits=false → "edited"，
 *   不再直接覆盖手改。
 * 2. same-sig（同图同参重跑）不再覆盖：真机实测踩坑——完整 Queue 会重跑 enabled
 *   的反推，若 prose/draft 重放写回 source，刚拆解好的画布会因签名失配立即回退
 *   caption（"画布只剩文字"）。只有目标为空时才允许恢复写入（清空后重放找回）。
 * 3. 只记录 draftHash，不在工作流里重复保存整份画布文本。
 */
export function reverseDecision(record, { ownerId, sig, mode, replaceEdits, edits, source }) {
  if (!sig) return "apply";
  const sameOwner = !!record && String(record.owner) === String(ownerId);
  const recHash = recordDraftHash(record);
  const curText = norm(edits);
  const curHash = curText ? textHash(curText) : "";
  const sameSig = sameOwner && record.sig === sig;
  // prose/draft 实际写 source；旧调用方可能只传 edits（source undefined），
  // 此时不能把“未提供 source”当成“source 已清空”，保持旧行为。
  const hasSourceArg = source !== undefined;
  const srcEmpty = hasSourceArg && !norm(source);

  if (sameSig) {
    if (mode === "prose") {
      // 目标 source 被清空才允许同结果重放恢复；否则不覆盖已有 source/拆解成果。
      if (hasSourceArg) return srcEmpty ? "apply" : "same";
      return curHash ? "same" : "apply";
    }
    if (mode === "draft") {
      // 目标 source；画布可能承载拆解/手改结果。同图同参重放不得覆盖：
      // 只有 source 与画布都为空才恢复，画布已手改时明确跳过。
      if (curHash && curHash !== recHash) return "same";
      return (srcEmpty && !curHash) ? "apply" : "same";
    }
    // canvas：目标就是 edits，保持原有保护逻辑
    if (!curHash) return "apply";         // 画布/结果被清空：允许重放恢复
    if (curHash === recHash) return "same";
    return replaceEdits ? "apply" : "edited";
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
