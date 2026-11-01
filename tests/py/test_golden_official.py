"""编译器对拍：与 vendor 的官方实现做差分（消除"自证正确"）。

官方脚本 tests/vendor/compile_prompt_official.py 原样取自
OpenMOSS/MOSS-Image@20f5592（Apache-2.0，出处与版本见该文件头）。

差分范围：
- layout 草稿 → 编译产物（要求逐字节一致，含 item 键序直通语义）
- caption 分支（官方返回纯文本）
- JSON 文本层（重复键 / NaN / Infinity / 非法 JSON 双方都必须拒绝）
不覆盖（我方扩展，官方无对应入口，由 test_compile_sniff 回环用例保障）：
validate_compiled、_reverse_compiled（已编译形态反解）。

断言哲学：合法用例比输出字节；非法用例只比"双方都拒"（错误文案有意不同：
官方英文 / 我方中文面向用户）。
"""
from helpers import CUP  # noqa: F401  (helpers 先导入以初始化 sys.path)

import copy
import hashlib
import importlib.util
import json
import math
import os
import random

from py.compile_prompt import compile_draft  # noqa: F401
from py.sniff import SniffError, detect, strict_loads  # noqa: F401

_VENDOR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "vendor", "compile_prompt_official.py")
_VENDOR_SHA256 = "2462f5d51b19fbbb1ba42e6ec7882e616d27c1ad67a262a1aea7110ef6faf6f4"

_spec = importlib.util.spec_from_file_location("compile_prompt_official", _VENDOR_PATH)
official = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(official)


def _official_text(text):
    """官方全链路：JSON 文本 →（官方解析钩子）→ compile_prompt。"""
    obj = json.loads(text, object_pairs_hook=official.unique_object,
                     parse_constant=official.reject_constant)
    return official.compile_prompt(obj)


def _ours_text(text):
    """我方全链路：JSON 文本 → strict_loads → detect → caption / compile_draft。

    与 nodes/compiler.py 的 run() 逐行同构（caption 纯文本 / draft 编译）。
    已编译形态不在差分范围（官方拒绝该输入，我方反解属扩展）。
    """
    obj = strict_loads(text)
    kind = detect(obj)
    if kind == "caption":
        cap = obj.get("caption")
        if not isinstance(cap, str) or not cap.strip():
            raise SniffError("caption 必须是非空字符串")
        return cap
    if kind == "draft":
        return compile_draft(obj)
    raise AssertionError(f"语料含已编译形态，不在差分范围: {kind}")


def _differ(label, official_fn, ours_fn):
    """差分断言：双方都接受 → 输出一致；双方都拒 → 通过；单边拒绝 → 失败。"""
    try:
        off, off_err = official_fn(), None
    except Exception as e:  # noqa: BLE001  (官方抛 ValueError/JSONDecodeError 等)
        off, off_err = None, e
    try:
        ours, ours_err = ours_fn(), None
    except Exception as e:  # noqa: BLE001
        ours, ours_err = None, e
    if off_err is None and ours_err is None:
        assert off == ours, f"{label}: 输出不一致\n官方: {off!r}\n我方: {ours!r}"
    elif off_err is not None and ours_err is not None:
        pass  # 双方都拒（文案有意不同）
    else:
        raise AssertionError(f"{label}: 拒绝行为不一致 官方err={off_err!r} 我方err={ours_err!r}")


# ---------- 语料 ----------


def _layout(width, height, summary, items):
    return {"width": width, "height": height, "bbox_coord_space": "abs",
            "bbox_format": "xyxy", "summary": summary, "items": items}


_VALID_DICTS = [
    ("官方 layout.md 杯子例", CUP),
    ("整数框+空数组+非text带text", _layout(1024, 1024,
        {"caption": "极简旅行海报，米色背景。", "meta": [], "palette": [], "canvas": "single"},
        [{"label": "object", "caption": "蓝色山脉插画。", "bbox": [128, 384, 896, 896],
          "tags": [], "palette": [], "text": "官方允许的附带文字"}])),
    ("小数边框(.5)", _layout(512, 512,
        {"caption": "soft gradient study"},
        [{"label": "object", "caption": "rounded blob", "bbox": [0.5, 0.5, 511.5, 511.5]}])),
    ("9位小数尘埃", _layout(512, 512,
        {"caption": "dust"},
        [{"label": "object", "caption": "dusty", "bbox": [0.9999999994, 0.0000000004, 300.9999999996, 200.0000000004]}])),
    ("整画布框", _layout(16, 16, {"caption": "full"},
        [{"label": "object", "caption": "everything", "bbox": [0, 0, 16, 16]}])),
    ("text 带 \\n 与中文", _layout(1024, 1024,
        {"caption": "中文海报。"},
        [{"label": "text", "caption": "两行主标题。", "text": "一杯温暖\n一刻从容", "bbox": [256, 96, 768, 448]},
         {"label": "text", "caption": "角标。", "text": "MOSS", "bbox": [16, 960, 200, 1008]}])),
    ("multi_panel/collage/unicode", _layout(2048, 1024,
        {"caption": "四格知识图解，含真实说明文字 ☂", "canvas": "multi_panel",
         "meta": ["infographic"], "palette": ["#F4EFE6", "#223344"]},
        [{"label": "text", "caption": "标题。", "text": "水循环循环水", "bbox": [64, 32, 1984, 160]},
         {"label": "character", "caption": "讲解小人 ☂", "bbox": [100, 200, 300, 600]}])),
    ("键序倒置（bbox 在前）", _layout(1024, 1024, {"caption": "key order"},
        [{"bbox": [10, 10, 100, 100], "caption": "reordered", "label": "object"},
         {"text": "T", "bbox": [200, 200, 400, 300], "caption": "t", "label": "text"}])),
    ("负零与紧凑坐标", _layout(1024, 1024, {"caption": "neg zero"},
        [{"label": "object", "caption": "z", "bbox": [-0.0, 0, 512, 512]}])),
    ("caption U+2028 全角空格与标点", {"caption": "第一行第二行　全角空格，标点！"}),
    ("caption 英文长文", {"caption": "A " * 400 + "end"}),
    ("caption U+2028 行分隔符", {"caption": "第一行\u2028第二行"}),
]

# (标签, dict) —— 官方与我方都必须拒绝；width 的字符串/布尔/浮点/零在文本层覆盖
_INVALID_DICTS = [
    ("width 非16倍数", {**_layout(1000, 1024, {"caption": "x"}, [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}])}),
    ("coord_space 错", {**_layout(1024, 1024, {"caption": "x"}, [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}]), "bbox_coord_space": "pixel"}),
    ("bbox_format 错", {**_layout(1024, 1024, {"caption": "x"}, [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}]), "bbox_format": "xywh"}),
    ("缺 summary", {"width": 1024, "height": 1024, "bbox_coord_space": "abs", "bbox_format": "xyxy", "items": []}),
    ("caption 空白", _layout(1024, 1024, {"caption": "  "}, [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}])),
    ("summary 未知字段", {**_layout(1024, 1024, {"caption": "x"}, [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}]), "summary": {"caption": "x", "style": "y"}}),
    ("canvas 非法", _layout(1024, 1024, {"caption": "x", "canvas": "grid"}, [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}])),
    ("meta 非数组", _layout(1024, 1024, {"caption": "x", "meta": "tags"}, [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}])),
    ("meta 空串元素", _layout(1024, 1024, {"caption": "x", "meta": [""]}, [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}])),
    ("meta null 元素", _layout(1024, 1024, {"caption": "x", "meta": [None]}, [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}])),
    ("items 空", _layout(1024, 1024, {"caption": "x"}, [])),
    ("item 缺 caption", _layout(1024, 1024, {"caption": "x"}, [{"label": "object", "bbox": [0, 0, 10, 10]}])),
    ("item 未知字段", _layout(1024, 1024, {"caption": "x"}, [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10], "id": 1}])),
    ("label 非法", _layout(1024, 1024, {"caption": "x"}, [{"label": "text2", "caption": "c", "bbox": [0, 0, 10, 10]}])),
    ("item caption 空白", _layout(1024, 1024, {"caption": "x"}, [{"label": "object", "caption": " ", "bbox": [0, 0, 10, 10]}])),
    ("bbox 长度3", _layout(1024, 1024, {"caption": "x"}, [{"label": "object", "caption": "c", "bbox": [1, 2, 3]}])),
    ("bbox 越界", _layout(1024, 1024, {"caption": "x"}, [{"label": "object", "caption": "c", "bbox": [0, 0, 1025, 10]}])),
    ("bbox 相等塌缩", _layout(1024, 1024, {"caption": "x"}, [{"label": "object", "caption": "c", "bbox": [5, 5, 5, 10]}])),
    ("bbox 布尔", _layout(1024, 1024, {"caption": "x"}, [{"label": "object", "caption": "c", "bbox": [True, 0, 10, 10]}])),
    ("bbox 字符串", _layout(1024, 1024, {"caption": "x"}, [{"label": "object", "caption": "c", "bbox": ["0", 0, 10, 10]}])),
    ("text item 缺 text", _layout(1024, 1024, {"caption": "x"}, [{"label": "text", "caption": "c", "bbox": [0, 0, 10, 10]}])),
    ("text item 空 text", _layout(1024, 1024, {"caption": "x"}, [{"label": "text", "caption": "c", "text": "", "bbox": [0, 0, 10, 10]}])),
    ("非 text item 空 text", _layout(1024, 1024, {"caption": "x"}, [{"label": "object", "caption": "c", "text": " ", "bbox": [0, 0, 10, 10]}])),
    ("tags 数字元素", _layout(1024, 1024, {"caption": "x"}, [{"label": "object", "caption": "c", "tags": [3], "bbox": [0, 0, 10, 10]}])),
    ("round 后塌缩", _layout(16, 16, {"caption": "x"}, [{"label": "object", "caption": "c", "bbox": [1.999999999, 0, 2.0, 10]}])),
    ("caption+未知字段", {"caption": "x", "style": "y"}),
    ("caption 非字符串", {"caption": 123}),
    ("caption 布尔", {"caption": True}),
    ("caption+items 混合", {"caption": "x", "items": []}),
]

_INVALID_TEXTS = [
    ("重复键", '{"caption":"a","caption":"b"}'),
    ("NaN", '{"caption": NaN}'),
    ("Infinity", '{"caption": Infinity}'),
    ("-Infinity", '{"caption": -Infinity}'),
    ("width NaN", '{"width": NaN, "height": 16, "bbox_coord_space": "abs", "bbox_format": "xyxy", "summary": {"caption": "x"}, "items": [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}]}'),
    ("非法 JSON", "{caption: 1}"),
    ("顶层数组", "[1,2]"),
    ("顶层字符串", '"caption"'),
    ("顶层 null", "null"),
    ("width 字符串", '{"width": "1024", "height": 1024, "bbox_coord_space": "abs", "bbox_format": "xyxy", "summary": {"caption": "x"}, "items": [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}]}'),
    ("width 布尔", '{"width": true, "height": 1024, "bbox_coord_space": "abs", "bbox_format": "xyxy", "summary": {"caption": "x"}, "items": [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}]}'),
    ("width 浮点", '{"width": 1024.0, "height": 1024, "bbox_coord_space": "abs", "bbox_format": "xyxy", "summary": {"caption": "x"}, "items": [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}]}'),
    ("width 零", '{"width": 0, "height": 1024, "bbox_coord_space": "abs", "bbox_format": "xyxy", "summary": {"caption": "x"}, "items": [{"label": "object", "caption": "c", "bbox": [0, 0, 10, 10]}]}'),
]

_CAPTION_CASES = [
    ("中文", {"caption": "你好，世界"}),
    ("含换行转义", {"caption": "第一行\\n第二行"}),
    ("英文", {"caption": "A calm studio scene."}),
]


# ---------- 测试 ----------


def test_vendor_integrity():
    """vendor 文件必须与记录的 sha256 一致：改动 vendor = 有意 re-baseline。"""
    with open(_VENDOR_PATH, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    assert digest == _VENDOR_SHA256, (
        "tests/vendor/compile_prompt_official.py 已改动——请确认是官方上游变更的"
        "有意 re-baseline（更新文件头 commit 与本用例的 _VENDOR_SHA256），"
        "并重跑全部差分用例")


def test_differential_valid_layout_and_caption():
    """合法语料：官方与我方输出逐字节一致（dict 层 + JSON 文本层）。"""
    for label, d in _VALID_DICTS:
        text = json.dumps(d, ensure_ascii=False)
        _differ(f"[dict] {label}", lambda d=d: official.compile_prompt(d),
                lambda d=d: _differ_dict_ours(d))
        _differ(f"[text] {label}", lambda t=text: _official_text(t),
                lambda t=text: _ours_text(t))


def _differ_dict_ours(d):
    """我方 dict 层编译：与 nodes/compiler.py 的 run() 同构（detect 分流）。"""
    kind = detect(d)
    if kind == "caption":
        cap = d.get("caption")
        if not isinstance(cap, str) or not cap.strip():
            raise SniffError("caption 必须是非空字符串")
        return cap
    return compile_draft(d)


def test_differential_invalid_both_reject():
    for label, d in _INVALID_DICTS:
        if d is None:
            continue
        _differ(f"[dict] {label}", lambda d=d: official.compile_prompt(d),
                lambda d=d: _differ_dict_ours(d))
    for label, text in _INVALID_TEXTS:
        _differ(f"[text] {label}", lambda t=text: _official_text(t),
                lambda t=text: _ours_text(t))


def test_differential_caption_branch():
    """caption 分支：官方返回纯文本；我方节点同构逻辑产出一致。"""
    for label, d in _CAPTION_CASES:
        expect = d["caption"]
        assert official.compile_prompt(dict(d)) == expect, label
        assert _differ_dict_ours(dict(d)) == expect, label
    # 已编译形态：官方拒绝；我方属扩展（反解），差分明确不覆盖该输入
    compiled = json.loads(compile_draft(CUP))
    try:
        official.compile_prompt(compiled)
        raised = False
    except ValueError:
        raised = True
    assert raised, "官方必须拒绝已编译形态输入（abs_centralized 不是合法草稿坐标口径）"


def test_compile_preserves_item_key_order():
    """item 键序按输入直通（官方语义，2026-11-01 对齐；此前归一化为 label,caption,bbox 序）。"""
    d = copy.deepcopy(CUP)
    d["items"][0] = {"bbox": [176, 224, 800, 864],
                     "caption": "An ivory ceramic cup.", "label": "object"}
    out = json.loads(compile_draft(d))
    assert list(out["items"][0].keys()) == ["bbox", "caption", "label"]


# ---------- 固定种子 fuzz 差分 ----------


_CAPTION_POOL = [
    "一场激烈战斗，雨夜城市废墟。无文字，无水印。",
    "A warm studio campaign with restrained typography.",
    "知识图解：光合作用如何把光变成糖 ☂（含真实说明）",
    "极简海报，米色背景蓝色山脉",
    "把日常，过成诗。",
]
_TEXT_POOL = ["探索世界", "一杯温暖\n一刻从容", "MOSS", "2026", "雨 ☂ 晴",
              "Long long title text for the poster header"]
_WORD_POOL = ["night", "rain", "neon", "single", "poster", "夜", "雨", "单色"]


def _rand_bbox(rng, w, h):
    x0 = rng.uniform(0, max(0.0, w - 4.0))
    y0 = rng.uniform(0, max(0.0, h - 4.0))
    x1 = rng.uniform(x0 + 0.5, float(w))
    y1 = rng.uniform(y0 + 0.5, float(h))
    style = rng.random()
    if style < 0.3:
        return [int(x0), int(y0), int(math.ceil(x1)), int(math.ceil(y1))]
    if style < 0.6:
        return [round(x0, 9), round(y0, 9), round(x1, 9), round(y1, 9)]
    return [x0, y0, x1, y1]


def _fuzz_case(rng):
    """生成一个草稿/caption dict；键序随机洗牌；允许合法空数组与非 text text。"""
    if rng.random() < 0.1:
        return {"caption": rng.choice(_CAPTION_POOL)}
    w = rng.choice([16, 512, 768, 928, 1024, 1280, 1536, 1648, 2048])
    h = rng.choice([16, 512, 768, 928, 1024, 1280, 1536, 1648, 2048])
    summary = {"caption": rng.choice(_CAPTION_POOL)}
    if rng.random() < 0.5:
        summary["meta"] = rng.choice([[], [rng.choice(_WORD_POOL), rng.choice(_WORD_POOL)]])
    if rng.random() < 0.4:
        summary["canvas"] = rng.choice(["single", "multi_panel", "collage"])
    if rng.random() < 0.4:
        summary["palette"] = rng.choice([[], ["#F4EFE6"], ["#F4EFE6", "#223344"]])
    items = []
    for _ in range(rng.randint(1, 6)):
        label = rng.choice(["object", "character", "text"])
        bbox = _rand_bbox(rng, w, h)
        fields = {"label": label, "caption": rng.choice(_CAPTION_POOL), "bbox": bbox}
        if rng.random() < 0.3:
            fields["tags"] = rng.choice([[], [rng.choice(_WORD_POOL)]])
        if rng.random() < 0.3:
            fields["palette"] = rng.choice([[], ["#112233"]])
        if label == "text" or rng.random() < 0.15:
            fields["text"] = rng.choice(_TEXT_POOL)
        order = list(fields)
        rng.shuffle(order)  # 洗牌键序，对拍官方"输入序直通"语义
        items.append({k: fields[k] for k in order})
    return _layout(w, h, summary, items)


def test_differential_fuzz_seeded():
    """固定种子 300 例：接受则逐字节一致，拒绝则双方都拒；种子写死保证可复现。"""
    rng = random.Random(20261101)
    n_accept = n_reject = 0
    for i in range(300):
        d = _fuzz_case(rng)
        try:
            off, off_err = official.compile_prompt(d), None
        except Exception as e:  # noqa: BLE001
            off, off_err = None, e
        try:
            ours, ours_err = _differ_dict_ours(d), None
        except Exception as e:  # noqa: BLE001
            ours, ours_err = None, e
        if off_err is None and ours_err is None:
            n_accept += 1
            assert off == ours, f"fuzz[{i}] 输出不一致\n官方: {off!r}\n我方: {ours!r}\n输入: {d!r}"
        elif off_err is not None and ours_err is not None:
            n_reject += 1
        else:
            raise AssertionError(
                f"fuzz[{i}] 拒绝行为不一致 官方err={off_err!r} 我方err={ours_err!r}\n输入: {d!r}")
    assert n_accept > 200, f"fuzz 有效接受样本过少（accept={n_accept}），语料生成器疑似退化"
    print(f"\n[fuzz] accept={n_accept} both_reject={n_reject} seed=20261101")
