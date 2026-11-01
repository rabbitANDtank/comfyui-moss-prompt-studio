/** AI 定向改稿前端：JS 镜像 applyOps（共享黄金 fixture 双端对拍）+ wordDiff + 审批卡纯函数。 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { applyOps, wordDiff, EDIT_OP_TYPES, EDIT_SEGMENT_KEYS, MAX_EDIT_OPS }
  from "../web/js/editor/edit_ops.js";
import { opTouchesText, formatOpSummary, diffForOp, decisionForOps, revisionLint }
  from "../web/js/editor/revision_panel.js";
import { sourceSignature } from "../web/js/editor/model.js";

const here = dirname(fileURLToPath(import.meta.url));
const cases = JSON.parse(readFileSync(join(here, "fixtures", "edit_ops_cases.json"), "utf-8"));
const deepCopy = (v) => JSON.parse(JSON.stringify(v));

test("共享黄金 fixture：与 py/edit_ops.apply_ops 逐例一致（双端契约）", () => {
  for (const c of cases) {
    const out = applyOps(deepCopy(c.draft), c.ops, c.scope ?? undefined);
    assert.equal(out.applied.length, c.expect.applied, `${c.name}: applied`);
    assert.equal(out.rejected.length, c.expect.rejected, `${c.name}: rejected`);
    assert.equal(out.dropped, c.expect.dropped, `${c.name}: dropped`);
    assert.equal(out.error, "", `${c.name}: 不应报错`);
    const exp = c.expect;
    if (exp.proposed_equals_draft) assert.deepEqual(out.draft, c.draft, `${c.name}: 全拒收时 proposed=原稿`);
    if (exp.rejected0_reason_contains) assert.ok(out.rejected[0].reason.includes(exp.rejected0_reason_contains), c.name);
    for (const [key, want] of Object.entries(exp)) {
      if (["applied", "rejected", "dropped", "proposed_equals_draft", "rejected0_reason_contains"].includes(key)) continue;
      let got;
      if (key === "items_len") got = out.draft.items.length;
      else if (key.startsWith("items")) got = out.draft.items[+key.split("_", 1)[0].slice(5)][key.split("_", 2)[1]];
      else if (key.startsWith("summary_")) got = out.draft.summary[key.split("_", 2)[1]];
      assert.deepEqual(got, want, `${c.name}: ${key}`);
    }
  }
});

test("指纹永远对照原始草稿：set_caption 后 set_segment 仍按原稿核验", () => {
  const draft = {
    width: 512, height: 512, bbox_coord_space: "abs", bbox_format: "xyxy",
    summary: { caption: "场景。" },
    items: [{ label: "character", caption: "女战士站在中央；姿势：双手叉腰", bbox: [64, 64, 448, 448] }],
  };
  const ops = [
    { op: "set_caption", target: { index: 0, label: "character", head_prefix: "女战士站在中央" }, reason: "改外貌", head: "白发女战士站在中央" },
    { op: "set_segment", target: { index: 0, label: "character", head_prefix: "女战士站在中央" }, reason: "换姿势", segment: "pose", text: "单膝跪地" },
  ];
  const out = applyOps(draft, ops);
  assert.equal(out.applied.length, 2);
  assert.equal(out.draft.items[0].caption, "白发女战士站在中央；姿势：单膝跪地");
});

test("应用后校验失败 → error 字段（不抛错），画布侧不采纳", () => {
  const draft = {
    width: 512, height: 512, bbox_coord_space: "abs", bbox_format: "xyxy",
    summary: { caption: "场景。" },
    items: [{ label: "object", caption: "唯一主体。", bbox: [16, 16, 480, 480] }],
  };
  const out = applyOps(draft, [
    { op: "remove_item", target: { index: 0, label: "object", head_prefix: "唯一主体。" }, reason: "清空" },
  ]);
  assert.ok(out.error.includes("官方校验"), out.error);
});

test("wordDiff：增删保留词级分段", () => {
  const segs = wordDiff("红裙女战士站在左侧", "白发女战士站在左侧，手持长剑");
  const delText = segs.filter((s) => s.type === "del").map((s) => s.text).join("");
  const addText = segs.filter((s) => s.type === "add").map((s) => s.text).join("");
  const sameText = segs.filter((s) => s.type === "same").map((s) => s.text).join("");
  assert.equal(delText, "红裙");                      // CJK 按单字删除，拼接后成词
  assert.equal(addText, "白发，手持长剑");
  assert.equal(sameText, "女战士站在左侧");
  assert.equal(wordDiff("abc", "abc").every((s) => s.type === "same"), true);
  assert.deepEqual(wordDiff("", "新"), [{ type: "add", text: "新" }]);
  assert.deepEqual(wordDiff("旧", ""), [{ type: "del", text: "旧" }]);
  // 超界兜底：整体两段
  const big = "字".repeat(1000);
  const out = wordDiff(big, big + "变");
  assert.equal(out.length, 2);
});

test("opTouchesText：set_text 与带文字的 add_item", () => {
  assert.equal(opTouchesText({ op: "set_text", target: {}, text: "X" }), true);
  assert.equal(opTouchesText({ op: "add_item", item: { label: "text", caption: "c", bbox: [0, 0, 10, 10], text: "T" } }), true);
  assert.equal(opTouchesText({ op: "add_item", item: { label: "object", caption: "c", bbox: [0, 0, 10, 10] } }), false);
  assert.equal(opTouchesText({ op: "set_caption", target: {}, head: "x" }), false);
  assert.equal(opTouchesText(null), false);
});

test("decisionForOps：锁文字强制排除 + 子集应用", () => {
  const base = {
    width: 512, height: 512, bbox_coord_space: "abs", bbox_format: "xyxy",
    summary: { caption: "场景。" },
    items: [
      { label: "object", caption: "主体。", bbox: [16, 16, 240, 240] },
      { label: "text", caption: "标题。", text: "旧标题", bbox: [256, 16, 496, 96] },
    ],
  };
  const payload = {
    ops: [
      { op: "set_caption", target: { index: 0, label: "object", head_prefix: "主体。" }, reason: "改描述", head: "新主体。" },
      { op: "set_text", target: { index: 1, label: "text", head_prefix: "标题。" }, reason: "改标题", text: "新标题" },
    ],
    draft: base, base_draft: base, scope: null,
  };
  const both = decisionForOps(payload, new Set(["0", "1"]));
  assert.ok(both.ok);
  assert.equal(both.draft.items[0].caption, "新主体。");
  assert.equal(both.draft.items[1].text, "新标题");
  // 锁文字对全集（"全部采纳"走的同一路径）与子集同样生效（B1 语义；btnAll DOM 接线属 P1a-3 真机验收）
  const locked = decisionForOps(payload, new Set(["0", "1"]), { lockText: true });
  assert.ok(locked.ok);
  assert.equal(locked.draft.items[1].text, "旧标题", "锁文字时 set_text 被排除");
  assert.equal(locked.draft.items[0].caption, "新主体。", "非文字 op 照常采纳");
  const none = decisionForOps(payload, new Set());
  assert.equal(none.ok, false);
  assert.equal(none.reason, "empty");
});

test("revisionLint：容量与 token 提示", () => {
  const small = {
    width: 1024, height: 1024, bbox_coord_space: "abs", bbox_format: "xyxy",
    summary: { caption: "场景。" },
    items: [{ label: "text", caption: "长标题。", text: "很长的中文标题文字超过小框容量", bbox: [0, 0, 80, 80] }],
  };
  assert.ok(revisionLint(small).some((w) => w.includes("文字容量不足")));
  const bigTokens = {
    width: 1024, height: 1024, bbox_coord_space: "abs", bbox_format: "xyxy",
    summary: { caption: "字".repeat(4500) },
    items: [{ label: "object", caption: "主体。", bbox: [16, 16, 480, 480] }],
  };
  assert.ok(revisionLint(bigTokens).some((w) => w.includes("tokens")));
  assert.deepEqual(revisionLint(null), []);
});

test("formatOpSummary / diffForOp：展示行与 diff 取值", () => {
  const draft = {
    width: 512, height: 512, bbox_coord_space: "abs", bbox_format: "xyxy",
    summary: { caption: "全局旧描述。" },
    items: [
      { label: "character", caption: "红裙女战士站在左侧；姿势：叉腰", bbox: [16, 16, 240, 480] },
      { label: "text", caption: "标题。", text: "旧标题", bbox: [256, 16, 496, 96] },
    ],
  };
  assert.equal(formatOpSummary({ op: "set_summary", fields: { meta: [] } }, draft), "改全局 meta");
  assert.ok(formatOpSummary({ op: "set_caption", target: { index: 0 } }, draft).includes("#0"));
  const df = diffForOp({ op: "set_caption", target: { index: 0 }, head: "白发女战士" }, draft);
  assert.equal(df.oldText, "红裙女战士站在左侧");
  assert.equal(df.newText, "白发女战士");
  const dt = diffForOp({ op: "set_text", target: { index: 1 }, text: "新标题" }, draft);
  assert.equal(dt.oldText, "旧标题");
  assert.equal(diffForOp({ op: "move_resize", target: { index: 0 }, bbox: [0, 0, 10, 10] }, draft), null);
});

test("常量与 py 侧 FNV 黄金对拍（跨语言契约）", () => {
  assert.deepEqual(EDIT_OP_TYPES, ["set_caption", "set_segment", "set_text", "move_resize", "add_item", "remove_item", "set_summary"]);
  assert.deepEqual(EDIT_SEGMENT_KEYS, ["pose", "costume"]);
  assert.equal(MAX_EDIT_OPS, 8);
  assert.equal(sourceSignature(EDIT_OP_TYPES.join(",")), "fnv1a:a3f6b8de");
  assert.equal(sourceSignature(EDIT_SEGMENT_KEYS.join(",")), "fnv1a:39f031be");
});
