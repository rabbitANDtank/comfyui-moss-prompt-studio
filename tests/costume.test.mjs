import test from "node:test";
import assert from "node:assert/strict";
import {
  COSTUME_MARKER, COSTUME_MODES, COSTUME_SENSITIVE_CATEGORIES, POSE_MARKER,
  costumeMarkedItems, poseMarkedItems, swapCostumeSegments, swapPoseSegments,
  splitCaptionSegments, joinCaptionSegments, setCaptionSegment,
  splitPoseCaption, splitCostumeCaption, costumeCategory, costumeIsSensitive,
  textMentionsCostume, normalizeCostumeMode, normalizeApplyCostume,
  normalizeCostumeText, resolveCostumeSource, buildSplitPayload,
} from "../web/js/editor/pose.js";
import { OCCL_FRONT, OCCL_BACK } from "../web/js/editor/model.js";

const mkItem = (caption, label = "character") => ({ label, caption, bbox: [0, 0, 10, 10] });
const mkDraft = (...items) => ({ width: 1024, height: 1024, items });

test("姿势/服装标记与分段顺序固定为 姿势 → 服装", () => {
  assert.equal(POSE_MARKER, "；姿势：");
  assert.equal(COSTUME_MARKER, "；服装：");
  assert.deepEqual(COSTUME_MODES, ["自动", "关闭"]);
  assert.deepEqual(COSTUME_SENSITIVE_CATEGORIES, ["内衣", "性感", "泳装"]);
});

test("normalizeCostumeMode 只接受 自动/关闭，非法值回落自动", () => {
  for (const v of COSTUME_MODES) assert.equal(normalizeCostumeMode(v), v);
  assert.equal(normalizeCostumeMode(" 自动 "), "自动");
  for (const v of ["", null, undefined, 0, "强制双人", "关闭服装"]) {
    assert.equal(normalizeCostumeMode(v), "自动");
  }
});

test("normalizeApplyCostume 与 normalizeCostumeText 归一旧工作流空值", () => {
  for (const v of [-2, -1, 0, 1, 63]) assert.equal(normalizeApplyCostume(v), v);
  for (const v of [-3, 64, 3.5, NaN, Infinity, "", "3", null, undefined, true, {}]) {
    assert.equal(normalizeApplyCostume(v), -1, `bad value ${String(v)}`);
  }
  assert.equal(normalizeCostumeText("春装 / 白色"), "春装 / 白色");
  for (const v of [null, undefined, 123, {}]) assert.equal(normalizeCostumeText(v), "");
});

test("resolveCostumeSource: 连线 > widget > none", () => {
  assert.equal(resolveCostumeSource({ linked: true, widgetValue: "" }), "link");
  assert.equal(resolveCostumeSource({ linked: false, widgetValue: "春装 / 白裙" }), "widget");
  assert.equal(resolveCostumeSource({ linked: false, widgetValue: "  " }), "none");
  assert.equal(resolveCostumeSource(), "none");
});

test("splitCaptionSegments 解析姿势+服装+遮挡后缀", () => {
  const cap = "角色甲" + POSE_MARKER + "站立"
    + COSTUME_MARKER + "红色长裙" + OCCL_BACK;
  assert.deepEqual(splitCaptionSegments(cap), {
    head: "角色甲",
    segments: { pose: "站立", costume: "红色长裙" },
    tail: OCCL_BACK,
  });
  assert.equal(joinCaptionSegments("角色甲", { costume: "红色长裙", pose: "站立" }, OCCL_BACK), cap);
  // 只有服装
  assert.deepEqual(splitCaptionSegments("乙" + COSTUME_MARKER + "黑衣"), {
    head: "乙", segments: { costume: "黑衣" }, tail: "",
  });
  // 无标记
  assert.deepEqual(splitCaptionSegments("无标记"), { head: "无标记", segments: {}, tail: "" });
});

test("setCaptionSegment 替换一段时保留另一段与遮挡后缀（幂等）", () => {
  const cap = "甲" + POSE_MARKER + "站立" + COSTUME_MARKER + "红衣" + OCCL_FRONT;
  const a = setCaptionSegment(cap, "costume", "蓝衣");
  assert.equal(a, "甲" + POSE_MARKER + "站立" + COSTUME_MARKER + "蓝衣" + OCCL_FRONT);
  const b = setCaptionSegment(a, "pose", "跪立");
  assert.equal(b, "甲" + POSE_MARKER + "跪立" + COSTUME_MARKER + "蓝衣" + OCCL_FRONT);
  assert.equal(setCaptionSegment(b, "costume", "蓝衣"), b, "重复写入应幂等");
});

test("splitPoseCaption / splitCostumeCaption 只取各自分段", () => {
  const cap = "甲" + POSE_MARKER + "站立" + COSTUME_MARKER + "红衣" + OCCL_BACK;
  assert.deepEqual(splitPoseCaption(cap), { head: "甲", phrase: "站立", tail: OCCL_BACK });
  assert.deepEqual(splitCostumeCaption(cap), { head: "甲", phrase: "红衣", tail: OCCL_BACK });
});

test("costumeMarkedItems 只统计带服装标记的角色", () => {
  const d = mkDraft(
    mkItem("甲" + COSTUME_MARKER + "红衣"),
    mkItem("乙" + POSE_MARKER + "站立"),
    mkItem("招牌" + COSTUME_MARKER + "霓虹", "object"),
  );
  assert.deepEqual(costumeMarkedItems(d), [0]);
  assert.deepEqual(poseMarkedItems(d), [1]);
  assert.deepEqual(costumeMarkedItems(null), []);
});

test("swapCostumeSegments 只换服装段，保留各自姿势段与遮挡后缀", () => {
  const d = mkDraft(
    mkItem("甲" + POSE_MARKER + "站立" + COSTUME_MARKER + "红衣" + OCCL_FRONT),
    mkItem("乙" + POSE_MARKER + "跪立" + COSTUME_MARKER + "蓝衣" + OCCL_BACK),
  );
  assert.ok(swapCostumeSegments(d));
  assert.equal(d.items[0].caption,
    "甲" + POSE_MARKER + "站立" + COSTUME_MARKER + "蓝衣" + OCCL_FRONT);
  assert.equal(d.items[1].caption,
    "乙" + POSE_MARKER + "跪立" + COSTUME_MARKER + "红衣" + OCCL_BACK);
});

test("swapPoseSegments 只换姿势段，服装段保持不动", () => {
  const d = mkDraft(
    mkItem("甲" + POSE_MARKER + "站立" + COSTUME_MARKER + "红衣"),
    mkItem("乙" + POSE_MARKER + "跪立" + COSTUME_MARKER + "蓝衣"),
  );
  assert.ok(swapPoseSegments(d));
  assert.equal(d.items[0].caption, "甲" + POSE_MARKER + "跪立" + COSTUME_MARKER + "红衣");
  assert.equal(d.items[1].caption, "乙" + POSE_MARKER + "站立" + COSTUME_MARKER + "蓝衣");
});

test("costumeCategory / costumeIsSensitive 只认第一段分类", () => {
  assert.equal(costumeCategory("春装 / 白色、长裙"), "春装");
  assert.equal(costumeCategory("红色长裙"), "");
  assert.equal(costumeIsSensitive("内衣 / 黑色、蕾丝"), true);
  assert.equal(costumeIsSensitive("泳装 / 白色、比基尼"), true);
  assert.equal(costumeIsSensitive("春装 / 白色、长裙"), false);
  assert.equal(costumeIsSensitive("红色长裙"), false);
  // 关键词里出现敏感词但分类不是敏感分类 → 不误报
  assert.equal(costumeIsSensitive("春装 / 蕾丝、文胸"), false);
});

test("textMentionsCostume 识别画布里的旧服装措辞（反推草稿混合提示用）", () => {
  assert.equal(textMentionsCostume("一位黑长直发年轻女子身穿浅粉色无袖连衣裙，侧身倚墙"), true);
  assert.equal(textMentionsCostume("短发女子低头，穿黑色吊带束身衣、白色领结"), true);
  assert.equal(textMentionsCostume("背景为灰白色斑驳水泥墙面，带有裂纹"), false);
  assert.equal(textMentionsCostume("右耳佩戴金色弧形耳环"), false);
  assert.equal(textMentionsCostume(""), false);
  assert.equal(textMentionsCostume(null), false);
  assert.equal(textMentionsCostume(undefined), false);
});

test("buildSplitPayload 带上 Costume Gallery 上游并支持 📥服装 overrides", () => {
  const prompt = {
    "1": { class_type: "CostumeGallery", inputs: { costume_id: "SPR-0001" } },
    "7": { class_type: "MOSSEditor", inputs: {
      source: "散文", edits: '{"width":1024}', ai_draft: false, force_split: false,
      apply_pose: -1, apply_costume: -1, costume_in: ["1", "description"],
    } },
    "9": { class_type: "MOSSTranslator", inputs: { draft: ["7", "draft"] } },
  };
  const sub = buildSplitPayload(prompt, 7, {
    edits: prompt["7"].inputs.edits, ai_draft: false, force_split: false,
    apply_costume: 0, apply_pose: -1,
  });
  assert.deepEqual(Object.keys(sub).sort(), ["1", "7"], "上游保留、下游裁掉");
  assert.deepEqual(sub["7"].inputs.costume_in, ["1", "description"]);
  assert.equal(sub["7"].inputs.apply_costume, 0);
  assert.equal(sub["7"].inputs.apply_pose, -1);
  assert.equal(sub["7"].inputs.edits, '{"width":1024}', "注入必须保留画布内容");
  assert.equal(prompt["7"].inputs.apply_costume, -1, "原 prompt 不被改动");
  // 自动分配传 -2；-1 是“不注入”
  const auto = buildSplitPayload(prompt, 7, { apply_costume: -2 });
  assert.equal(auto["7"].inputs.apply_costume, -2);
});
