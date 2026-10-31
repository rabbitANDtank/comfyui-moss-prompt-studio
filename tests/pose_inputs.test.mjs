import test from "node:test";
import assert from "node:assert/strict";
import {
  POSE_INPUT_FIELDS, POSE_INPUT_SPEC, COSTUME_INPUT_FIELDS, COSTUME_INPUT_SPEC,
  poseInputDisplay, referenceInputDisplay,
} from "../web/js/editor/pose_inputs.js";

test("姿势参考面板固定为 pose_in / pose_in_2 两列", () => {
  assert.deepEqual([...POSE_INPUT_FIELDS], ["pose_in", "pose_in_2"]);
  for (const name of POSE_INPUT_FIELDS) {
    assert.ok(POSE_INPUT_SPEC[name], `缺少 ${name} 的配置`);
    assert.ok(POSE_INPUT_SPEC[name].placeholder, `${name} 缺少 placeholder`);
  }
});

test("服装参考面板固定为 costume_in / costume_in_2 两列", () => {
  assert.deepEqual([...COSTUME_INPUT_FIELDS], ["costume_in", "costume_in_2"]);
  for (const name of COSTUME_INPUT_FIELDS) {
    assert.ok(COSTUME_INPUT_SPEC[name], `缺少 ${name} 的配置`);
    assert.ok(COSTUME_INPUT_SPEC[name].placeholder, `${name} 缺少 placeholder`);
    assert.ok(COSTUME_INPUT_SPEC[name].title, `${name} 缺少 tooltip`);
  }
  assert.match(COSTUME_INPUT_SPEC.costume_in.title, /description/);
  assert.match(COSTUME_INPUT_SPEC.costume_in.title, /连线值优先/);
});

test("poseInputDisplay：有连线时禁用编辑并提示断开，无连线保持可编辑", () => {
  const title = POSE_INPUT_SPEC.pose_in.title;
  const linked = poseInputDisplay({ connected: true, value: "站立", title });
  assert.equal(linked.connected, true);
  assert.equal(linked.disabled, true, "连线优先：面板字段不可编辑");
  assert.equal(linked.value, "站立");
  assert.match(linked.title, /当前由连线提供/);
  assert.ok(linked.title.startsWith(title), "保留原 tooltip 前缀");

  const free = poseInputDisplay({ connected: false, value: null, title });
  assert.equal(free.connected, false);
  assert.equal(free.disabled, false);
  assert.equal(free.value, "", "null/undefined 归一成空字符串");
  assert.equal(free.title, title, "无连线时 tooltip 不变");

  // 空标题也给出可读提示（不出现 undefined）
  assert.equal(poseInputDisplay({ connected: true, title: "" }).title, "（当前由连线提供；断开连线后可在此编辑）");
  // 通用命名与旧命名等价（姿势/服装共用同一套显示逻辑）
  assert.deepEqual(referenceInputDisplay({ connected: true, value: "红衣", title: "t" }),
    poseInputDisplay({ connected: true, value: "红衣", title: "t" }));
});
