import test from "node:test";
import assert from "node:assert/strict";
import {
  LLM_OPTION_LEFT, LLM_OPTION_RIGHT, LLM_OPTION_FULL, LLM_OPTION_NAMES,
  LLM_OPTION_SPEC, widgetKind, normalizeComboOptions, parseNumberInput, inputConnected,
} from "../web/js/editor/llm_options.js";

test("双列字段顺序符合约定：左列 5 项、右列 4 项、ai_prompt 全宽", () => {
  assert.deepEqual([...LLM_OPTION_LEFT], ["ai_draft", "provider", "base_url", "model", "api_key"]);
  assert.deepEqual([...LLM_OPTION_RIGHT], ["temperature", "thinking", "timeout", "agent_md"]);
  assert.deepEqual([...LLM_OPTION_FULL], ["ai_prompt"]);
  assert.equal(LLM_OPTION_NAMES.length, 10);
  for (const name of LLM_OPTION_NAMES) assert.ok(LLM_OPTION_SPEC[name], `缺少 ${name} 的配置`);
});

test("widgetKind 把 ComfyUI widget 类型归到面板控件", () => {
  assert.equal(widgetKind({ type: "toggle" }), "toggle");
  assert.equal(widgetKind({ type: "boolean" }), "toggle");
  assert.equal(widgetKind({ type: "combo" }), "combo");
  assert.equal(widgetKind({ type: "number" }), "number");
  assert.equal(widgetKind({ type: "int" }), "number");
  assert.equal(widgetKind({ type: "float" }), "number");
  assert.equal(widgetKind({ type: "customtext" }), "textarea");
  assert.equal(widgetKind({ type: "text" }), "text");
  assert.equal(widgetKind(null), "text");
});

test("normalizeComboOptions 去空、去重、保留当前值", () => {
  assert.deepEqual(normalizeComboOptions(["mimo", "", "deepseek", "mimo"], "deepseek"), ["mimo", "deepseek"]);
  assert.deepEqual(normalizeComboOptions(["a", "b"], "c"), ["a", "b", "c"]);
  assert.deepEqual(normalizeComboOptions(["a", "b"], ""), ["a", "b"]);
  assert.deepEqual(normalizeComboOptions(null, ""), []);
  assert.deepEqual(normalizeComboOptions(["(默认 AGENT.md)", "(不注入)", ""], "(默认 AGENT.md)"), ["(默认 AGENT.md)", "(不注入)"]);
});

test("parseNumberInput 处理空值、非法值、min/max 夹取", () => {
  assert.equal(parseNumberInput("", { min: 0, max: 2 }), null);
  assert.equal(parseNumberInput(null, {}), null);
  assert.equal(parseNumberInput("abc", {}), null);
  assert.equal(parseNumberInput("0.3", { min: 0, max: 2 }), 0.3);
  assert.equal(parseNumberInput("-1", { min: 0, max: 2 }), 0);
  assert.equal(parseNumberInput("9", { min: 0, max: 2 }), 2);
  assert.equal(parseNumberInput("180", { min: 10, max: 1200 }), 180);
  assert.equal(parseNumberInput("0.1+0.2", {}), null);
});

test("inputConnected：连线优先 → 面板字段禁用（连接态决策）", () => {
  // 普通 slot：link 非空 = 连线提供值
  assert.equal(inputConnected([{ name: "provider", link: 7 }], "provider"), true);
  assert.equal(inputConnected([{ name: "provider", link: null }], "provider"), false);
  assert.equal(inputConnected([{ name: "provider" }], "provider"), false);
  // 新版前端把 widget 挂在 input.widget 上
  assert.equal(inputConnected([{ name: "provider", widget: { name: "model" }, link: 3 }], "model"), true);
  assert.equal(inputConnected([{ name: "provider", widget: { name: "model" }, link: null }], "model"), false);
  // 不同字段 / 空输入 / 畸形输入不误判
  assert.equal(inputConnected([{ name: "provider", link: 7 }], "model"), false);
  assert.equal(inputConnected(null, "provider"), false);
  assert.equal(inputConnected([null, undefined], "provider"), false);
  assert.equal(inputConnected([{ name: "provider", link: 0 }], "provider"), true, "link=0 也是有效连线 id");
});
