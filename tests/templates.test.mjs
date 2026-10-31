import test from "node:test";
import assert from "node:assert/strict";
import { validateDraft } from "../web/js/editor/model.js";
import { TEMPLATES, listTemplates, templateById } from "../web/js/editor/templates.js";

const layout = (d) => ({ kind: "layout", d });

test("模板库至少 6 个自建版式，id 唯一且名称/说明非空", () => {
  assert.ok(TEMPLATES.length >= 6);
  const ids = new Set();
  for (const t of TEMPLATES) {
    assert.equal(typeof t.id, "string");
    assert.ok(t.id.trim(), "id 不能为空");
    assert.ok(!ids.has(t.id), `id 重复: ${t.id}`);
    ids.add(t.id);
    assert.ok(typeof t.name === "string" && t.name.trim(), `${t.id}: name 为空`);
    assert.ok(typeof t.description === "string" && t.description.trim(), `${t.id}: description 为空`);
    assert.ok(t.draft && typeof t.draft === "object", `${t.id}: 缺少 draft`);
  }
});

test("每个模板都通过官方 validateDraft，宽高为 16 的倍数", () => {
  for (const t of TEMPLATES) {
    const d = t.draft;
    assert.equal(d.width % 16, 0, `${t.id}: width 不是 16 的倍数`);
    assert.equal(d.height % 16, 0, `${t.id}: height 不是 16 的倍数`);
    assert.ok(d.width > 0 && d.height > 0);
    const errs = validateDraft(layout(d));
    assert.deepEqual(errs, [], `${t.id}: ${errs.join("; ")}`);
  }
});

test("text item 必须带非空 text，非 text item 不应有 text", () => {
  for (const t of TEMPLATES) {
    let textCount = 0;
    for (const it of t.draft.items) {
      if (it.label === "text") {
        textCount += 1;
        assert.equal(typeof it.text, "string", `${t.id}: text item 缺少 text`);
        assert.ok(it.text.trim(), `${t.id}: text item 的 text 为空`);
      } else {
        assert.ok(!("text" in it), `${t.id}: 非 text item 不应带 text`);
      }
    }
    assert.ok(textCount > 0, `${t.id}: 至少需要一个 text item`);
  }
});

test("六个版式类型齐备（竖版海报/方形社媒/电商主图/杂志封面/人物卡/四格分镜）", () => {
  for (const id of [
    "vertical_poster", "square_social", "ecommerce_hero",
    "magazine_cover", "character_card", "four_panel_storyboard",
  ]) {
    assert.ok(templateById(id), `缺少模板: ${id}`);
  }
  const storyboard = templateById("four_panel_storyboard");
  assert.equal(storyboard.draft.summary.canvas, "multi_panel");
  assert.equal(storyboard.draft.items.filter((it) => it.label === "object").length, 4);
  assert.equal(storyboard.draft.items.filter((it) => it.label === "text").length, 4);
});

test("draft 使用官方 layout 字段：abs/xyxy + summary.caption/meta/canvas/palette", () => {
  for (const t of TEMPLATES) {
    const d = t.draft;
    assert.equal(d.bbox_coord_space, "abs");
    assert.equal(d.bbox_format, "xyxy");
    assert.ok(d.summary && typeof d.summary.caption === "string" && d.summary.caption.trim());
    assert.ok(Array.isArray(d.summary.meta) && d.summary.meta.length > 0);
    assert.ok(Array.isArray(d.summary.palette) && d.summary.palette.length > 0);
    assert.ok(typeof d.summary.canvas === "string" && d.summary.canvas.trim());
    assert.ok(Array.isArray(d.items) && d.items.length > 0);
  }
});

test("listTemplates / templateById 查询行为", () => {
  const list = listTemplates();
  assert.equal(list.length, TEMPLATES.length);
  assert.deepEqual(Object.keys(list[0]).sort(), ["description", "draft", "id", "name"]);
  assert.equal(templateById("vertical_poster").name, "竖版海报");
  assert.equal(templateById("no_such_template"), null);
  assert.equal(templateById(null), null);
  // listTemplates 返回浅拷贝条目，改元数据不会污染模板库
  list[0].name = "被改掉的名字";
  assert.notEqual(TEMPLATES[0].name, "被改掉的名字");
});
