import test from "node:test";
import assert from "node:assert/strict";
import {
  POSE_MARKER, POSE_MODES, poseMarkedItems, swapPoseSegments, buildSplitPayload,
  normalizePoseMode, normalizeApplyPose, normalizePoseText, normalizeCostumeText, resolvePoseSource,
  poseSourceLabel, poseWriteTarget, poseSourceWritable, splitPoseCaption,
} from "../web/js/editor/pose.js";
import { OCCL_FRONT, OCCL_BACK } from "../web/js/editor/model.js";

const mkItem = (caption, label = "character") => ({ label, caption, bbox: [0, 0, 10, 10] });
const mkDraft = (...items) => ({ width: 1024, height: 1024, items });

test("poseMarkedItems counts only character items carrying the marker", () => {
  const d = mkDraft(
    mkItem("银甲战士" + POSE_MARKER + "四肢着地"),
    mkItem("怪兽，无姿势标记"),
    mkItem("背景招牌" + POSE_MARKER + "站姿", "object"), // 非角色：不算
  );
  assert.deepEqual(poseMarkedItems(d), [0]);
  assert.deepEqual(poseMarkedItems(null), []);
  assert.deepEqual(poseMarkedItems({}), []);
});

test("swapPoseSegments swaps marker tails between exactly two marked characters", () => {
  const d = mkDraft(
    mkItem("银甲战士" + POSE_MARKER + "四肢着地・单腿后高抬"),
    mkItem("机械怪兽人" + POSE_MARKER + "S形站姿"),
  );
  const out = swapPoseSegments(d);
  assert.equal(out, d, "就地修改并返回原对象");
  assert.equal(d.items[0].caption, "银甲战士" + POSE_MARKER + "S形站姿");
  assert.equal(d.items[1].caption, "机械怪兽人" + POSE_MARKER + "四肢着地・单腿后高抬");
});

test("swapPoseSegments marker at caption start keeps heads with their own item", () => {
  const d = mkDraft(
    mkItem(POSE_MARKER + "跪姿"),
    mkItem("戴眼镜的女孩" + POSE_MARKER + "站姿"),
  );
  assert.ok(swapPoseSegments(d));
  // 头部各自保留，只交换末尾姿势段
  assert.equal(d.items[0].caption, POSE_MARKER + "站姿");
  assert.equal(d.items[1].caption, "戴眼镜的女孩" + POSE_MARKER + "跪姿");
});

test("swapPoseSegments rejects 0/1/3 marked characters without mutating", () => {
  const one = mkDraft(mkItem("A" + POSE_MARKER + "x"), mkItem("B"));
  const before = one.items[0].caption;
  assert.equal(swapPoseSegments(one), null);
  assert.equal(one.items[0].caption, before);

  const three = mkDraft(
    mkItem("A" + POSE_MARKER + "x"), mkItem("B" + POSE_MARKER + "y"),
    mkItem("C" + POSE_MARKER + "z"),
  );
  assert.equal(swapPoseSegments(three), null);
  assert.equal(swapPoseSegments(mkDraft(mkItem("无标记"))), null);
  assert.equal(swapPoseSegments(null), null);
});

test("buildSplitPayload collects upstream subgraph and forces a real split", () => {
  const prompt = {
    "1": { class_type: "PoseGallery", inputs: { pose_id: "full_single_P031_front" } },
    "3": { class_type: "LoadImage", inputs: { image: "ref.png" } },
    "7": { class_type: "MOSSEditor", inputs: {
      source: "散文", edits: '{"width":1024}', ai_draft: false, force_split: false,
      pose_in: ["1", "description"], pose_in_2: ["1", "pose_id"], underlay: ["3", "IMAGE"],
    } },
    "9": { class_type: "MOSSTranslator", inputs: { draft: ["7", "draft"] } }, // 下游：不得带入
  };
  const sub = buildSplitPayload(prompt, 7);
  assert.deepEqual(Object.keys(sub).sort(), ["1", "3", "7"], "上游全带上、下游裁掉");
  assert.equal(sub["7"].inputs.edits, "", "必须清空 edits，否则服务端透传旧草稿不重拆");
  assert.equal(sub["7"].inputs.ai_draft, true);
  assert.equal(sub["7"].inputs.force_split, true);
  assert.equal(sub["7"].inputs.source, "散文");
  assert.deepEqual(sub["7"].inputs.pose_in, ["1", "description"]);
  // 原 prompt 不被改动
  assert.equal(prompt["7"].inputs.edits, '{"width":1024}');
  assert.equal(prompt["7"].inputs.force_split, false);
});

test("buildSplitPayload drops the reverse_in chain (no double vision-LLM billing)", () => {
  // 真机踩坑回归：反推 enabled 时随拆解子图重跑 → 重复计费 + 新散文写回使拆解
  // 画布 edits_source_sig 失配、立刻回退 caption 形态
  const prompt = {
    "2": { class_type: "MOSSImageReverse", inputs: { image: ["1", "IMAGE"], enabled: true } },
    "1": { class_type: "LoadImage", inputs: { image: "a.png" } },
    "4": { class_type: "PoseGallery", inputs: { pose_id: "p031" } },
    "7": { class_type: "MOSSEditor", inputs: {
      source: "散文", edits: "", ai_draft: true, force_split: false,
      reverse_in: ["2", "prose_zh"], pose_in: ["4", "description"],
    } },
  };
  const sub = buildSplitPayload(prompt, 7);
  assert.ok(sub["7"], "根节点在");
  assert.ok(!sub["2"] && !sub["1"], "反推链（含其 LoadImage 上游）必须被剔除");
  assert.ok(sub["4"], "姿势上游保留");
  assert.equal(sub["7"].inputs.reverse_in, undefined, "根节点上的 reverse_in 输入项必须删除（否则链接指向子图外，服务端校验失败）");
  assert.ok(sub["7"].inputs.pose_in, "pose_in 链接保留");
});

test("buildSplitPayload returns null for missing/bypassed root", () => {
  assert.equal(buildSplitPayload({}, 42), null);
  assert.equal(buildSplitPayload(null, 7), null);
  assert.equal(buildSplitPayload({ "7": "bypassed-string" }, 7), null);
});

test("buildSplitPayload overrides support the 📥pose inject submission", () => {
  const prompt = {
    "1": { class_type: "PoseGallery", inputs: { pose_id: "p031" } },
    "7": { class_type: "MOSSEditor", inputs: {
      source: "散文", edits: '{"canvas":"current"}', ai_draft: false, force_split: false,
      apply_pose: -1, pose_in: ["1", "description"],
    } },
  };
  // 注入语义：保留当前 edits、不改 ai_draft、不强制重拆、带目标下标（-2=自动分配）
  const sub = buildSplitPayload(prompt, 7, {
    edits: prompt["7"].inputs.edits,
    ai_draft: false,
    force_split: false,
    apply_pose: 0,
  });
  assert.equal(sub["7"].inputs.edits, '{"canvas":"current"}', "注入必须保留画布内容");
  assert.equal(sub["7"].inputs.force_split, false);
  assert.equal(sub["7"].inputs.apply_pose, 0);
  // 未选中角色时按钮应传 -2（后端约定：自动分配），不得用 -1（那是"不注入"）
  const subAuto = buildSplitPayload(prompt, 7, {
    edits: prompt["7"].inputs.edits, ai_draft: false, force_split: false, apply_pose: -2,
  });
  assert.equal(subAuto["7"].inputs.apply_pose, -2);
  assert.ok(sub["1"], "姿势上游仍随子图提交");
  // 原 prompt 不被改动（apply_pose 仍为 -1，普通 Queue 不注入）
  assert.equal(prompt["7"].inputs.apply_pose, -1);
  assert.equal(prompt["7"].inputs.edits, '{"canvas":"current"}');
});

// ---------- 旧工作流 widget 空值归一化（F1） ----------

test("normalizePoseMode keeps valid modes and falls back to 自动", () => {
  for (const v of POSE_MODES) assert.equal(normalizePoseMode(v), v);
  assert.equal(normalizePoseMode(" 自动 "), "自动");
  assert.equal(normalizePoseMode(""), "自动");
  assert.equal(normalizePoseMode(null), "自动");
  assert.equal(normalizePoseMode(undefined), "自动");
  assert.equal(normalizePoseMode(0), "自动");
  assert.equal(normalizePoseMode("关闭姿势"), "自动");
});

test("normalizeApplyPose accepts -2..63 integers and falls back to -1", () => {
  for (const v of [-2, -1, 0, 1, 63]) assert.equal(normalizeApplyPose(v), v);
  for (const v of [-3, 64, 3.5, NaN, Infinity, "", "3", null, undefined, true, {}]) {
    assert.equal(normalizeApplyPose(v), -1, `bad value ${String(v)}`);
  }
});

test("normalizePoseText keeps strings and falls back to empty string", () => {
  assert.equal(normalizePoseText("跪立"), "跪立");
  assert.equal(normalizePoseText(""), "");
  assert.equal(normalizePoseText(null), "");
  assert.equal(normalizePoseText(undefined), "");
  assert.equal(normalizePoseText(123), "");
  assert.equal(normalizePoseText({}), "");
});

test("normalizePoseText clears polluted 自动 from widgets_values misalignment", () => {
  // v1.9.0-beta0 真机：工作流重载后 pose_in_2 被 costume_mode 默认值「自动」污染，
  // 不清理会把未填的第二姿势当已填送给拆解 LLM。
  assert.equal(normalizePoseText("自动"), "");
  assert.equal(normalizePoseText(" 自动 "), "");
  assert.equal(normalizePoseText("自动跟随"), "自动跟随");
  assert.equal(normalizeCostumeText("自动"), "");
});

// ---------- 姿势三入口有效来源（F2） ----------

test("resolvePoseSource prefers link, then non-empty widget, then none", () => {
  assert.equal(resolvePoseSource({ linked: true, widgetValue: "" }), "link");
  assert.equal(resolvePoseSource({ linked: true, widgetValue: "跪立" }), "link");
  assert.equal(resolvePoseSource({ linked: false, widgetValue: "跪立" }), "widget");
  assert.equal(resolvePoseSource({ linked: false, widgetValue: "  " }), "none");
  assert.equal(resolvePoseSource({ linked: false, widgetValue: "" }), "none");
  assert.equal(resolvePoseSource({ linked: false, widgetValue: null }), "none");
  assert.equal(resolvePoseSource({}), "none");
  assert.equal(resolvePoseSource(), "none");
});

// ---------- 点选同步到连线源（PrimitiveString 等常量节点） ----------

test("poseWriteTarget: widget / source / prompt-disconnect", () => {
  assert.equal(poseWriteTarget({ linked: false, sourceWritable: false }), "widget");
  assert.equal(poseWriteTarget({ linked: false, sourceWritable: true }), "widget");
  assert.equal(poseWriteTarget({ linked: true, sourceWritable: true }), "source");
  assert.equal(poseWriteTarget({ linked: true, sourceWritable: false }), "prompt-disconnect");
  assert.equal(poseWriteTarget({}), "widget");
});

test("poseSourceLabel marks pose person 1/2", () => {
  assert.equal(poseSourceLabel(1), "姿势人物①（pose_in）");
  assert.equal(poseSourceLabel(2), "姿势人物②（pose_in_2）");
});

// ---------- F1 连线源可写白名单（Pose Gallery 的 pose_id 不得被误写） ----------

test("poseSourceWritable: only explicit constant nodes or same-name passthrough", () => {
  assert.equal(poseSourceWritable({ nodeType: "PrimitiveString", widgetName: "value", outName: "STRING" }), true);
  assert.equal(poseSourceWritable({ nodeType: "PrimitiveStringMultiline", widgetName: "text", outName: "STRING" }), true);
  assert.equal(poseSourceWritable({ nodeType: "PrimitiveNode", widgetName: "value", outName: "STRING" }), true);
  // Pose Gallery：pose_id widget + description 输出，禁止写入
  assert.equal(poseSourceWritable({ nodeType: "PoseGallery", widgetName: "pose_id", outName: "description" }), false);
  // 显式同名透传
  assert.equal(poseSourceWritable({ nodeType: "SomeNode", widgetName: "description", outName: "description" }), true);
  assert.equal(poseSourceWritable({ nodeType: "SomeNode", widgetName: "pose_id", outName: "description" }), false);
  assert.equal(poseSourceWritable({}), false);
  assert.equal(poseSourceWritable({ nodeType: "PrimitiveString", widgetName: "", outName: "" }), false);
});

// ---------- F6 对调姿势保留遮挡后缀 ----------

test("splitPoseCaption keeps known occlusion suffix as tail", () => {
  assert.deepEqual(splitPoseCaption("甲" + POSE_MARKER + "站立" + OCCL_FRONT),
    { head: "甲", phrase: "站立", tail: OCCL_FRONT });
  assert.deepEqual(splitPoseCaption("乙" + POSE_MARKER + "跪立" + OCCL_BACK),
    { head: "乙", phrase: "跪立", tail: OCCL_BACK });
  // 手填描述里的逗号不属于已知遮挡后缀 → 整段 phrase
  assert.deepEqual(splitPoseCaption("丙" + POSE_MARKER + "站立，双手插兜"),
    { head: "丙", phrase: "站立，双手插兜", tail: "" });
  assert.deepEqual(splitPoseCaption("无标记"), { head: "无标记", phrase: "", tail: "" });
});

test("swapPoseSegments swaps phrases only and keeps occlusion tails", () => {
  const d = mkDraft(
    mkItem("甲" + POSE_MARKER + "站立" + OCCL_FRONT),
    mkItem("乙" + POSE_MARKER + "跪立" + OCCL_BACK),
  );
  assert.ok(swapPoseSegments(d));
  assert.equal(d.items[0].caption, "甲" + POSE_MARKER + "跪立" + OCCL_FRONT);
  assert.equal(d.items[1].caption, "乙" + POSE_MARKER + "站立" + OCCL_BACK);
});
