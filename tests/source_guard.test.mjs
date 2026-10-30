import test from "node:test";
import assert from "node:assert/strict";
import {
  isStaleDraftSource, editsOwnerSignature, draftSourceAttribution, rebuildCandidates,
} from "../web/js/editor/source_guard.js";

// P0-1 回归：异步拆解/注入结果必须绑定提交时的 source，不能把旧画布盖成新 source。

test("isStaleDraftSource: 无提交记录时保持旧行为（接受）", () => {
  assert.equal(isStaleDraftSource("fnv1a:aaaa", []), false);
  assert.equal(isStaleDraftSource("fnv1a:aaaa", ""), false);
  assert.equal(isStaleDraftSource("fnv1a:aaaa", undefined), false);
  assert.equal(isStaleDraftSource("fnv1a:aaaa", [""]), false);
});

test("isStaleDraftSource: source 已变则结果作废", () => {
  assert.equal(isStaleDraftSource("fnv1a:bbbb", ["fnv1a:aaaa"]), true);
  assert.equal(isStaleDraftSource("", ["fnv1a:aaaa"]), true);
});

test("isStaleDraftSource: source 未变则接受", () => {
  assert.equal(isStaleDraftSource("fnv1a:aaaa", ["fnv1a:aaaa"]), false);
  assert.equal(isStaleDraftSource(" fnv1a:aaaa ", ["fnv1a:aaaa"]), false);
});

test("isStaleDraftSource: 多个待回结果中任一匹配当前 source 即可接受", () => {
  assert.equal(isStaleDraftSource("fnv1a:bbbb", ["fnv1a:aaaa", "fnv1a:bbbb"]), false);
  assert.equal(isStaleDraftSource("fnv1a:cccc", ["fnv1a:aaaa", "fnv1a:bbbb"]), true);
  assert.equal(isStaleDraftSource("fnv1a:bbbb", ["", "fnv1a:bbbb", null]), false);
});

test("editsOwnerSignature: 沿用画布生成时的 source 签名，不被新 source 覆盖", () => {
  // 旧画布（A）晚到时 source 已是 B：写入必须仍记为 A，后端据此忽略旧画布
  assert.equal(editsOwnerSignature("fnv1a:aaaa", "fnv1a:bbbb"), "fnv1a:aaaa");
  // 画布就是当前 source 生成的：记录当前签名
  assert.equal(editsOwnerSignature("fnv1a:bbbb", "fnv1a:bbbb"), "fnv1a:bbbb");
  // 旧工作流/初始状态没有归属：回落到当前 source（保持旧行为）
  assert.equal(editsOwnerSignature("", "fnv1a:bbbb"), "fnv1a:bbbb");
  assert.equal(editsOwnerSignature(undefined, "fnv1a:bbbb"), "fnv1a:bbbb");
  assert.equal(editsOwnerSignature(null, "fnv1a:bbbb"), "fnv1a:bbbb");
});

// ---------- F3 结果自带 source 归属 ----------

test("draftSourceAttribution: uiSig 优先，缺失时回退 pending", () => {
  // 后端签名匹配/不匹配（即使 pending 不同，也以后端为准）
  assert.equal(draftSourceAttribution({ uiSig: "fnv1a:aaaa", currentSig: "fnv1a:aaaa", pendingSig: "fnv1a:bbbb" }), "accept");
  assert.equal(draftSourceAttribution({ uiSig: "fnv1a:aaaa", currentSig: "fnv1a:bbbb", pendingSig: "fnv1a:bbbb" }), "stale");
  // ws 字符数组归一
  assert.equal(draftSourceAttribution({ uiSig: ["fnv1a:a", "aaa"], currentSig: "fnv1a:aaaa", pendingSig: "" }), "accept");
  // 缺失 uiSig → pending 兜底
  assert.equal(draftSourceAttribution({ currentSig: "fnv1a:bbbb", pendingSig: "fnv1a:aaaa" }), "stale");
  assert.equal(draftSourceAttribution({ currentSig: "fnv1a:aaaa", pendingSig: "fnv1a:aaaa" }), "accept");
  assert.equal(draftSourceAttribution({ currentSig: "fnv1a:aaaa", pendingSig: "" }), "accept");
  assert.equal(draftSourceAttribution({ currentSig: "fnv1a:aaaa" }), "accept");
  assert.equal(draftSourceAttribution({}), "accept");
});

// ---------- F8 重建候选选择 ----------

test("rebuildCandidates: preferEdits / owner 校验 / 回退顺序", () => {
  const src = "SRC", edits = "EDITS";
  let r = rebuildCandidates({ preferEdits: true, src, edits, srcSig: "s", storedSig: "s" });
  assert.equal(r.editsValid, true);
  assert.deepEqual(r.candidates, [
    { text: "EDITS", fromEdits: true },
    { text: "SRC", fromEdits: false },
  ]);

  // owner 不匹配：当前 source 的有效画布不能被当作可接管候选
  r = rebuildCandidates({ preferEdits: true, src, edits, srcSig: "s", storedSig: "old" });
  assert.equal(r.editsValid, false);
  assert.deepEqual(r.candidates, [{ text: "SRC", fromEdits: false }]);

  // preferEdits=false：source 优先
  r = rebuildCandidates({ preferEdits: false, src, edits, srcSig: "s", storedSig: "s" });
  assert.deepEqual(r.candidates, [{ text: "SRC", fromEdits: false }]);

  // source 为空：edits 是唯一来源
  r = rebuildCandidates({ preferEdits: false, src: "", edits, srcSig: "", storedSig: "" });
  assert.deepEqual(r.candidates, [{ text: "EDITS", fromEdits: true }]);

  // 旧工作流没有归属签名：按有效处理
  r = rebuildCandidates({ preferEdits: true, src, edits, srcSig: "s", storedSig: "" });
  assert.equal(r.editsValid, true);

  // 无内容
  r = rebuildCandidates({ preferEdits: true, src: "", edits: "", srcSig: "", storedSig: "" });
  assert.deepEqual(r.candidates, []);
});
