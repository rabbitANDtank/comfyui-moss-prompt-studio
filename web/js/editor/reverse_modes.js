/** 反推模式：前端显隐/预设值。与 nodes/image_reverse.py 的 MODE_PRESETS 保持同步。 */

export const DETAIL_WIDGETS = [
  "thinking", "temperature", "max_items", "include_text", "caption_detail",
  "tag_detail", "max_tokens", "image_detail", "image_max_side",
];

export const REVERSE_MODES = ["concise", "standard", "detailed", "manual"];

// 旧（修正前）版本的控件顺序：reverse_mode 被插在 canvas_height 与 max_items 之间。
// 若用户用那个版本保存过工作流，widgets_values 会按这个顺序排布，需要迁移。
export const BROKEN_REVERSE_WIDGET_ORDER = [
  "provider", "base_url", "model", "api_key", "temperature", "thinking",
  "timeout", "canvas_width", "canvas_height", "reverse_mode", "max_items",
  "include_text", "caption_detail", "tag_detail", "max_tokens", "image_detail",
  "image_max_side", "system_prompt", "extra_body", "agent_md",
];

/**
 * 识别“修正前新版”保存的 widgets_values：索引 9 是合法模式、索引 10 是数字。
 * 当前/更旧版本的这两种形态都不会同时满足（当前版本索引 9 是 max_items 数字）。
 */
export function detectBrokenWidgetLayout(values) {
  if (!Array.isArray(values) || values.length < 20) return false;
  return REVERSE_MODES.includes(values[9]) && typeof values[10] === "number";
}

/** 把 broken 顺序的值映射为控件名；非 broken 返回 null。 */
export function migrateBrokenWidgetValues(values) {
  if (!detectBrokenWidgetLayout(values)) return null;
  const out = {};
  BROKEN_REVERSE_WIDGET_ORDER.forEach((name, i) => { out[name] = values[i]; });
  return out;
}

export const MODE_VALUES = {
  concise: {
    max_items: 4, include_text: false, caption_detail: "low",
    tag_detail: "none", max_tokens: 8192, image_detail: "medium_low",
    image_max_side: "1024", thinking: "off", temperature: 0.1,
  },
  standard: {
    max_items: 6, include_text: true, caption_detail: "medium_low",
    tag_detail: "full", max_tokens: 65536, image_detail: "high",
    image_max_side: "1536", thinking: "off", temperature: 0.2,
  },
  detailed: {
    max_items: 10, include_text: true, caption_detail: "medium_high",
    tag_detail: "full", max_tokens: 65536, image_detail: "high",
    image_max_side: "1728", thinking: "off", temperature: 0.3,
  },
};

export const MODE_TEMPERATURES = {
  concise: { deepseek: 0.1, mimo: 0.1, minimax: 1.0, custom: 0.2 },
  standard: { deepseek: 0.2, mimo: 0.2, minimax: 1.0, custom: 0.3 },
  detailed: { deepseek: 0.3, mimo: 0.3, minimax: 1.0, custom: 0.3 },
};

export const CAPTION_DETAIL_OPTIONS = [
  "off", "low", "medium_low", "medium", "medium_high", "high", "ultra",
];
export const TAG_DETAIL_OPTIONS = ["none", "basic", "full"];
export const IMAGE_DETAIL_OPTIONS = [
  "low", "medium_low", "medium", "medium_high", "high", "ultra",
];
export const IMAGE_MAX_SIDE_OPTIONS = [
  "512", "720", "1024", "1232", "1424", "1536", "1728", "2000",
];
const THINKING_OPTIONS = ["off", "low", "medium", "high"];
const CAPTION_ALIASES = { short: "low", long: "medium_high" };

/** 旧工作流/异常值 → 合法模式；无效值统一回退 standard。 */
export function normalizeReverseMode(mode) {
  return REVERSE_MODES.includes(mode) ? mode : "standard";
}

export function isManual(mode) {
  return normalizeReverseMode(mode) === "manual";
}

export function modeValues(mode, provider) {
  const key = normalizeReverseMode(mode);
  const v = { ...(MODE_VALUES[key] || MODE_VALUES.standard) };
  // 本地 Ollama 常见 32K 上下文：custom 非 manual 的输出预算压低到 16384，
  // 与 nodes/image_reverse.py 的 resolve_reverse_options 保持一致。
  if (provider === "custom" && key !== "manual" && v.max_tokens > 16384) {
    v.max_tokens = 16384;
  }
  return v;
}

export function modeTemperature(mode, provider) {
  const key = normalizeReverseMode(mode);
  const table = MODE_TEMPERATURES[key] || MODE_TEMPERATURES.standard;
  return table[provider] ?? (MODE_VALUES[key] || MODE_VALUES.standard).temperature;
}

function clampNumber(v, lo, hi, fallback) {
  const n = Number(v);
  return Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : fallback;
}

/**
 * 进入 manual 前清洗控件值：旧工作流升级后，新控件位置可能拿到别的控件留下的
 * 字符串/数字（例如 reverse_mode 拿到旧 max_items=6）。manual 只接受合法值，
 * 不合法则回退 standard 预设，避免把垃圾值提交给后端。
 */
export function sanitizeDetailValues(values = {}) {
  const v = values || {};
  const preset = MODE_VALUES.standard;
  const capRaw = String(v.caption_detail ?? "");
  const cap = CAPTION_ALIASES[capRaw] || capRaw;
  const side = String(v.image_max_side ?? "");
  return {
    thinking: THINKING_OPTIONS.includes(v.thinking) ? v.thinking : preset.thinking,
    temperature: clampNumber(v.temperature, 0, 2, preset.temperature),
    max_items: Math.round(clampNumber(v.max_items, 1, 30, preset.max_items)),
    include_text: typeof v.include_text === "boolean" ? v.include_text : preset.include_text,
    caption_detail: CAPTION_DETAIL_OPTIONS.includes(cap) ? cap : preset.caption_detail,
    tag_detail: TAG_DETAIL_OPTIONS.includes(v.tag_detail) ? v.tag_detail : preset.tag_detail,
    max_tokens: Math.round(clampNumber(v.max_tokens, 0, 524288, 0)),
    image_detail: IMAGE_DETAIL_OPTIONS.includes(v.image_detail) ? v.image_detail : preset.image_detail,
    image_max_side: IMAGE_MAX_SIDE_OPTIONS.includes(side) ? side : preset.image_max_side,
  };
}
