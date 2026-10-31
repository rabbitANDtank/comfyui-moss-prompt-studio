import test from "node:test";
import assert from "node:assert/strict";
import {
  estimateTextFit, lintTextItems, estimatePromptTokens, TOKEN_WARN_LIMIT,
} from "../web/js/editor/text_fit.js";

const textItem = (text, bbox) => ({ label: "text", caption: "示例文字", text, bbox });

test("CJK 按约 1em 估宽，字号受 bbox 高度与 maxFontSize 约束", () => {
  const fit = estimateTextFit(textItem("中文", [0, 0, 200, 120]));
  assert.equal(fit.fontSize, 72); // min(120/1.2=100, 72)
  assert.equal(fit.maxLineWidth, 144); // 2 × 72
  assert.equal(fit.lines, 1);
  assert.equal(fit.ok, true);

  const capped = estimateTextFit(textItem("中文", [0, 0, 600, 600]), { maxFontSize: 40 });
  assert.equal(capped.fontSize, 40);
  assert.equal(capped.maxLineWidth, 80);
});

test("ASCII 按约 0.55em 估宽，空格按 0.35em", () => {
  const fit = estimateTextFit(textItem("Hello", [0, 0, 200, 120]));
  assert.equal(fit.fontSize, 72);
  assert.equal(fit.lines, 1);
  assert.ok(Math.abs(fit.maxLineWidth - 5 * 0.55 * 72) < 1e-9); // 198
  assert.equal(fit.ok, true);

  const spaced = estimateTextFit(textItem("AB CD", [0, 0, 200, 120]));
  const expected = (4 * 0.55 + 0.35) * 72; // 183.6
  assert.ok(Math.abs(spaced.maxLineWidth - expected) < 1e-9);
});

test("合适框 pass；小框因行高溢出 fail", () => {
  const ok = estimateTextFit(textItem("中文标题", [0, 0, 400, 120]));
  assert.equal(ok.ok, true);
  assert.equal(ok.overflowX, false);
  assert.equal(ok.overflowY, false);

  const bad = estimateTextFit(textItem("中文标题内容", [0, 0, 80, 80]));
  assert.equal(bad.ok, false);
  assert.equal(bad.overflowY, true);
});

test("单行超宽（字符本身宽于框宽）fail", () => {
  const fit = estimateTextFit(textItem("宽", [0, 0, 10, 40]));
  assert.equal(fit.ok, false);
  assert.equal(fit.overflowX, true);
  assert.equal(fit.overflowY, false);
});

test("多行换行按框宽逐字折行，行数正确", () => {
  const fit = estimateTextFit(textItem("ABCDEFGHIJ", [0, 0, 200, 200]));
  // 每字符 39.6px，每行 5 个（198px），共 2 行；2 × 86.4 = 172.8 <= 200
  assert.equal(fit.lines, 2);
  assert.equal(fit.maxLineWidth, 198);
  assert.equal(fit.ok, true);

  const explicit = estimateTextFit(textItem("上\n下", [0, 0, 200, 200]));
  assert.equal(explicit.lines, 2);
  assert.equal(explicit.ok, true);
});

test("空 text / 非 text / 非法 bbox 返回 null", () => {
  assert.equal(estimateTextFit(textItem("", [0, 0, 200, 120])), null);
  assert.equal(estimateTextFit(textItem("   ", [0, 0, 200, 120])), null);
  assert.equal(estimateTextFit({ label: "object", caption: "x", bbox: [0, 0, 200, 120] }), null);
  assert.equal(estimateTextFit(textItem("中文", [0, 0, 200])), null);
  assert.equal(estimateTextFit(textItem("中文", [0, 0, 0, 100])), null);
  assert.equal(estimateTextFit(null), null);
});

test("lintTextItems 只报告容量不足的 text item，空 text 跳过", () => {
  const draft = {
    items: [
      { label: "text", caption: "ok", text: "中文", bbox: [0, 0, 200, 120] },
      { label: "object", caption: "obj", bbox: [0, 0, 100, 100] },
      { label: "text", caption: "empty", text: "", bbox: [0, 0, 100, 100] },
      { label: "text", caption: "bad", text: "很长的中文标题文字", bbox: [0, 0, 80, 80] },
    ],
  };
  const out = lintTextItems(draft);
  assert.equal(out.length, 1);
  assert.equal(out[0].index, 3);
  assert.match(out[0].message, /items\[3\]/);
  assert.match(out[0].message, /框高/);
  assert.deepEqual(lintTextItems(null), []);
  assert.deepEqual(lintTextItems({}), []);
});

test("lintTextItems 对单行超宽给出可读提示", () => {
  const out = lintTextItems({ items: [textItem("宽", [0, 0, 10, 40])] });
  assert.equal(out.length, 1);
  assert.match(out[0].message, /单行超宽/);
});

test("estimatePromptTokens：CJK 1 token/字，其余约 4 字符/token", () => {
  assert.equal(TOKEN_WARN_LIMIT, 4000);
  assert.equal(estimatePromptTokens(""), 0);
  assert.equal(estimatePromptTokens(null), 0);
  assert.equal(estimatePromptTokens("中文"), 2);
  assert.equal(estimatePromptTokens("abcd"), 1);
  assert.equal(estimatePromptTokens("abcde"), 2);
  assert.equal(estimatePromptTokens("中文abcd"), 3);
  assert.equal(estimatePromptTokens("中abcde"), 3);
  assert.equal(estimatePromptTokens("，。"), 2); // 全角标点按 CJK 计
  assert.equal(estimatePromptTokens("中".repeat(4001)), 4001);
  assert.ok(Number.isInteger(estimatePromptTokens("mixed 中文 abc")));
});
