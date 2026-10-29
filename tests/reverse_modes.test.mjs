import test from "node:test";
import assert from "node:assert/strict";
import {
  DETAIL_WIDGETS, MODE_VALUES, modeValues, modeTemperature, isManual,
  normalizeReverseMode, sanitizeDetailValues, REVERSE_MODES,
  detectBrokenWidgetLayout, migrateBrokenWidgetValues,
} from "../web/js/editor/reverse_modes.js";

test("concise mode disables text and keeps output small", () => {
  const v = modeValues("concise");
  assert.equal(v.max_items, 4);
  assert.equal(v.include_text, false);
  assert.equal(v.caption_detail, "low");
  assert.equal(v.tag_detail, "none");
  assert.equal(v.max_tokens, 8192);
  assert.equal(v.image_detail, "medium_low");
  assert.equal(v.image_max_side, "1024");
  assert.equal(v.thinking, "off");
  assert.equal(v.temperature, 0.1);
});

test("standard matches the first-run settings with fixed token budget", () => {
  const v = modeValues("standard");
  assert.equal(v.max_items, 6);
  assert.equal(v.include_text, true);
  assert.equal(v.caption_detail, "medium_low");
  assert.equal(v.tag_detail, "full");
  assert.equal(v.max_tokens, 65536);
  assert.equal(v.image_detail, "high");
  assert.equal(v.image_max_side, "1536");
  assert.equal(v.thinking, "off");
});

test("detailed raises max_items to 10 and medium_high captions", () => {
  const v = modeValues("detailed");
  assert.equal(v.max_items, 10);
  assert.equal(v.caption_detail, "medium_high");
  assert.equal(v.max_tokens, 65536);
  assert.equal(v.image_max_side, "1728");
  assert.equal(v.thinking, "off");
});

test("temperature follows the selected provider", () => {
  assert.equal(modeTemperature("concise", "deepseek"), 0.1);
  assert.equal(modeTemperature("standard", "mimo"), 0.2);
  assert.equal(modeTemperature("standard", "minimax"), 1.0);
  assert.equal(modeTemperature("detailed", "mimo"), 0.3);
  assert.equal(modeTemperature("nope", "unknown"), 0.2);
});

test("unknown mode falls back to standard; manual is explicit", () => {
  assert.deepEqual(modeValues("nope"), MODE_VALUES.standard);
  assert.equal(isManual("manual"), true);
  assert.equal(isManual("standard"), false);
  for (const name of ["thinking", "temperature", "max_items", "include_text", "caption_detail",
    "tag_detail", "max_tokens", "image_detail", "image_max_side"]) {
    assert.ok(DETAIL_WIDGETS.includes(name), `missing ${name}`);
  }
});

test("legacy workflow value (e.g. max_items 6 in reverse_mode) is normalized to standard", () => {
  // autosave 旧工作流：widgets_values 索引 9 的旧 max_items 被新控件读到 reverse_mode
  assert.equal(normalizeReverseMode(6), "standard");
  assert.equal(normalizeReverseMode(""), "standard");
  assert.equal(normalizeReverseMode("manual"), "manual");
  for (const m of REVERSE_MODES) assert.equal(normalizeReverseMode(m), m);
});

test("custom provider clamps non-manual token budget (local Ollama 32K risk)", () => {
  assert.equal(modeValues("standard", "custom").max_tokens, 16384);
  assert.equal(modeValues("detailed", "custom").max_tokens, 16384);
  assert.equal(modeValues("standard", "deepseek").max_tokens, 65536);
  assert.equal(modeValues("standard", "minimax").max_tokens, 65536);
  assert.equal(modeValues("standard", "mimo").max_tokens, 65536);
});

test("sanitizeDetailValues repairs values shifted by legacy widgets_values", () => {
  const out = sanitizeDetailValues({
    thinking: 42, temperature: "0.9", max_items: "not-a-number",
    include_text: "yes", caption_detail: "short", tag_detail: "weird",
    max_tokens: -5, image_detail: "default", image_max_side: "9999",
  });
  assert.equal(out.thinking, "off");
  assert.equal(out.temperature, 0.9);
  assert.equal(out.max_items, 6);
  assert.equal(out.include_text, true);
  assert.equal(out.caption_detail, "low", "旧 short 别名");
  assert.equal(out.tag_detail, "full");
  assert.equal(out.max_tokens, 0);
  assert.equal(out.image_detail, "high");
  assert.equal(out.image_max_side, "1536");
  // 正常 manual 值原样保留
  const keep = sanitizeDetailValues({
    thinking: "high", temperature: 0.7, max_items: 12, include_text: false,
    caption_detail: "ultra", tag_detail: "basic", max_tokens: 32768,
    image_detail: "low", image_max_side: "1024",
  });
  assert.deepEqual(keep, {
    thinking: "high", temperature: 0.7, max_items: 12, include_text: false,
    caption_detail: "ultra", tag_detail: "basic", max_tokens: 32768,
    image_detail: "low", image_max_side: "1024",
  });
});

test("broken interim layout (reverse_mode at index 9) is detected and remapped", () => {
  const broken = [
    "mimo", "", "m", "EMPTY", 0.3, "off", 180, 0, 0,     // 0-8
    "standard", 6, true, "medium_low", "full", 65536,     // 9-13
    "high", "1536", "SYS", '{"x":1}', "AGENT", "",        // 14-20（末位 DOM）
  ];
  assert.equal(detectBrokenWidgetLayout(broken), true);
  const migrated = migrateBrokenWidgetValues(broken);
  assert.equal(migrated.reverse_mode, "standard");
  assert.equal(migrated.max_items, 6);
  assert.equal(migrated.include_text, true);
  assert.equal(migrated.caption_detail, "medium_low");
  assert.equal(migrated.tag_detail, "full");
  assert.equal(migrated.max_tokens, 65536);
  assert.equal(migrated.image_detail, "high");
  assert.equal(migrated.image_max_side, "1536");
  assert.equal(migrated.system_prompt, "SYS");
  assert.equal(migrated.extra_body, '{"x":1}');
  assert.equal(migrated.agent_md, "AGENT");
  // 当前正确布局（索引 9 是 max_items 数字）不应被误迁移
  const fixed = ["mimo", "", "m", "EMPTY", 0.3, "off", 180, 0, 0, 6,
    "SYS", "", "AGENT", "standard", true, "medium_low", "full", 65536,
    "high", "1536", false, ""];
  assert.equal(detectBrokenWidgetLayout(fixed), false);
  assert.equal(migrateBrokenWidgetValues(fixed), null);
  // 旧的 14 值工作流也不迁移
  assert.equal(detectBrokenWidgetLayout(new Array(14).fill("")), false);
});
