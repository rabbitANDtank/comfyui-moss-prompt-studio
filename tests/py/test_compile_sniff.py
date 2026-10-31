"""compile / sniff：官方口径编译（含官方文档期望坐标）与三形态识别反解往返。"""
from helpers import COMPILED_POSTER, CUP, _expect  # noqa: F401  (helpers 先导入以初始化 sys.path)

import copy
import json

from py.compile_prompt import (DraftError, compile_bbox, compile_draft,  # noqa: F401
                               validate_compiled, validate_draft)
from py.sniff import SniffError, sniff, strict_loads  # noqa: F401


# ---------- 编译器 ----------


def test_compile_official_examples():
    s = compile_draft(CUP)
    assert "\n" not in s, "编译产物必须是单行"
    out = json.loads(s)
    assert out["bbox_coord_space"] == "abs_centralized" and out["bbox_format"] == "yxyx"
    assert "width" not in out and "height" not in out
    assert out["items"][0]["bbox"] == [-288, -592, 352, 32], "官方 layout.md 给出的期望值"
    assert out["items"][1]["bbox"] == [-240, 160, -64, 640], "官方 test_compile_prompt.py 断言值"
    assert out["items"][1]["text"] == "把日常，过成诗。"


def test_validate_rejects():
    import copy

    _expect(DraftError, validate_draft, {**CUP, "width": 1000})          # 非 16 倍数
    _expect(DraftError, validate_draft, {**CUP, "extra": 1})             # 未知顶层字段
    bad = copy.deepcopy(CUP); bad["items"][0]["id"] = "x"
    _expect(DraftError, validate_draft, bad)                             # 未知 item 字段
    bad = copy.deepcopy(CUP); bad["items"][0]["bbox"] = [0, 0, 9999, 900]
    _expect(DraftError, validate_draft, bad)                             # 越界
    bad = copy.deepcopy(CUP); del bad["items"][1]["text"]
    _expect(DraftError, validate_draft, bad)                             # text 元素缺 text
    # 官方允许空 meta/tags/palette 数组、允许非 text item 携带 text
    ok = copy.deepcopy(CUP)
    ok["summary"]["meta"] = []
    ok["summary"]["palette"] = []
    ok["items"][0]["tags"] = []
    ok["items"][0]["palette"] = []
    ok["items"][0]["text"] = "官方允许的附带文字"
    validate_draft(ok)
    compile_draft(ok)
    bad = copy.deepcopy(CUP); bad["items"][0]["caption"] = "  "
    _expect(DraftError, validate_draft, bad)                             # 空 caption
    bad = copy.deepcopy(CUP); bad["items"] = []
    _expect(DraftError, validate_draft, bad)                             # 空 items
    _expect(DraftError, validate_compiled, {**COMPILED_POSTER, "width": 1024})  # 编译态不该有 width
    badc = copy.deepcopy(COMPILED_POSTER); badc["items"][0]["bbox"] = [-416.0, -384, -288, 384]
    _expect(DraftError, validate_compiled, badc)                              # 编译态 bbox 必须是整数
    badc = copy.deepcopy(COMPILED_POSTER); badc["items"][0]["bbox"] = [-288, -384, -416, 384]
    _expect(DraftError, validate_compiled, badc)                              # y0 必须小于 y1
    badc = copy.deepcopy(COMPILED_POSTER); badc["items"][0]["bbox"] = [-416, 384, -288, -384]
    _expect(DraftError, validate_compiled, badc)                              # x0 必须小于 x1


def test_compiled_contract_matches_draft():
    """草稿与已编译形态的 text/meta/tags/palette 规则必须一致。

    validate_draft 允许空 meta/tags/palette 与非 text item 携带非空 text；
    validate_compiled 以前会拒绝后者，导致 compile_draft 的合法产物无法回环。
    """
    d = copy.deepcopy(CUP)
    d["summary"]["meta"] = []
    d["summary"]["palette"] = []
    d["items"][0]["tags"] = []
    d["items"][0]["palette"] = []
    d["items"][0]["text"] = "官方允许的附带文字"

    # 空数组与非 text item 的 text 在草稿形态合法
    validate_draft(d)
    compiled_str = compile_draft(d)
    compiled = json.loads(compiled_str)
    assert compiled["items"][0]["text"] == "官方允许的附带文字"
    assert compiled["summary"]["meta"] == [] and compiled["summary"]["palette"] == []
    assert compiled["items"][0]["tags"] == [] and compiled["items"][0]["palette"] == []

    # 已编译形态必须接受同样的字段，且无损回环
    assert validate_compiled(compiled) == compiled_str
    assert json.loads(validate_compiled(json.loads(compiled_str))) == compiled

    # 契约只放宽“非 text item 可携带 text”，空 text 仍必须拒绝
    bad = copy.deepcopy(compiled)
    bad["items"][0]["text"] = "  "
    _expect(DraftError, validate_compiled, bad)
    # text item 缺 text 仍必须拒绝
    bad = copy.deepcopy(compiled)
    del bad["items"][1]["text"]
    _expect(DraftError, validate_compiled, bad)


def test_compile_bbox_official_rounding():
    """官方先 round(v, 9) 再 floor/ceil；编译后塌缩的框必须拒绝。"""
    assert compile_bbox([0.9999999999, 0.9999999999, 2.0, 2.0], 16, 16) == [-7, -7, -6, -6]
    _expect(DraftError, compile_bbox,
            [0.0000000001, 0.0000000001, 0.0000000002, 0.0000000002], 16, 16)


def test_sniff_forms_and_roundtrip():
    # caption 形态
    info = sniff('{"caption":"你好，世界"}')
    assert info == {"kind": "caption", "draft": {"caption": "你好，世界"}}
    # 草稿形态原样
    info = sniff(json.dumps(CUP, ensure_ascii=False))
    assert info["kind"] == "layout" and info["draft"]["width"] == 1536
    # 已编译形态反解（1024x1024）→ 再编译应与原编译完全一致
    info = sniff(json.dumps(COMPILED_POSTER, ensure_ascii=False), 1024, 1024)
    assert info["draft"]["items"][0]["bbox"] == [128, 96, 896, 224]
    again = json.loads(compile_draft(info["draft"]))
    assert again == COMPILED_POSTER, "反解→重编译应还原"
    # 坏输入
    _expect(SniffError, sniff, "")
    _expect(SniffError, sniff, '{"caption":"a","caption":"b"}')     # 重复键
    _expect(SniffError, sniff, '{"caption": NaN}')                  # NaN
    _expect(SniffError, sniff, '{"foo": 1}')                        # 形态不识别
    _expect(SniffError, strict_loads, '{"a":1,"a":2}')
    # 纯文本散文 → 自动包 caption 壳
    info = sniff("电影感特摄动作插画，一场激烈战斗。无文字，无水印。")
    assert info == {"kind": "caption", "draft": {"caption": "电影感特摄动作插画，一场激烈战斗。无文字，无水印。"}}


# ---------- 已编译形态反解的友好报错 ----------


def test_sniff_compiled_errors_are_friendly():
    _expect(SniffError, sniff, json.dumps({
        "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx",
    }))
    _expect(SniffError, sniff, json.dumps({
        "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx",
        "summary": {}, "items": [{"bbox": [0, 0, 1]}],
    }))
    _expect(SniffError, sniff, json.dumps({
        "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx",
        "summary": {}, "items": [1],
    }))
    _expect(SniffError, sniff, json.dumps({
        "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx",
        "summary": [], "items": [],
    }))
