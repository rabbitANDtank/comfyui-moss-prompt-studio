/**
 * mps_draft 结果写回（纯逻辑 + 注入的读写器，便于 Node 测试）。
 *
 * 旧实现先把结果字符串写进 edits widget，再调用 rebuildFromEdits 解析；
 * 解析失败时坏 JSON 已经留在 widget 里（保存工作流/下次 Queue 都受影响）。
 * 这里先完成 JSON.parse 与结构校验，只有成功后才写 widget 并重建画布；
 * 重建失败则回滚到旧 edits。
 */

import { detectKind, validateDraft } from "./model.js";

/** 解析 + 结构校验，返回 { text, draft }；不修改任何 widget。 */
export function prepareDraftWriteback(raw) {
  const text = String(raw ?? "");
  if (!text.trim()) throw new Error("结果为空");
  let obj;
  try {
    obj = JSON.parse(text);
  } catch (e) {
    throw new Error("JSON 解析失败: " + (e && e.message ? e.message : e));
  }
  if (detectKind(obj) !== "draft") {
    throw new Error("结果不是布局草稿（需要 abs/xyxy 的 summary+items）");
  }
  const errs = validateDraft({ kind: "layout", d: obj });
  if (errs.length) throw new Error(errs.join("；"));
  return { text, draft: obj };
}

/**
 * 写回一次 mps_draft 结果。
 * - getEdits/setEdits：读写 edits widget（可以是普通对象，测试无需 DOM）；
 * - rebuild：用解析后的结果重建画布，返回 false/抛错表示失败。
 * 返回 { ok, error?, draft?, rolledBack? }；解析失败时 setEdits 不会被调用。
 */
export function commitDraftWriteback({ raw, getEdits, setEdits, rebuild } = {}) {
  let prepared;
  try {
    prepared = prepareDraftWriteback(raw);
  } catch (e) {
    return { ok: false, error: e.message || String(e), rolledBack: false };
  }
  const before = typeof getEdits === "function" ? getEdits() : undefined;
  if (typeof setEdits === "function") setEdits(prepared.text);
  let rebuilt = true;
  try {
    rebuilt = typeof rebuild === "function" ? rebuild(prepared) !== false : true;
  } catch (e) {
    rebuilt = false;
  }
  if (!rebuilt) {
    // 解析成功但画布拒绝重建：回滚 widget，保留用户原来的 edits。
    if (typeof setEdits === "function" && before !== undefined) setEdits(before);
    return { ok: false, error: "画布重建失败，已回滚 edits", rolledBack: before !== undefined };
  }
  return { ok: true, text: prepared.text, draft: prepared.draft };
}
