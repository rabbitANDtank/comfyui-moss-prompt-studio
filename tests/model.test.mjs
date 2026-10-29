import test from "node:test";
import assert from "node:assert/strict";
import {
  detectKind, reverseCompiled, parseSourceText, validateDraft,
  snapshotOf, serializeDraft, clampBox, resizeDraft, sourceSignature, clampArea,
} from "../web/js/editor/model.js";

const DRAFT = {
  width: 1024, height: 1024,
  bbox_coord_space: "abs", bbox_format: "xyxy",
  summary: { caption: "test scene", meta: ["poster"], canvas: "single" },
  items: [
    { label: "object", caption: "a cup", bbox: [100, 100, 500, 500] },
    { label: "text", caption: "title", text: "HELLO", bbox: [600, 100, 900, 200] },
  ],
};

const COMPILED = {
  summary: { caption: "compiled" },
  items: [{ label: "text", caption: "title", text: "EXPLORE", bbox: [-416, -384, -288, 384] }],
  bbox_coord_space: "abs_centralized", bbox_format: "yxyx",
};

const clone = (v) => structuredClone(v);
const layout = (d) => ({ kind: "layout", d });

test("detectKind: caption / prose / draft / compiled", () => {
  assert.equal(detectKind({ caption: "x" }), "caption");
  assert.equal(detectKind(DRAFT), "draft");
  assert.equal(detectKind(COMPILED), "compiled");
  assert.equal(parseSourceText("一段纯文本散文").kind, "caption");
  assert.equal(parseSourceText(JSON.stringify(DRAFT)).kind, "layout");
});

test("compiled ycyx is rejected instead of silently treated as yxyx", () => {
  const bad = { ...clone(COMPILED), bbox_format: "ycyx" };
  assert.throws(() => parseSourceText(JSON.stringify(bad), 1024, 1024), /yxyx/);
});

test("reverseCompiled: centered yxyx -> top-left xyxy", () => {
  const out = reverseCompiled(clone(COMPILED), 1024, 1024);
  assert.deepEqual(out.items[0].bbox, [128, 96, 896, 224]);
  assert.equal(out.bbox_coord_space, "abs");
  assert.equal(out.bbox_format, "xyxy");
  assert.equal(validateDraft(layout(out)).length, 0);
});

test("reverseCompiled rejects malformed compiled JSON", () => {
  assert.throws(() => reverseCompiled({ items: [1], summary: {} }, 1024, 1024), /items\[0\]/);
  assert.throws(() => reverseCompiled({ items: [] }, 1024, 1024), /summary/);
});

test("validateDraft accepts the official-shape draft and rejects schema drift", () => {
  assert.deepEqual(validateDraft(layout(clone(DRAFT))), []);

  const unknown = clone(DRAFT); unknown.foo = 1;
  assert.match(validateDraft(layout(unknown)).join(";"), /未知的顶层字段/);

  const badMeta = clone(DRAFT); badMeta.summary.meta = "poster";
  assert.match(validateDraft(layout(badMeta)).join(";"), /summary\.meta/);

  const badBox = clone(DRAFT); badBox.items[0].bbox = [0, 0, 9999, 500];
  assert.match(validateDraft(layout(badBox)).join(";"), /bbox x 越界/);

  const badW = clone(DRAFT); badW.width = 1000;
  assert.match(validateDraft(layout(badW)).join(";"), /16 的正倍数/);

  const noText = clone(DRAFT); delete noText.items[1].text;
  assert.match(validateDraft(layout(noText)).join(";"), /text 为空/);

  const strayText = clone(DRAFT); strayText.items[0].text = "oops";
  assert.match(validateDraft(layout(strayText)).join(";"), /不应有 text/);
});

test("serialize/snapshot round-trip", () => {
  const cap = { kind: "caption", caption: "hello" };
  assert.deepEqual(JSON.parse(serializeDraft(cap)), { caption: "hello" });
  assert.equal(snapshotOf(cap), JSON.stringify({ caption: "hello" }));

  const s = layout(clone(DRAFT));
  assert.deepEqual(JSON.parse(serializeDraft(s)), DRAFT);
  assert.deepEqual(JSON.parse(snapshotOf(s)), DRAFT);
});

test("resizeDraft scales and clamps boxes", () => {
  const out = resizeDraft(clone(DRAFT), 512, 512);
  assert.equal(out.width, 512);
  assert.deepEqual(out.items[0].bbox, [50, 50, 250, 250]);
  assert.deepEqual(out.items[1].bbox, [300, 50, 450, 100]);
});

test("clampBox keeps boxes inside the canvas and non-zero", () => {
  assert.deepEqual(clampBox([1020, 100, 1020, 200], 1024, 1024), [1020, 100, 1024, 200]);
  assert.deepEqual(clampBox([0, 0, 0, 0], 1024, 1024), [0, 0, 8, 8]);
  assert.deepEqual(clampBox([-10, -10, 2000, 2000], 1024, 1024), [0, 0, 1024, 1024]);
});

test("sourceSignature matches py.translate.source_signature (FNV-1a UTF-8)", () => {
  // 值由 Python 侧同算法生成；两边必须一致，否则 edits 归属判断永远失效
  assert.equal(sourceSignature("abc"), "fnv1a:1a47e90b");
  assert.equal(sourceSignature("中文 source"), "fnv1a:00c2069a");
  assert.equal(sourceSignature(""), "fnv1a:811c9dc5");
});

test("clampArea keeps preset resizes inside MOSS 0.25M–4M", () => {
  assert.deepEqual(clampArea(15360, 15360), [2000, 2000]);
  const [w, h] = clampArea(64, 64);
  assert.ok(w >= 512 && h >= 512, "过小画布应抬到 0.25M 以上");
  assert.ok(w % 16 === 0 && h % 16 === 0);
  assert.deepEqual(clampArea(1024, 1024), [1024, 1024], "合法面积不变");
  const [w2, h2] = clampArea(4096, 2160);
  assert.ok(w2 * h2 <= 4000000 && w2 % 16 === 0 && h2 % 16 === 0);
});
