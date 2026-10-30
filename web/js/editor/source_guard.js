/**
 * source 归属守卫（纯逻辑，无 DOM，便于 node --test 覆盖）。
 *
 * 背景：⚡拆解 / 📥姿势 / 普通 Queue 的 mps_draft 都是异步结果。若用户在 LLM
 * 执行期间改了 source，旧 source 的结果晚到后会被写进 edits —— 旧实现还会把
 * edits_source_sig 盖成“当前 source”的签名，于是更旧的画布被当成新 source 的
 * 画布提交（P0-1）。这里只回答两个问题：
 *   1. 结果是否还属于当前 source（不属于则丢弃）；
 *   2. 写回 edits 时应记录哪份 source 的归属签名（必须沿用画布生成时的签名）。
 */

/** 没有任何提交记录时返回 false（兼容页面刷新/旧路径，保持旧行为）。 */
export function isStaleDraftSource(currentSourceSig, submittedSourceSigs) {
  const list = Array.isArray(submittedSourceSigs) ? submittedSourceSigs : [submittedSourceSigs];
  const pending = list.filter((s) => typeof s === "string" && s.trim());
  if (!pending.length) return false;
  return !pending.includes(String(currentSourceSig || "").trim());
}

/** 写回 edits 的归属签名：优先沿用生成这份画布时的 source 签名。 */
export function editsOwnerSignature(stateSourceSig, currentSourceSig) {
  const owned = typeof stateSourceSig === "string" ? stateSourceSig.trim() : "";
  return owned || String(currentSourceSig || "");
}

/**
 * 结果归属决策：
 * - 后端回传 ui.mps_src_sig（本次实际使用的 source 签名）时严格比较，结果自带归属；
 * - 缺失时回退到内存 pending（旧后端/旧工作流）。
 * 返回 "accept" | "stale"。
 */
export function draftSourceAttribution({ uiSig, currentSig, pendingSig } = {}) {
  const raw = Array.isArray(uiSig) ? uiSig.join("") : uiSig;
  const ui = typeof raw === "string" ? raw.trim() : "";
  if (ui) return ui === String(currentSig || "").trim() ? "accept" : "stale";
  return isStaleDraftSource(currentSig, pendingSig ? [pendingSig] : []) ? "stale" : "accept";
}

/**
 * 重建候选选择（纯逻辑）：edits 只对写入时的那份 source 有效；签名缺失（旧工作流）
 * 先按有效处理。preferEdits=true 时先尝试当前 source 的有效画布，再回退 source。
 * 返回 {editsValid, candidates}；调用方在全部候选解析失败后才清理旧 edits。
 */
export function rebuildCandidates({
  preferEdits = false, src = "", edits = "", srcSig = "", storedSig = "",
} = {}) {
  const s = String(src || "").trim();
  const e = String(edits || "").trim();
  const stored = String(storedSig || "").trim();
  const current = String(srcSig || "").trim();
  const editsValid = !!e && (!s || !stored || stored === current);
  const candidates = [];
  if (preferEdits) {
    if (editsValid) candidates.push({ text: e, fromEdits: true });
    if (s) candidates.push({ text: s, fromEdits: false });
  } else {
    if (s) candidates.push({ text: s, fromEdits: false });
    else if (e) candidates.push({ text: e, fromEdits: true });
  }
  return { editsValid, candidates };
}
