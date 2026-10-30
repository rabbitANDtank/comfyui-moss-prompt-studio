import test from "node:test";
import assert from "node:assert/strict";
import {
  queueRemaining, shouldResetOnQueueIdle, shouldResetOnTimeout, queueHasNode,
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
