/** 提交/队列状态判定（纯逻辑，无 DOM，便于 node --test 覆盖）。
 * 解决 /queue 轮询下标、status 负载解析、“提交卡死”复位与结果归属。 */

import { draftSourceAttribution } from "./source_guard.js";

/** 解析 ComfyUI status 事件里的 queue_remaining。
 * 前端 1.57.4 解包为 {exec_info:{queue_remaining}}；旧负载为 {status:{exec_info:{...}}}。
 * 注意：必须显式分支，不能用 `0 || ...`（0 是合法值，会被误当成缺失）。 */
export function queueRemaining(statusDetail) {
  const st = statusDetail;
  if (!st || typeof st !== "object") return undefined;
  return st.exec_info
    ? st.exec_info.queue_remaining
    : (st.status && st.status.exec_info ? st.status.exec_info.queue_remaining : undefined);
}

/** 队列已空但本次拆解/注入仍挂着（缓存跳过/事件丢失）→ 复位。 */
export function shouldResetOnQueueIdle({ queueRemaining: q, hasPending, elapsedMs, idleMs = 4000 } = {}) {
  return !!hasPending && q === 0 && Number(elapsedMs) > idleMs;
}

/** 180s 硬兜底：拆解/注入无论什么原因没回来都要复位，避免永久否决 Queue。 */
export function shouldResetOnTimeout({ hasPending, elapsedMs, timeoutMs = 180000 } = {}) {
  return !!hasPending && Number(elapsedMs) > timeoutMs;
}

/** /queue 条目形如 [number, prompt_id, prompt, extra_data, outputs]（server.py 截到 5 项）。
 * 判断某个节点 id 是否在 prompt 对象里；不能取 item[1]（那是 prompt_id 字符串）。 */
export function queueHasNode(entries, nodeId) {
  const id = String(nodeId);
  return (Array.isArray(entries) ? entries : []).some((item) => {
    if (!Array.isArray(item)) return false;
    const prompt = item[2];
    return !!prompt && typeof prompt === "object" && Object.prototype.hasOwnProperty.call(prompt, id);
  });
}

/** ws 有时把长字符串拆成单字符数组；统一归一成去空白字符串。 */
function normText(v) {
  const raw = Array.isArray(v) ? v.join("") : v;
  return typeof raw === "string" ? raw.trim() : "";
}

/**
 * 同一 mps_draft 是否属于“双通道重复投递”。
 * 只有文本相同、在时间窗口内、且两条投递没有可区分的 prompt_id 时才算重复；
 * 不同 prompt_id 的同文结果属于两次提交（旧提交 vs 本次提交），不能去重，
 * 否则真正属于本次提交的结果会被误杀。
 */
export function isDuplicateDraft({
  lastText = "", lastAt = 0, lastPid = "", text = "", now = Date.now(), promptId = "", windowMs = 2000,
} = {}) {
  if (!text || lastText !== text) return false;
  if (!(Number(now) - (Number(lastAt) || 0) < windowMs)) return false;
  const prev = normText(lastPid);
  const cur = normText(promptId);
  return !prev || !cur || prev === cur;
}

/**
 * mps_draft 结果归属/写回决策（纯逻辑）。
 *
 * 返回 { outcome, write, resetPending, notify }：
 *   - accept    最近一次提交的当前 source：写回画布；
 *   - duplicate 双通道重复投递：只复位，不重复写回；
 *   - stale     结果自带的 source 已变化：丢弃内容、复位并提示；
 *   - foreign   prompt_id 不属于最近一次提交（别的 Queue/旧提交）：
 *               丢弃内容并提示，但不解除本次等待，避免抢跑仍在飞的手工提交。
 *
 * prompt_id 匹配只在“最近一次手工提交”已知时生效；无 prompt_id（旧前端
 * onExecuted）或最近提交已复位（lastSubmitId 为空）时回退到 ui.mps_src_sig /
 * pending 签名，保持现有行为。任何真正属于本次提交的结果（含重复/过期）
 * 都必须复位提交状态，否则 M.submitting 会一直否决 Queue/Ctrl+Enter。
 */
export function resultAttributionDecision({
  promptId, lastSubmitId, uiSig, currentSig, pendingSig, duplicate = false,
} = {}) {
  const pid = normText(promptId);
  const last = normText(lastSubmitId);
  // 结果自带 prompt_id 且与最近一次提交不符：这是别的执行（完整 Queue/旧提交），
  // 即使 source 签名相同也不能写回（同 source 二次提交的场景）。
  if (pid && last && pid !== last) {
    return { outcome: "foreign", write: false, resetPending: false, notify: true };
  }
  if (duplicate) {
    // 双通道的第二次投递：内容已经由第一条路径处理过，只确保提交状态复位。
    return { outcome: "duplicate", write: false, resetPending: true, notify: false };
  }
  // source 归属回退：复用 source_guard 的既有规则（uiSig 优先、缺失时 pending）。
  const stale = draftSourceAttribution({ uiSig, currentSig, pendingSig }) === "stale";
  if (stale) {
    return { outcome: "stale", write: false, resetPending: true, notify: true };
  }
  return { outcome: "accept", write: true, resetPending: true, notify: false };
}
