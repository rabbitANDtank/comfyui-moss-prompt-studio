/** 姿势/服装注入 ↔ Editor 的纯逻辑（无 DOM，便于 node --test 覆盖）。
 * POSE_MARKER / COSTUME_MARKER / POSE_MODES / COSTUME_MODES 与后端
 * py/translate.py 的同名常量保持一致。 */
import { collectPromptSubset } from "./reverse.js";
import { OCCL_FRONT, OCCL_BACK } from "./model.js";

export const POSE_MARKER = "；姿势：";
export const POSE_MODES = ["自动", "强制双人", "关闭"];
export const COSTUME_MARKER = "；服装：";
export const COSTUME_MODES = ["自动", "关闭"];
export const COSTUME_SENSITIVE_CATEGORIES = ["内衣", "性感", "泳装"];
// 与后端 _COSTUME_OBJECT_KEYWORDS 保持一致：用于提示“画布里已有旧服装措辞”。
export const COSTUME_KEYWORDS = [
  "连衣裙", "半身裙", "短裙", "长裙", "短裤", "长裤", "背心", "吊带",
  "束身衣", "大衣", "外套", "衬衫", "上衣", "裙", "裤", "鞋", "袜",
  "手套", "领结", "文胸", "内裤", "泳装", "比基尼",
];

// caption 分段顺序：姿势在前、服装在后（与后端 _CAPTION_SEGMENTS 一致）。
const SEGMENTS = [["pose", POSE_MARKER], ["costume", COSTUME_MARKER]];
const escapeRe = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const segmentRe = () => new RegExp(`(${SEGMENTS.map(([, m]) => escapeRe(m)).join("|")})`, "g");

/** 旧工作流可能把新 widget 读成 ""（widgets_values 错位/缺省）：
 * pose_mode 不在三档内一律回落默认值「自动」。 */
export function normalizePoseMode(value) {
  const s = typeof value === "string" ? value.trim() : "";
  return POSE_MODES.includes(s) ? s : "自动";
}

/** 服装模式只有 自动/关闭；旧工作流/手改的非法值回落「自动」。 */
export function normalizeCostumeMode(value) {
  const s = typeof value === "string" ? value.trim() : "";
  return COSTUME_MODES.includes(s) ? s : "自动";
}

/** apply_pose/apply_costume 只接受 -2..63 的整数（-1=不注入）；其余（""/null/
 * undefined/小数/越界/数字字符串）一律回落 -1，避免服务端 INT 校验拒绝整次提交。 */
export function normalizeApplyPose(value) {
  return typeof value === "number" && Number.isInteger(value) && value >= -2 && value <= 63
    ? value
    : -1;
}

export function normalizeApplyCostume(value) {
  return normalizeApplyPose(value);
}

/** pose_in / costume_in 等参考输入是字符串 widget；旧工作流可能读成 null/数字。 */
export function normalizePoseText(value) {
  return typeof value === "string" ? value : "";
}

export function normalizeCostumeText(value) {
  return normalizePoseText(value);
}

/** 参考输入有效来源：连线 > 手填/点选 widget；两者都空才算没有来源。
 * 与 ComfyUI 「连线优先」的提交语义保持一致（不改后端）。 */
export function resolvePoseSource({ linked, widgetValue } = {}) {
  if (linked) return "link";
  return String(widgetValue ?? "").trim() ? "widget" : "none";
}

export function resolveCostumeSource(opts) {
  return resolvePoseSource(opts);
}

/** 连线源节点自动命名（🎯点选同步时用，方便在画布上区分两个姿势源）。 */
export function poseSourceLabel(slot) {
  return slot === 1 ? "姿势人物①（pose_in）" : "姿势人物②（pose_in_2）";
}

/**
 * 点选结果写到哪里：
 * - 无连线 → 写 Editor widget；
 * - 有连线且源节点可写（PrimitiveString 等常量节点）→ 写源节点 widget，保持连线；
 * - 有连线但源不可写（Pose Gallery 等计算输出）→ 提示断开后再写 widget。
 */
export function poseWriteTarget({ linked, sourceWritable } = {}) {
  if (!linked) return "widget";
  return sourceWritable ? "source" : "prompt-disconnect";
}

/** 连线源是否可安全写入点选结果。
 * 只允许常量字符串节点（PrimitiveString / PrimitiveStringMultiline / PrimitiveNode
 * 的 value/text widget），或 widget 名与输出名完全相同的显式透传节点。
 * Pose Gallery 只有一个 pose_id widget、输出 description —— 不能把 description
 * 写进 pose_id（上游 VALIDATE_INPUTS 会抛“未知姿势 ID”），否则返回 false。 */
export function poseSourceWritable({ nodeType, widgetName, outName } = {}) {
  const type = String(nodeType || "");
  const widget = String(widgetName || "");
  const out = String(outName || "");
  if (!widget) return false;
  if (out && widget === out) return true;
  return (type === "PrimitiveString" || type === "PrimitiveStringMultiline" || type === "PrimitiveNode")
    && (widget === "value" || widget === "text");
}

/**
 * 把带分段标记的 caption 切成 {head, segments, tail}：
 * head = 第一个标记前的角色描述；segments = {pose, costume}；tail = 已知遮挡后缀
 * （OCCL_FRONT/OCCL_BACK，精确串）。未识别残片并入 head，不丢用户手写内容。
 */
export function splitCaptionSegments(caption) {
  const text = typeof caption === "string" ? caption : "";
  const hits = SEGMENTS
    .map(([key, marker]) => ({ key, marker, at: text.indexOf(marker) }))
    .filter((h) => h.at >= 0)
    .sort((a, b) => a.at - b.at);
  if (!hits.length) return { head: text, segments: {}, tail: "" };

  let head = text.slice(0, hits[0].at).replace(/\s+$/, "");
  let body = text.slice(hits[0].at);
  let tail = "";
  for (const suf of [OCCL_FRONT, OCCL_BACK]) {
    if (suf && body.endsWith(suf)) {
      tail = suf;
      body = body.slice(0, -suf.length);
      break;
    }
  }
  const segments = {};
  let current = "";
  for (const part of body.split(segmentRe())) {
    const hit = SEGMENTS.find(([, marker]) => marker === part);
    if (hit) {
      current = hit[0];
      if (!(current in segments)) segments[current] = "";
    } else if (current) {
      segments[current] += part;
    } else {
      head += part;
    }
  }
  for (const key of Object.keys(segments)) segments[key] = String(segments[key]).trim();
  return { head, segments, tail };
}

/** 按固定顺序（姿势 → 服装）拼回 caption；空分段不写标记。 */
export function joinCaptionSegments(head, segments, tail = "") {
  let out = String(head || "").replace(/\s+$/, "");
  for (const [key, marker] of SEGMENTS) {
    const text = String((segments && segments[key]) || "").trim();
    if (text) out += marker + text;
  }
  return out + String(tail || "");
}

/** 替换/追加一个分段，保留另一分段与已知遮挡后缀（幂等）。 */
export function setCaptionSegment(caption, key, text) {
  const { head, segments, tail } = splitCaptionSegments(caption);
  segments[key] = text;
  return joinCaptionSegments(head, segments, tail);
}

/** 兼容旧 API：只取姿势段。 */
export function splitPoseCaption(caption) {
  const { head, segments, tail } = splitCaptionSegments(caption);
  return { head, phrase: segments.pose || "", tail };
}

/** 只取服装段。 */
export function splitCostumeCaption(caption) {
  const { head, segments, tail } = splitCaptionSegments(caption);
  return { head, phrase: segments.costume || "", tail };
}

/** 带指定分段标记的 character 条目下标列表。 */
export function markedItems(draft, key) {
  const marker = key === "pose" ? POSE_MARKER : (key === "costume" ? COSTUME_MARKER : "");
  if (!marker) return [];
  if (!draft || typeof draft !== "object" || !Array.isArray(draft.items)) return [];
  const out = [];
  draft.items.forEach((it, i) => {
    if (it && typeof it === "object" && it.label === "character"
        && typeof it.caption === "string" && it.caption.includes(marker)) {
      out.push(i);
    }
  });
  return out;
}

export function poseMarkedItems(draft) {
  return markedItems(draft, "pose");
}

export function costumeMarkedItems(draft) {
  return markedItems(draft, "costume");
}

/**
 * 对调两人某个分段：只交换两个带标记 character caption 的对应段，
 * 各自保留 head、另一分段与 tail。就地修改并返回 draft；
 * 不满足条件（标记条目数 ≠ 2）返回 null 不改动。
 */
export function swapCaptionSegments(draft, key) {
  const idx = markedItems(draft, key);
  if (idx.length !== 2) return null;
  const [a, b] = idx.map((i) => draft.items[i]);
  const A = splitCaptionSegments(a.caption);
  const B = splitCaptionSegments(b.caption);
  a.caption = joinCaptionSegments(A.head, {
    ...A.segments, [key]: B.segments[key] || "",
  }, A.tail);
  b.caption = joinCaptionSegments(B.head, {
    ...B.segments, [key]: A.segments[key] || "",
  }, B.tail);
  return draft;
}

export function swapPoseSegments(draft) {
  return swapCaptionSegments(draft, "pose");
}

export function swapCostumeSegments(draft) {
  return swapCaptionSegments(draft, "costume");
}

/** 图库 description 的第一段（分类/季节）；手填文本无 " / " 时返回 ""。 */
export function costumeCategory(text) {
  const s = String(text || "").trim();
  const i = s.indexOf(" / ");
  return i >= 0 ? s.slice(0, i).trim() : "";
}

/** 是否为内衣/性感/泳装分类（提交到云端 LLM 前提示内容审核风险）。 */
export function costumeIsSensitive(text) {
  return COSTUME_SENSITIVE_CATEGORIES.includes(costumeCategory(text));
}

/** 文本是否提到服装（用于提示本地 📥服装 可能和画布里的旧衣服措辞混合）。 */
export function textMentionsCostume(text) {
  const s = String(text || "");
  return COSTUME_KEYWORDS.some((k) => s.includes(k));
}

/**
 * ⚡拆解 提交子图：以 graphToPrompt() 的完整 prompt 为基础，把根节点的
 * edits 清空、ai_draft/force_split 置位，再裁剪出以根节点为始的子图——
 * 这样 pose_in / costume_in / underlay 等链接输入的上游（如 Pose Gallery /
 * Costume Gallery）会一并提交，参考描述才能真正送进拆解 LLM。
 * overrides 在拆解默认值之后合并：📥姿势/📥服装 按钮用它保留 edits、关掉
 * force_split、并带上 apply_pose/apply_costume 目标下标。
 * excludeInputs（默认 reverse_in）：反推链不随拆解/注入重跑——真机实测踩坑：
 * 反推 enabled 时被带入子图会重复计一次视觉 LLM 费用，且它写回的新散文会让
 * 本次拆解画布的 edits_source_sig 失配、画布立刻回退 caption 形态。反推有自己的
 * ⚡反推 单独执行入口，语义上也不该搭车。剔除时必须同时删根节点上的该输入项，
 * 否则链接指向子图外节点会通不过服务端校验。
 */
export function buildSplitPayload(prompt, rootId, overrides = {}, excludeInputs = ["reverse_in"]) {
  const key = String(rootId);
  const entry = prompt && prompt[key];
  if (!entry || typeof entry !== "object") return null;
  // 关键（拆解语义）：必须清空 edits 再提交。后端 edits 非空且对应当前 source
  // 时优先，带着上一次的旧草稿提交，服务端只会透传旧布局、不触发 AI 拆解。
  const inputs = { ...(entry.inputs || {}), edits: "", ai_draft: true, force_split: true, ...overrides };
  for (const nm of excludeInputs) delete inputs[nm];
  const root = { ...entry, inputs };
  return collectPromptSubset({ ...prompt, [key]: root }, key);
}
