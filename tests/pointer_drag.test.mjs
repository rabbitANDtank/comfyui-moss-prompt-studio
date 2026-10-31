import test from "node:test";
import assert from "node:assert/strict";
import { bindPointerDrag, DRAG_END_EVENTS } from "../web/js/editor/pointer_drag.js";

function fakeTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(fn); },
    removeEventListener(type, fn) {
      const arr = listeners.get(type);
      if (arr) listeners.set(type, arr.filter((x) => x !== fn));
    },
    dispatch(type, ev = {}) { for (const fn of [...(listeners.get(type) || [])]) fn(ev); },
    count(type) { return (listeners.get(type) || []).length; },
  };
}

test("bindPointerDrag cleans up on pointercancel and lostpointercapture, not just pointerup", () => {
  assert.deepEqual([...DRAG_END_EVENTS], ["pointerup", "pointercancel", "lostpointercapture"]);

  for (const endType of ["pointerup", "pointercancel", "lostpointercapture"]) {
    const target = fakeTarget();
    let moves = 0;
    let ends = 0;
    bindPointerDrag(target, { onMove: () => { moves += 1; }, onEnd: () => { ends += 1; } });

    target.dispatch("pointermove", { x: 1 });
    assert.equal(moves, 1);
    target.dispatch(endType, { type: endType });
    assert.equal(ends, 1, `${endType} 必须结束拖拽`);
    assert.equal(target.count("pointermove"), 0, `${endType} 后应移除 pointermove（避免 hover 继续改尺寸）`);
    for (const type of DRAG_END_EVENTS) assert.equal(target.count(type), 0, `${endType} 后应移除 ${type}`);

    // 结束后再来的事件不应重复回调
    target.dispatch("pointerup", {});
    assert.equal(ends, 1);
    assert.equal(moves, 1);
  }
});

test("bindPointerDrag cleanup can be called early", () => {
  const target = fakeTarget();
  let ends = 0;
  const cleanup = bindPointerDrag(target, { onMove() {}, onEnd: () => { ends += 1; } });
  cleanup();
  cleanup();
  assert.equal(target.count("pointermove"), 0);
  target.dispatch("pointerup", {});
  assert.equal(ends, 0);
});
