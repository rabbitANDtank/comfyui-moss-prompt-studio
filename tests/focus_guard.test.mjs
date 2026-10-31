import test from "node:test";
import assert from "node:assert/strict";
import { flushFocusedField } from "../web/js/editor/focus_guard.js";

test("flushFocusedField blurs only when focus is inside the inspector", () => {
  let blurred = 0;
  const inside = { blur() { blurred += 1; } };
  const container = { contains: (el) => el === inside };
  assert.equal(flushFocusedField(inside, container), true);
  assert.equal(blurred, 1);

  // 焦点在检查器之外（画布/工具栏）：不要抢焦点
  const outside = { blur() { blurred += 1; } };
  assert.equal(flushFocusedField(outside, container), false);
  assert.equal(blurred, 1);

  // 无焦点 / 不支持 blur / 容器缺失
  assert.equal(flushFocusedField(null, container), false);
  assert.equal(flushFocusedField({}, container), false);
  assert.equal(flushFocusedField(inside, null), false);
});
