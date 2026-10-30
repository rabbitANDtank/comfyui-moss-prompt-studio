import test from "node:test";
import assert from "node:assert/strict";
import {
  LLM_CONFIG_KEYS, SYNC_LLM_KEYS, isLlmNode, readLlmConfig, applyLlmConfig,
  syncLlmConfig, apiKeyNeedsWarning, warnApiKeyIfSet, API_KEY_WARNING_TEXT,
} from "../web/js/editor/llm_config.js";

function mkNode(cls, values = {}) {
  const widgets = Object.entries(values).map(([name, value]) => ({
    name,
    value,
    inputEl: { value, dispatchEvent() {} },
  }));
  return { comfyClass: cls, constructor: { comfyClass: cls }, widgets };
}

const FULL = {
  provider: "mimo",
  base_url: "https://mimo.example/v1",
  model: "mimo-v2.6-flash",
  api_key: "test-key",
  temperature: 0.3,
  thinking: "off",
  timeout: 180,
};

test("readLlmConfig reads the seven connection fields", () => {
  const node = mkNode("MOSSEditor", FULL);
  assert.deepEqual(readLlmConfig(node), FULL);
  assert.equal(LLM_CONFIG_KEYS.length, 7);
});

test("applyLlmConfig writes values and dispatches input once per widget", () => {
  let fired = 0;
  const node = mkNode("MOSSTranslator", FULL);
  for (const w of node.widgets) w.inputEl.dispatchEvent = () => { fired += 1; };
  const n = applyLlmConfig(node, {
    provider: "minimax", base_url: "https://api.minimax.cn/v1",
    model: "MiniMax-M3.1-Flash", api_key: "EMPTY",
    temperature: 0.2, thinking: "off", timeout: 300,
  });
  assert.equal(n, 7);
  assert.equal(node.widgets.find((w) => w.name === "provider").value, "minimax");
  assert.equal(node.widgets.find((w) => w.name === "model").value, "MiniMax-M3.1-Flash");
  assert.equal(fired, 7);
});

test("applyLlmConfig skips fields the target does not have", () => {
  const node = mkNode("MOSSEditor", { provider: "mimo" });
  const n = applyLlmConfig(node, FULL);
  assert.equal(n, 1);
  assert.equal(node.widgets[0].value, "mimo");
});

test("isLlmNode only accepts the three MOSS LLM nodes", () => {
  assert.equal(isLlmNode(mkNode("MOSSEditor")), true);
  assert.equal(isLlmNode(mkNode("MOSSTranslator")), true);
  assert.equal(isLlmNode(mkNode("MOSSImageReverse")), true);
  assert.equal(isLlmNode(mkNode("MOSSCompiler")), false);
  assert.equal(isLlmNode(mkNode("KSampler")), false);
});

test("syncLlmConfig copies connection fields but never api_key", () => {
  const src = mkNode("MOSSImageReverse", FULL);
  const editor = mkNode("MOSSEditor", { ...FULL, provider: "deepseek", model: "old" });
  const translator = mkNode("MOSSTranslator", { ...FULL, provider: "deepseek", model: "old" });
  const sampler = mkNode("KSampler", { steps: 25 });
  const graph = { _nodes: [src, editor, translator, sampler] };
  const n = syncLlmConfig(src, graph, null);
  assert.equal(n, 2);
  assert.ok(!SYNC_LLM_KEYS.includes("api_key"), "SYNC_LLM_KEYS 不得包含 api_key");
  for (const target of [editor, translator]) {
    assert.equal(target.widgets.find((w) => w.name === "provider").value, "mimo");
    assert.equal(target.widgets.find((w) => w.name === "model").value, "mimo-v2.6-flash");
    // key 不随 ⇄同步 复制：目标保留自己的值
    assert.equal(target.widgets.find((w) => w.name === "api_key").value, "test-key");
  }
  assert.equal(sampler.widgets[0].value, 25);
});

test("applyLlmConfig honours an explicit key list (sync path excludes api_key)", () => {
  const node = mkNode("MOSSTranslator", FULL);
  const n = applyLlmConfig(node, { ...FULL, api_key: "test-key-should-not-apply" }, SYNC_LLM_KEYS);
  assert.equal(n, 6, "只应用 6 个非 key 字段");
  assert.equal(node.widgets.find((w) => w.name === "api_key").value, "test-key", "key 保持原值");
  // 目标缺少的字段自动跳过
  const sparse = mkNode("MOSSTranslator", { provider: "mimo", api_key: "EMPTY" });
  assert.equal(applyLlmConfig(sparse, FULL, SYNC_LLM_KEYS), 1);
  assert.equal(sparse.widgets.find((w) => w.name === "api_key").value, "EMPTY");
});

test("apiKeyNeedsWarning flags non-empty non-EMPTY keys", () => {
  assert.equal(apiKeyNeedsWarning("test-key"), true);
  assert.equal(apiKeyNeedsWarning("  test-key  "), true);
  assert.equal(apiKeyNeedsWarning("EMPTY"), false);
  assert.equal(apiKeyNeedsWarning(""), false);
  assert.equal(apiKeyNeedsWarning("   "), false);
  assert.equal(apiKeyNeedsWarning(undefined), false);
});

test("warnApiKeyIfSet warns once per key and re-arms on change", () => {
  const seen = [];
  const node = mkNode("MOSSEditor", { api_key: "test-key-one" });
  assert.equal(warnApiKeyIfSet(node, (t) => seen.push(t)), true);
  assert.deepEqual(seen, [API_KEY_WARNING_TEXT]);
  assert.equal(warnApiKeyIfSet(node, (t) => seen.push(t)), false, "同一个 key 不重复提示");
  node.widgets.find((w) => w.name === "api_key").value = "EMPTY";
  assert.equal(warnApiKeyIfSet(node, (t) => seen.push(t)), false, "EMPTY 不提示");
  node.widgets.find((w) => w.name === "api_key").value = "test-key-two";
  assert.equal(warnApiKeyIfSet(node, (t) => seen.push(t)), true, "换了新 key 重新提示");
  assert.equal(seen.length, 2);
  // 无 show 回调时走 console.warn，不抛错
  assert.equal(warnApiKeyIfSet(mkNode("MOSSTranslator", { api_key: "test-key-x" })), true);
});
