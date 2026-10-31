import test from "node:test";
import assert from "node:assert/strict";
import {
  prepareDraftWriteback, commitDraftWriteback,
} from "../web/js/editor/result_writeback.js";

const DRAFT = {
  width: 1024, height: 1024,
  bbox_coord_space: "abs", bbox_format: "xyxy",
  summary: { caption: "test scene", meta: [], palette: [] },
  items: [
    { label: "object", caption: "a cup", text: "CUP", bbox: [100, 100, 500, 500] },
  ],
};
const OK = JSON.stringify(DRAFT);

test("prepareDraftWriteback accepts official draft JSON and rejects bad payloads", () => {
  const out = prepareDraftWriteback(OK);
  assert.equal(out.text, OK);
  assert.deepEqual(out.draft, DRAFT);

  assert.throws(() => prepareDraftWriteback("{not json"), /JSON 解析失败/);
  assert.throws(() => prepareDraftWriteback('{"caption":"just prose"}'), /不是布局草稿/);
  assert.throws(() => prepareDraftWriteback(""), /结果为空/);

  // 结构非法（bbox 越界）在解析阶段就拒绝，不进入写回
  const bad = structuredClone(DRAFT);
  bad.items[0].bbox = [0, 0, 9999, 500];
  assert.throws(() => prepareDraftWriteback(JSON.stringify(bad)), /bbox x 越界/);
});

test("invalid JSON never touches the edits widget (parse before write-back)", () => {
  const edits = { value: "OLD-CANVAS" };
  let setCalls = 0;
  let rebuildCalls = 0;
  const res = commitDraftWriteback({
    raw: "{broken",
    getEdits: () => edits.value,
    setEdits: (v) => { setCalls += 1; edits.value = v; },
    rebuild: () => { rebuildCalls += 1; return true; },
  });
  assert.equal(res.ok, false);
  assert.match(res.error, /JSON 解析失败/);
  assert.equal(setCalls, 0, "解析失败时不得写 edits widget");
  assert.equal(rebuildCalls, 0, "解析失败时不得重建画布");
  assert.equal(edits.value, "OLD-CANVAS", "旧画布/旧 edits 保持不变");
});

test("structurally invalid draft also leaves the widget unchanged", () => {
  const bad = structuredClone(DRAFT);
  bad.items[0].bbox = [-1, 0, 500, 500];
  const edits = { value: "OLD-CANVAS" };
  let setCalls = 0;
  const res = commitDraftWriteback({
    raw: JSON.stringify(bad),
    getEdits: () => edits.value,
    setEdits: (v) => { setCalls += 1; edits.value = v; },
    rebuild: () => true,
  });
  assert.equal(res.ok, false);
  assert.equal(setCalls, 0);
  assert.equal(edits.value, "OLD-CANVAS");
});

test("valid result writes edits once and rebuilds with the parsed draft", () => {
  const edits = { value: "OLD-CANVAS" };
  const rebuildArgs = [];
  const res = commitDraftWriteback({
    raw: OK,
    getEdits: () => edits.value,
    setEdits: (v) => { edits.value = v; },
    rebuild: (prepared) => { rebuildArgs.push(prepared.draft); return true; },
  });
  assert.equal(res.ok, true);
  assert.equal(edits.value, OK);
  assert.deepEqual(rebuildArgs, [DRAFT]);
});

test("rebuild failure rolls the edits widget back to the previous canvas", () => {
  const edits = { value: "OLD-CANVAS" };
  const res = commitDraftWriteback({
    raw: OK,
    getEdits: () => edits.value,
    setEdits: (v) => { edits.value = v; },
    rebuild: () => false,
  });
  assert.equal(res.ok, false);
  assert.equal(res.rolledBack, true);
  assert.equal(edits.value, "OLD-CANVAS");
});
