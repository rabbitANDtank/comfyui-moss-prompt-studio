/** 姿势注入 ↔ Editor 的纯逻辑（无 DOM，便于 node --test 覆盖）。
 * POSE_MARKER / POSE_MODES 与后端 py/translate.py 的同名常量保持一致。 */
import { collectPromptSubset } from "./reverse.js";
import { OCCL_FRONT, OCCL_BACK } from "./model.js";

export const POSE_MARKER = "；姿势：";
export const POSE_MODES = ["自动", "强制双人", "关闭"];

/** 旧工作流可能把新 widget 读成 ""（widgets_values 错位/缺省）：
 * pose_mode 不在三档内一律回落默认值「自动」。 */
export function normalizePoseMode(value) {
  const s = typeof value === "string" ? value.trim() : "";
  return POSE_MODES.includes(s) ? s : "自动";
}

/** apply_pose 只接受 -2..63 的整数（-1=不注入）；其余（""/null/undefined/
 * 小数/越界/数字字符串）一律回落 -1，避免服务端 INT 校验拒绝整次提交。 */
export function normalizeApplyPose(value) {
  return typeof value === "number" && Number.isInteger(value) && value >= -2 && value <= 63
    ? value
    : -1;
}

/** pose_in / pose_in_2 是字符串 widget；旧工作流可能读成 null/数字。 */
export function normalizePoseText(value) {
  return typeof value === "string" ? value : "";
}

/** 姿势有效来源：连线 > 手填/点选 widget；两者都空才算没有姿势来源。
 * 与 ComfyUI 「连线优先」的提交语义保持一致（不改后端）。 */
export function resolvePoseSource({ linked, widgetValue } = {}) {
  if (linked) return "link";
  return String(widgetValue ?? "").trim() ? "widget" : "none";
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

/** 把带姿势标记的 caption 切成 {head, phrase, tail}：
 * head = 标记前的角色描述；phrase = 标记后的姿势短语；tail = 已知遮挡后缀
 * （OCCL_FRONT/OCCL_BACK，精确串）。非已知后缀（含手填描述里的逗号）整段算 phrase，
 * 避免对调姿势时把遮挡措辞一起交换。 */
export function splitPoseCaption(caption) {
  const text = typeof caption === "string" ? caption : "";
  const i = text.lastIndexOf(POSE_MARKER);
  if (i < 0) return { head: text, phrase: "", tail: "" };
  const head = text.slice(0, i);
  const rest = text.slice(i + POSE_MARKER.length);
  for (const suf of [OCCL_FRONT, OCCL_BACK]) {
    if (suf && rest.endsWith(suf)) {
      return { head, phrase: rest.slice(0, -suf.length), tail: suf };
    }
  }
  return { head, phrase: rest, tail: "" };
}

/** 带姿势标记的 character 条目下标列表（label=character 且 caption 含标记）。 */
export function poseMarkedItems(draft) {
  if (!draft || typeof draft !== "object" || !Array.isArray(draft.items)) return [];
  const out = [];
  draft.items.forEach((it, i) => {
    if (it && typeof it === "object" && it.label === "character"
        && typeof it.caption === "string" && it.caption.includes(POSE_MARKER)) {
      out.push(i);
    }
  });
  return out;
}

/**
 * 对调两人姿势：只交换两个带标记 character caption 的“；姿势：…”短语，
 * 各自保留 head（角色描述）与 tail（已知遮挡后缀）。就地修改并返回 draft；
 * 不满足条件（标记条目数 ≠ 2）返回 null 不改动。
 */
export function swapPoseSegments(draft) {
  const idx = poseMarkedItems(draft);
  if (idx.length !== 2) return null;
  const [a, b] = idx.map((i) => draft.items[i]);
  const A = splitPoseCaption(a.caption);
  const B = splitPoseCaption(b.caption);
  a.caption = A.head + POSE_MARKER + B.phrase + A.tail;
  b.caption = B.head + POSE_MARKER + A.phrase + B.tail;
  return draft;
}

/**
 * ⚡拆解 提交子图：以 graphToPrompt() 的完整 prompt 为基础，把根节点的
 * edits 清空、ai_draft/force_split 置位，再裁剪出以根节点为始的子图——
 * 这样 pose_in / pose_in_2 / underlay 等链接输入的上游（如 Pose Gallery）会一并提交，
 * 姿势描述才能真正送进拆解 LLM（旧实现只提交编辑器单节点，链接全被丢掉）。
 * overrides 在拆解默认值之后合并：📥姿势 按钮用它保留 edits、关掉 force_split、
 * 并带上 apply_pose 目标下标。
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
