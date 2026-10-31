import test from "node:test";
import assert from "node:assert/strict";
import {
  queueRemaining, shouldResetOnQueueIdle, shouldResetOnTimeout, fallbackTimeoutMs, queueHasNode,
  isDuplicateDraft, resultAttributionDecision,
} from "../web/js/editor/submit_gate.js";

// F2：status 负载解析（0 必须命中，不能用 `0 || ...`）
test("queueRemaining parses both status payload shapes and keeps 0", () => {
  assert.equal(queueRemaining({ exec_info: { queue_remaining: 0 } }), 0);
  assert.equal(queueRemaining({ status: { exec_info: { queue_remaining: 0 } } }), 0);
  assert.equal(queueRemaining({ exec_info: { queue_remaining: 3 } }), 3);
  assert.equal(queueRemaining({ status: { exec_info: { queue_remaining: 7 } } }), 7);
  assert.equal(queueRemaining({}), undefined);
  assert.equal(queueRemaining(null), undefined);
  assert.equal(queueRemaining(undefined), undefined);
});

// F2：队列空复位（>4s）与 180s 硬兜底
test("shouldResetOnQueueIdle only resets pending submissions on queue_remaining=0 past idle", () => {
  assert.equal(shouldResetOnQueueIdle({ queueRemaining: 0, hasPending: true, elapsedMs: 5000 }), true);
  assert.equal(shouldResetOnQueueIdle({ queueRemaining: 0, hasPending: true, elapsedMs: 4000 }), false);
  assert.equal(shouldResetOnQueueIdle({ queueRemaining: 1, hasPending: true, elapsedMs: 5000 }), false);
  assert.equal(shouldResetOnQueueIdle({ queueRemaining: undefined, hasPending: true, elapsedMs: 5000 }), false);
  assert.equal(shouldResetOnQueueIdle({ queueRemaining: 0, hasPending: false, elapsedMs: 5000 }), false);
});

test("shouldResetOnTimeout covers split and pose pending buttons at 180s", () => {
  assert.equal(shouldResetOnTimeout({ hasPending: true, elapsedMs: 180001 }), true);
  assert.equal(shouldResetOnTimeout({ hasPending: true, elapsedMs: 180000 }), false);
  assert.equal(shouldResetOnTimeout({ hasPending: false, elapsedMs: 999999 }), false);
  assert.equal(shouldResetOnTimeout({ hasPending: true, elapsedMs: 1000 }), false);
});

// F4：/queue 条目 [number, prompt_id, prompt, extra, outputs]
test("queueHasNode reads the 3rd tuple item (prompt object)", () => {
  const entries = [
    [0, "pid-1", { "224": { class_type: "MOSSTranslator" } }, {}, ["224"]],
    [1, "pid-2", { "225": { class_type: "MOSSEditor" } }, {}, ["225"]],
  ];
  assert.equal(queueHasNode(entries, "225"), true);
  assert.equal(queueHasNode(entries, 225), true);
  assert.equal(queueHasNode(entries, "224"), true);
  assert.equal(queueHasNode(entries, "999"), false);
  assert.equal(queueHasNode([], "225"), false);
  assert.equal(queueHasNode(null, "225"), false);
  // 旧错误实现：把 prompt_id 字符串当对象（"225" 不是 pid 的字符）
  assert.equal(queueHasNode([[0, "225", { "999": {} }]], "225"), false);
  // 畸形条目
  assert.equal(queueHasNode([null, "x", [0, "pid"], [0, "pid", null]], "225"), false);
});

// P1：结果到达后无论是否重复都必须复位提交状态；去重只影响是否写回画布。
test("resultAttributionDecision resets pending state for normal, duplicate and stale results", () => {
  const base = { promptId: "pid-1", lastSubmitId: "pid-1", uiSig: "fnv1a:aaaa", currentSig: "fnv1a:aaaa" };
  assert.deepEqual(resultAttributionDecision(base), {
    outcome: "accept", write: true, resetPending: true, notify: false,
  });
  const dup = resultAttributionDecision({ ...base, duplicate: true });
  assert.equal(dup.outcome, "duplicate");
  assert.equal(dup.write, false);
  assert.equal(dup.resetPending, true, "重复投递也必须复位提交状态/按钮");

  const stale = resultAttributionDecision({ ...base, currentSig: "fnv1a:bbbb" });
  assert.equal(stale.outcome, "stale");
  assert.equal(stale.write, false);
  assert.equal(stale.resetPending, true, "stale 结果同样结束本次提交等待");
});

// P1：prompt_id 必须匹配最近一次提交；同 source 二次提交也不能被旧结果写回。
test("resultAttributionDecision matches prompt_id of the latest submission", () => {
  // 当前提交 → 接受
  assert.equal(resultAttributionDecision({
    promptId: "pid-2", lastSubmitId: "pid-2", uiSig: "fnv1a:aaaa", currentSig: "fnv1a:aaaa",
  }).outcome, "accept");
  // 旧提交（即使 source 签名完全相同）→ 丢弃，且不解除本次等待
  const old = resultAttributionDecision({
    promptId: "pid-1", lastSubmitId: "pid-2", uiSig: "fnv1a:aaaa", currentSig: "fnv1a:aaaa",
  });
  assert.equal(old.outcome, "foreign");
  assert.equal(old.write, false);
  assert.equal(old.resetPending, false);
  assert.equal(old.notify, true);
  // 同 source 二次提交：旧 prompt_id 的结果不能因为签名相同被采纳
  assert.equal(resultAttributionDecision({
    promptId: "pid-old", lastSubmitId: "pid-new", currentSig: "fnv1a:same", pendingSig: "fnv1a:same",
  }).write, false);
});

// v1.8：本地注入执行快，executed 可能先于 /prompt 响应到达（lastSubmitId 已清空）。
test("resultAttributionDecision accepts same-submit result before /prompt response", () => {
  // 新提交的结果先到：last 为空、prev 是上一提交 → 接受
  const accepted = resultAttributionDecision({
    promptId: "pid-new", lastSubmitId: "", previousSubmitId: "pid-old",
    uiSig: "fnv1a:same", currentSig: "fnv1a:same",
  });
  assert.equal(accepted.outcome, "accept");
  assert.equal(accepted.write, true);
  // 上一提交的迟到结果：pid 等于 prev → 丢弃且不解除本次等待
  const late = resultAttributionDecision({
    promptId: "pid-old", lastSubmitId: "", previousSubmitId: "pid-old",
    uiSig: "fnv1a:same", currentSig: "fnv1a:same",
  });
  assert.equal(late.outcome, "foreign");
  assert.equal(late.write, false);
  assert.equal(late.resetPending, false);
  // 首次提交（prev 为空）的结果先到：接受
  assert.equal(resultAttributionDecision({
    promptId: "pid-first", lastSubmitId: "", previousSubmitId: "",
    uiSig: "fnv1a:same", currentSig: "fnv1a:same",
  }).outcome, "accept");
});

// P1：无 prompt_id（旧前端 onExecuted）或最近提交已复位时回退到签名判断。
test("resultAttributionDecision falls back to source/pending signatures without prompt_id", () => {
  // 无 prompt_id，ui.mps_src_sig 与当前 source 一致 → 接受（保持旧行为）
  assert.equal(resultAttributionDecision({
    lastSubmitId: "pid-1", uiSig: "fnv1a:aaaa", currentSig: "fnv1a:aaaa",
  }).outcome, "accept");
  // ui.mps_src_sig 不一致 → stale
  assert.equal(resultAttributionDecision({
    lastSubmitId: "pid-1", uiSig: "fnv1a:aaaa", currentSig: "fnv1a:bbbb",
  }).outcome, "stale");
  // uiSig 缺失 → pending 兜底
  assert.equal(resultAttributionDecision({ currentSig: "fnv1a:bbbb", pendingSig: "fnv1a:bbbb" }).outcome, "accept");
  assert.equal(resultAttributionDecision({ currentSig: "fnv1a:bbbb", pendingSig: "fnv1a:aaaa" }).outcome, "stale");
  // ws 字符数组归一
  assert.equal(resultAttributionDecision({
    uiSig: ["fnv1a:a", "aaa"], currentSig: "fnv1a:aaaa",
  }).outcome, "accept");
  // 最近提交已复位（lastSubmitId 为空）+ 完整 Queue 结果 → 回退签名，不误杀
  assert.equal(resultAttributionDecision({
    promptId: "pid-from-full-queue", lastSubmitId: "", uiSig: "fnv1a:aaaa", currentSig: "fnv1a:aaaa",
  }).outcome, "accept");
});

// P1：双通道去重只在“同一次投递”上生效，不能吞掉两次提交的同文结果。
test("isDuplicateDraft distinguishes dual-channel replay from two real submissions", () => {
  const base = { lastText: "DRAFT", lastAt: 1000, text: "DRAFT", now: 1500 };
  assert.equal(isDuplicateDraft(base), true, "无 prompt_id 的旧路径 + 新路径 = 同一结果");
  assert.equal(isDuplicateDraft({ ...base, lastPid: "pid-1", promptId: "pid-1" }), true);
  assert.equal(isDuplicateDraft({ ...base, lastPid: "", promptId: "pid-2" }), true, "至少一方无 pid 视为双通道");
  assert.equal(isDuplicateDraft({ ...base, lastPid: "pid-1", promptId: "pid-2" }), false, "不同提交的同文结果不得去重");
  assert.equal(isDuplicateDraft({ ...base, text: "OTHER" }), false);
  assert.equal(isDuplicateDraft({ ...base, now: 4000 }), false, "超出 2s 窗口");
  assert.equal(isDuplicateDraft({ ...base, text: "" }), false, "空结果不做重复判定");
});

test("fallbackTimeoutMs covers user-configurable timeout (review P2-3)", () => {
  assert.equal(fallbackTimeoutMs(undefined), 210000, "缺省按 180s+30s");
  assert.equal(fallbackTimeoutMs(180), 210000);
  assert.equal(fallbackTimeoutMs(600), 630000, "用户 timeout=600s 时兜底应随之放宽");
  assert.equal(fallbackTimeoutMs(1200), 1230000, "最大档 1200s");
  assert.equal(fallbackTimeoutMs(0), 210000, "0/负数/非数回落默认");
  assert.equal(fallbackTimeoutMs("abc"), 210000);
  assert.equal(fallbackTimeoutMs(-5), 210000);
});
