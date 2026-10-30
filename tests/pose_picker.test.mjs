import test from "node:test";
import assert from "node:assert/strict";
import { buildFacets, filterPoses, toDescription, groupByPose, poseThumbUrl } from "../web/js/editor/pose_picker.js";

const ITEMS = [
  { id: "full_single_P001_front", system: "全身单人・标准系", category: "站姿", name: "S形站姿・胯部侧顶", view: "正面", search: "…" },
  { id: "full_single_P001_high", system: "全身单人・标准系", category: "站姿", name: "S形站姿・胯部侧顶", view: "俯视", search: "…" },
  { id: "full_single_P031_front", system: "全身单人・标准系", category: "跪立", name: "四肢着地・单腿后高抬", view: "正面", search: "…" },
  { id: "pair60_P001_front", system: "双人缠绵", category: "立位贴身", name: "面对面・抱起单膝", view: "正面", search: "…" },
  { id: "pair60_P001_side", system: "双人缠绵", category: "立位贴身", name: "面对面・抱起单膝", view: "侧面", search: "…" },
];

test("buildFacets collects unique systems/categories/views in order", () => {
  const f = buildFacets(ITEMS);
  assert.deepEqual(f.systems, ["全身单人・标准系", "双人缠绵"]);
  assert.deepEqual(f.categories, ["站姿", "跪立", "立位贴身"]);
  assert.deepEqual(f.views, ["正面", "俯视", "侧面"]);
  assert.deepEqual(buildFacets([]), { systems: [], categories: [], views: [] });
});

test("filterPoses by system/category/view/query", () => {
  assert.equal(filterPoses(ITEMS, { system: "双人缠绵" }).length, 2);
  assert.equal(filterPoses(ITEMS, { category: "站姿" }).length, 2);
  assert.equal(filterPoses(ITEMS, { view: "正面" }).length, 3);
  assert.equal(filterPoses(ITEMS, { query: "四肢着地" }).length, 1);
  assert.equal(filterPoses(ITEMS, { query: "P031" }).length, 1); // 编号也可搜
  assert.equal(filterPoses(ITEMS, { system: "双人缠绵", view: "正面" }).length, 1);
  assert.equal(filterPoses(ITEMS).length, 5); // 空筛选=全部
  assert.equal(filterPoses(null).length, 0);
});

test("toDescription matches gallery description format", () => {
  assert.equal(toDescription(ITEMS[3]), "双人缠绵 / 立位贴身 / 面对面・抱起单膝 / 正面");
  assert.equal(toDescription(null), "");
});

test("groupByPose merges views of the same pose group", () => {
  const g = groupByPose(ITEMS);
  assert.equal(g.length, 3); // P001单人 / P031 / pair60_P001
  const pair = g.find((x) => x.system === "双人缠绵");
  assert.equal(pair.views.length, 2);
  assert.deepEqual(pair.views.map((v) => v.view).sort(), ["侧面", "正面"]);
});

// ---------- F9 缩略图子路径 ----------

test("poseThumbUrl prefers apiURL, then api_base, then root path", () => {
  const id = "full_single_P001_front";
  assert.equal(
    poseThumbUrl({ apiURL: (p) => "/comfy" + p }, id),
    "/comfy/pose_gallery/assets/thumbs/full_single_P001_front.jpg",
  );
  assert.equal(
    poseThumbUrl({ api_base: "/comfy/" }, id),
    "/comfy/pose_gallery/assets/thumbs/full_single_P001_front.jpg",
  );
  assert.equal(
    poseThumbUrl({ api_base: "/comfy" }, id),
    "/comfy/pose_gallery/assets/thumbs/full_single_P001_front.jpg",
  );
  assert.equal(
    poseThumbUrl(undefined, id),
    "/pose_gallery/assets/thumbs/full_single_P001_front.jpg",
  );
  // id 编码
  assert.equal(
    poseThumbUrl({}, "a b/c"),
    "/pose_gallery/assets/thumbs/a%20b%2Fc.jpg",
  );
});
