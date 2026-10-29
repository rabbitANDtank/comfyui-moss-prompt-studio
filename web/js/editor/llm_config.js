/** 三个 LLM 节点的连接配置读写/同步（纯逻辑，无 DOM 依赖）。 */

export const LLM_CONFIG_KEYS = [
  "provider", "base_url", "model", "api_key", "temperature", "thinking", "timeout",
];

// ⇄同步 复制的字段：不含 api_key。key 会随工作流/历史明文保存，节点间复制只会
// 扩大泄露面；key 的唯一推荐存放位置是 config.json（节点保持 EMPTY）。
export const SYNC_LLM_KEYS = LLM_CONFIG_KEYS.filter((k) => k !== "api_key");

export function isLlmNode(node) {
  const cls = (node?.constructor && node.constructor.comfyClass) || node?.comfyClass;
  return cls === "MOSSEditor" || cls === "MOSSTranslator" || cls === "MOSSImageReverse";
}

export function readLlmConfig(node) {
  const out = {};
  for (const k of LLM_CONFIG_KEYS) {
    const w = (node?.widgets || []).find((x) => x.name === k);
    if (w) out[k] = w.value;
  }
  return out;
}

export function applyLlmConfig(node, values, keys = LLM_CONFIG_KEYS) {
  let n = 0;
  for (const k of keys) {
    if (!values || values[k] === undefined) continue;
    const w = (node?.widgets || []).find((x) => x.name === k);
    if (!w) continue;
    w.value = values[k];
    if (w.inputEl) {
      w.inputEl.value = values[k];
      if (w.inputEl.dispatchEvent && typeof Event !== "undefined") {
        w.inputEl.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
    n++;
  }
  return n;
}

/** 把源节点的连接配置复制到图里其他 MOSS 节点；api_key 不复制。返回被更新的节点数。 */
export function syncLlmConfig(node, graph, app) {
  const values = readLlmConfig(node);
  const nodes = (graph && graph._nodes) || [];
  const targets = nodes.filter((n) => n !== node && isLlmNode(n));
  for (const t of targets) applyLlmConfig(t, values, SYNC_LLM_KEYS);
  try {
    (app || (typeof window !== "undefined" ? window.app : null))?.graph?.setDirtyCanvas?.(true, true);
  } catch (e) { /* noop */ }
  return targets.length;
}

// ---------- api_key 明文保存警告 ----------

export function readApiKey(node) {
  const w = (node?.widgets || []).find((x) => x.name === "api_key");
  return w ? w.value : undefined;
}

/** 非空且不是 EMPTY（含纯空白）即视为明文 key。 */
export function apiKeyNeedsWarning(value) {
  const v = String(value ?? "").trim();
  return !!v && v !== "EMPTY";
}

export const API_KEY_WARNING_TEXT =
  "api_key 会随工作流/执行历史明文保存；建议改回 EMPTY 并把 key 放 config.json";

/**
 * 检查节点是否存了明文 key，需要时通过 show(text) 提示一次。
 * 同一个 key 只提示一次；清空后再填新 key 会重新提示。返回是否提示了。
 */
export function warnApiKeyIfSet(node, show) {
  const key = readApiKey(node);
  if (!apiKeyNeedsWarning(key)) return false;
  const norm = String(key).trim();
  if (node && node._mpsApiKeyWarnedFor === norm) return false;
  if (node) node._mpsApiKeyWarnedFor = norm;
  if (typeof show === "function") {
    try { show(API_KEY_WARNING_TEXT); } catch (e) { /* 展示失败不影响节点 */ }
  } else if (typeof console !== "undefined") {
    console.warn("[MOSS Studio] " + API_KEY_WARNING_TEXT);
  }
  return true;
}

/** 给 api_key widget 挂一次性回调（防重复包装），key 变化时复查。 */
export function bindApiKeyWarn(node, show) {
  const w = (node?.widgets || []).find((x) => x.name === "api_key");
  if (!w || w.__mpsKeyWarnCb) return;
  const prev = w.callback;
  w.callback = function (...args) {
    const r = prev ? prev.apply(this, args) : undefined;
    try { warnApiKeyIfSet(node, show); } catch (e) { /* noop */ }
    return r;
  };
  w.__mpsKeyWarnCb = true;
}
