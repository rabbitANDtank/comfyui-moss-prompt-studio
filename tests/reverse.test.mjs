import test from "node:test";
import assert from "node:assert/strict";
import {
  parseReversePayload, pickReverseForm, parseReverseRecord,
  reverseDecision, reverseRecord, textHash, collectPromptSubset,
} from "../web/js/editor/reverse.js";

test("parseReversePayload handles string, object and char-array", () => {
  const obj = { sig: "s", prose: "p", draft: "d", width: 1024, height: 1024 };
  assert.deepEqual(parseReversePayload(JSON.stringify(obj)), obj);
  assert.deepEqual(parseReversePayload(obj), obj);
  assert.equal(parseReversePayload(null), null);
  assert.equal(parseReversePayload("not json"), null);
  const chars = [...JSON.stringify(obj)];
  assert.equal(parseReversePayload(chars).sig, "s");
});

test("pickReverseForm maps slots; canvas falls back to draft", () => {
  const payload = { prose: "散文", draft: '{"items":[]}', width: 1024 };
  assert.deepEqual(pickReverseForm(payload, 0), { mode: "prose", text: "散文" });
  assert.deepEqual(pickReverseForm(payload, 1), { mode: "draft", text: '{"items":[]}' });
  assert.deepEqual(pickReverseForm(payload, 2), { mode: "canvas", text: '{"items":[]}' });
  assert.equal(pickReverseForm(payload, 3), null);
  assert.equal(pickReverseForm({ prose: "  " }, 0), null);
  assert.equal(pickReverseForm(null, 0), null);
});

test("same-sig: replay dedupes unchanged; protects split canvas; recovers only when empty", () => {
  const rec = { owner: 7, sig: "a", mode: "canvas", draftHash: textHash("A") };
  // 真机踩坑回归：完整 Queue 重跑同图同参反推，prose 重放不得覆盖已有拆解画布
  assert.equal(reverseDecision(rec, {
    ownerId: 7, sig: "a", mode: "prose", replaceEdits: true, edits: "split-canvas",
  }), "same", "same-sig 的 prose 重放必须跳过：拆解成果在它之上，写回会让画布失配回退");
  assert.equal(reverseDecision(rec, {
    ownerId: 7, sig: "a", mode: "prose", replaceEdits: true, edits: "",
  }), "apply", "画布/结果被清空时允许重放恢复");
  assert.equal(reverseDecision(rec, {
    ownerId: 7, sig: "a", mode: "canvas", replaceEdits: true, edits: "A",
  }), "same", "内容未变的重放直接去重（不再无意义写回）");
  assert.equal(reverseDecision(rec, {
    ownerId: 7, sig: "a", mode: "canvas", replaceEdits: true, edits: "B",
  }), "apply", "显式 replace_edits=true 且画布已变化：允许覆盖");
  assert.equal(reverseDecision(rec, {
    ownerId: 7, sig: "a", mode: "canvas", replaceEdits: false, edits: "A",
  }), "same", "重复事件且内容未变：去重");
  assert.equal(reverseDecision(rec, {
    ownerId: 7, sig: "a", mode: "canvas", replaceEdits: false, edits: "",
  }), "apply", "画布被清空后允许恢复同一结果");
  assert.equal(reverseDecision(rec, {
    ownerId: 7, sig: "a", mode: "canvas", replaceEdits: false, edits: "B",
  }), "edited", "手改后 replace=false 保留手改");
  assert.equal(reverseDecision(rec, {
    ownerId: 8, sig: "a", mode: "canvas", replaceEdits: false, edits: "A",
  }), "edited", "不同 owner 视同首轮，replace=false 时保留现有画布");
});

test("first run with replace_edits=false must not overwrite an existing canvas", () => {
  // record 为空（第一次接反推）且 edits 已有内容 → 保留，并提示 edited
  assert.equal(reverseDecision(null, {
    ownerId: 7, sig: "a", mode: "canvas", replaceEdits: false, edits: "hand-drawn",
  }), "edited");
  assert.equal(reverseDecision(null, {
    ownerId: 7, sig: "a", mode: "canvas", replaceEdits: false, edits: "",
  }), "apply");
  assert.equal(reverseDecision(null, {
    ownerId: 7, sig: "a", mode: "canvas", replaceEdits: true, edits: "hand-drawn",
  }), "apply");
});

test("new sig: unchanged auto canvas can be replaced; hand edits are protected", () => {
  const rec = { owner: 7, sig: "a", mode: "canvas", draftHash: textHash("A") };
  const base = { ownerId: 7, sig: "b", mode: "canvas" };
  assert.equal(reverseDecision(rec, { ...base, replaceEdits: false, edits: "A" }), "apply");
  assert.equal(reverseDecision(rec, { ...base, replaceEdits: false, edits: "B" }), "edited");
  assert.equal(reverseDecision(rec, { ...base, replaceEdits: true, edits: "B" }), "apply");
  // prose/draft 写 source，不走画布保护
  assert.equal(reverseDecision(rec, { ...base, mode: "prose", replaceEdits: false, edits: "B" }), "apply");
});

test("legacy record with full draft still works", () => {
  const legacy = { owner: 7, sig: "a", mode: "canvas", draft: "A" };
  assert.equal(reverseDecision(legacy, {
    ownerId: 7, sig: "a", mode: "canvas", replaceEdits: false, edits: "A",
  }), "same");
  const raw = reverseRecord(12, "sig", "canvas", '{"a":1}');
  const rec = parseReverseRecord(raw);
  assert.equal(String(rec.owner), "12");
  assert.equal(rec.sig, "sig");
  assert.equal(rec.mode, "canvas");
  assert.equal(rec.draft, undefined, "新记录不再保存完整 draft");
  assert.equal(rec.draftHash, textHash('{"a":1}'));
  assert.equal(parseReverseRecord("bad"), null);
});

test("collectPromptSubset keeps the root and its upstream only", () => {
  const prompt = {
    "1": { class_type: "LoadImage", inputs: { image: "x.png" } },
    "2": { class_type: "MOSSImageReverse", inputs: { image: ["1", 0], provider: "mimo" } },
    "3": { class_type: "MOSSEditor", inputs: { source: ["2", 0] } },
    "4": { class_type: "KSampler", inputs: { model: ["9", 0] } },
    "9": { class_type: "CheckpointLoader", inputs: {} },
  };
  assert.deepEqual(Object.keys(collectPromptSubset(prompt, 2)).sort(), ["1", "2"]);
  assert.deepEqual(Object.keys(collectPromptSubset(prompt, "3")).sort(), ["1", "2", "3"]);
  assert.deepEqual(collectPromptSubset(prompt, 99), {});
});

// ---------- F7 same-sig 保护按写入目标判定 ----------

test("prose same-sig: restore only when target source is empty", () => {
  const rec = parseReverseRecord(reverseRecord(7, "a", "prose", "source-A"));
  // 目标 source 被清空：允许同结果重放恢复
  assert.equal(reverseDecision(rec, {
    ownerId: 7, sig: "a", mode: "prose", replaceEdits: true, source: "", edits: "caption-json",
  }), "apply");
  // source 仍有内容：不覆盖
  assert.equal(reverseDecision(rec, {
    ownerId: 7, sig: "a", mode: "prose", replaceEdits: true, source: "source-A", edits: "caption-json",
  }), "same");
  // 旧调用方未传 source：保持旧行为（edits 有内容 → same）
  assert.equal(reverseDecision(rec, {
    ownerId: 7, sig: "a", mode: "prose", replaceEdits: true, edits: "caption-json",
  }), "same");
});

test("draft same-sig: never overwrite hand-edited canvas; restore only both empty", () => {
  const rec = parseReverseRecord(reverseRecord(7, "a", "draft", "DRAFT-A"));
  const base = { ownerId: 7, sig: "a", mode: "draft", replaceEdits: true };
  // 画布已手改（hash != record hash）→ 同图同参重放不得覆盖
  assert.equal(reverseDecision(rec, { ...base, source: "source-A", edits: "HAND" }), "same");
  // source 非空 → 不覆盖
  assert.equal(reverseDecision(rec, { ...base, source: "source-A", edits: "" }), "same");
  // source 空但画布仍是旧 draft（未手改）→ 按“source 与画布都空”规则不恢复
  assert.equal(reverseDecision(rec, { ...base, source: "", edits: "DRAFT-A" }), "same");
  // source 空 + 画布空 → 恢复
  assert.equal(reverseDecision(rec, { ...base, source: "", edits: "" }), "apply");
});
