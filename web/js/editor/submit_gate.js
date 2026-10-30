/** 提交/队列状态判定（纯逻辑，无 DOM，便于 node --test 覆盖）。
 * 解决 /queue 轮询下标、status 负载解析与“提交卡死”复位条件。 */

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
