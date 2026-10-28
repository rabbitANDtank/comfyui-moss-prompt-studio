"""MOSS-Image 官方编译器移植（skills/moss-image-prompt-enhancer/scripts/compile_prompt.py 口径）。

坐标约定：
- 草稿（人写/画布编辑）：左上原点、绝对像素、xyxy，带 width/height（16 的倍数）。
- 模型输入（编译产物）：画布中心原点、yxyx、整数；边缘左/上 floor、右/下 ceil；
  不含 width/height；单行紧凑 JSON。

校验哲学照抄官方：拒绝而不是静默修复（未知字段、越界框、非 16 倍数尺寸、空 caption 全部报错）。
"""
from __future__ import annotations

import json
import math

TOP_KEYS = {"width", "height", "bbox_coord_space", "bbox_format", "summary", "items"}
SUMMARY_KEYS = {"caption", "meta", "canvas", "palette"}
ITEM_KEYS = {"label", "caption", "bbox", "text", "tags", "palette"}
VALID_LABELS = {"object", "character", "text"}
VALID_CANVAS = {"single", "multi_panel", "collage"}


class DraftError(ValueError):
    """草稿不符合官方 skill 规范。"""


def _fail(msg: str):
    raise DraftError(msg)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _check_str_list(v, name: str):
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and x.strip() for x in v):
        _fail(f"{name} 必须是非空字符串数组")
    return v


def _check_bbox(b, name: str, w: int, h: int, check_bounds: bool):
    if not isinstance(b, list) or len(b) != 4 or not all(_is_num(x) for x in b):
        _fail(f"{name}.bbox 必须是四个有限数字 [x_min,y_min,x_max,y_max]")
    if not check_bounds:
        return
    x0, y0, x1, y1 = b
    if not (0 <= x0 < x1 <= w):
        _fail(f"{name}.bbox x 越界或翻转: {b}（width={w}）")
    if not (0 <= y0 < y1 <= h):
        _fail(f"{name}.bbox y 越界或翻转: {b}（height={h}）")


def validate_draft(d: dict, check_bounds: bool = True):
    """完整校验 layout 草稿；返回 (width, height)。

    check_bounds=False 仅供"已编译 JSON 反解"场景：真实画布尺寸未知，
    边界问题留到执行期的完整校验再暴露。
    """
    if not isinstance(d, dict):
        _fail("草稿必须是 JSON 对象")
    extra = set(d) - TOP_KEYS
    if extra:
        _fail(f"未知的顶层字段: {sorted(extra)}（官方编译器拒绝未识别字段）")
    missing = TOP_KEYS - set(d)
    if missing:
        _fail(f"缺少顶层字段: {sorted(missing)}")

    w, h = d["width"], d["height"]
    for name, v in (("width", w), ("height", h)):
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0 or v % 16 != 0:
            _fail(f"{name} 必须是 16 的正整数倍（当前 {v!r}）")
    if d["bbox_coord_space"] != "abs":
        _fail('草稿 bbox_coord_space 必须是 "abs"')
    if d["bbox_format"] != "xyxy":
        _fail('草稿 bbox_format 必须是 "xyxy"')

    s = d["summary"]
    if not isinstance(s, dict):
        _fail("summary 必须是对象")
    extra = set(s) - SUMMARY_KEYS
    if extra:
        _fail(f"summary 中有未知字段: {sorted(extra)}")
    cap = s.get("caption")
    if not isinstance(cap, str) or not cap.strip():
        _fail("summary.caption 不能为空")
    if "meta" in s:
        _check_str_list(s["meta"], "summary.meta")
    if "canvas" in s and s["canvas"] not in VALID_CANVAS:
        _fail(f'summary.canvas 必须是 {sorted(VALID_CANVAS)} 之一（当前 {s["canvas"]!r}）')
    if "palette" in s:
        _check_str_list(s["palette"], "summary.palette")

    items = d["items"]
    if not isinstance(items, list) or not items:
        _fail("items 必须是非空数组")
    for i, it in enumerate(items):
        _validate_item(i, it, w, h, check_bounds)
    return w, h


def _validate_item(i: int, it, w: int, h: int, check_bounds: bool):
    pre = f"items[{i}]"
    if not isinstance(it, dict):
        _fail(f"{pre} 必须是对象")
    extra = set(it) - ITEM_KEYS
    if extra:
        _fail(f"{pre} 中有未知字段: {sorted(extra)}")
    missing = {"label", "caption", "bbox"} - set(it)
    if missing:
        _fail(f"{pre} 缺少字段: {sorted(missing)}")
    if it["label"] not in VALID_LABELS:
        _fail(f'{pre}.label 必须是 {sorted(VALID_LABELS)} 之一（当前 {it["label"]!r}）')
    if not isinstance(it["caption"], str) or not it["caption"].strip():
        _fail(f"{pre}.caption 不能为空")
    _check_bbox(it["bbox"], pre, w, h, check_bounds)
    if it["label"] == "text":
        txt = it.get("text")
        if not isinstance(txt, str) or not txt.strip():
            _fail(f"{pre} 是 text 元素，text 字段必填且不能为空（画面可见文字，按原文保留）")
    elif "text" in it:
        _fail(f"{pre} 不是 text 元素，不应携带 text 字段")
    for key in ("tags", "palette"):
        if key in it:
            _check_str_list(it[key], f"{pre}.{key}")


def compile_bbox(b, w: int, h: int):
    """abs/xyxy（左上原点）→ abs_centralized/yxyx（中心原点），边缘 floor/ceil。"""
    x0, y0, x1, y1 = b
    x0, y0 = math.floor(x0), math.floor(y0)
    x1, y1 = math.ceil(x1), math.ceil(y1)
    return [y0 - h // 2, x0 - w // 2, y1 - h // 2, x1 - w // 2]


def compile_draft(d: dict) -> str:
    """草稿 dict → 模型输入的单行紧凑 JSON 字符串。"""
    w, h = validate_draft(d)
    out_items = []
    for it in d["items"]:
        o = {"label": it["label"], "caption": it["caption"]}
        if "text" in it:
            o["text"] = it["text"]
        if "tags" in it:
            o["tags"] = it["tags"]
        if "palette" in it:
            o["palette"] = it["palette"]
        o["bbox"] = compile_bbox(it["bbox"], w, h)
        out_items.append(o)
    result = {
        "summary": d["summary"],
        "items": out_items,
        "bbox_coord_space": "abs_centralized",
        "bbox_format": "yxyx",
    }
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def validate_compiled(c: dict) -> str:
    """校验已是模型输入形态的 JSON 并重新以紧凑单行输出。"""
    if not isinstance(c, dict):
        _fail("已编译提示词必须是 JSON 对象")
    keys = set(c)
    if keys != {"summary", "items", "bbox_coord_space", "bbox_format"}:
        _fail(
            "已编译形态应只含 summary/items/bbox_coord_space/bbox_format"
            f"（多出的字段: {sorted(keys - {'summary', 'items', 'bbox_coord_space', 'bbox_format'}) or '缺少字段 ' + str(sorted({'summary', 'items', 'bbox_coord_space', 'bbox_format'} - keys))}）"
        )
    if c["bbox_coord_space"] != "abs_centralized":
        _fail('已编译形态 bbox_coord_space 必须是 "abs_centralized"')
    if c["bbox_format"] != "yxyx":
        _fail('已编译形态 bbox_format 必须是 "yxyx"')
    s = c["summary"]
    if not isinstance(s, dict) or not isinstance(s.get("caption"), str) or not s["caption"].strip():
        _fail("summary.caption 不能为空")
    extra = set(s) - SUMMARY_KEYS
    if extra:
        _fail(f"summary 中有未知字段: {sorted(extra)}")
    if "canvas" in s and s["canvas"] not in VALID_CANVAS:
        _fail(f'summary.canvas 非法: {s["canvas"]!r}')
    if "meta" in s:
        _check_str_list(s["meta"], "summary.meta")
    if "palette" in s:
        _check_str_list(s["palette"], "summary.palette")
    items = c["items"]
    if not isinstance(items, list) or not items:
        _fail("items 必须是非空数组")
    for i, it in enumerate(items):
        pre = f"items[{i}]"
        if not isinstance(it, dict):
            _fail(f"{pre} 必须是对象")
        extra = set(it) - ITEM_KEYS
        if extra:
            _fail(f"{pre} 中有未知字段: {sorted(extra)}")
        missing = {"label", "caption", "bbox"} - set(it)
        if missing:
            _fail(f"{pre} 缺少字段: {sorted(missing)}")
        if it["label"] not in VALID_LABELS:
            _fail(f'{pre}.label 非法: {it["label"]!r}')
        if not isinstance(it["caption"], str) or not it["caption"].strip():
            _fail(f"{pre}.caption 不能为空")
        b = it["bbox"]
        if not isinstance(b, list) or len(b) != 4 or not all(
            isinstance(x, int) and not isinstance(x, bool) for x in b
        ):
            _fail(f"{pre}.bbox 必须是四个整数（yxyx 中心原点）")
        y0, x0, y1, x1 = b
        if not y0 < y1:
            _fail(f"{pre}.bbox y 顺序非法（y0 必须小于 y1）: {b}")
        if not x0 < x1:
            _fail(f"{pre}.bbox x 顺序非法（x0 必须小于 x1）: {b}")
        if it["label"] == "text" and (not isinstance(it.get("text"), str) or not it["text"].strip()):
            _fail(f"{pre} 是 text 元素，text 必填")
        if it["label"] != "text" and "text" in it:
            _fail(f"{pre} 不是 text 元素，不应携带 text 字段")
        for key in ("tags", "palette"):
            if key in it:
                _check_str_list(it[key], f"{pre}.{key}")
    return json.dumps(c, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
