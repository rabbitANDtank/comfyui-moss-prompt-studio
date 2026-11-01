"""前后端契约常量对拍：py 常量 vs web/js 源码里手抄的同名常量。

这些常量双语各写一份且无法运行时下发（marker 要进 LLM prompt、反推预设要进
INPUT_TYPES），只能靠对拍测试防漂移（审查 P1-13）。JS 侧用正则提取字面量，
格式敏感是刻意的：改动 JS 常量写法时本文件会红，提醒同步另一侧。
"""
from helpers import _expect  # noqa: F401  (helpers 先导入以初始化 sys.path)

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _js(rel):
    return (ROOT / "web" / "js" / rel).read_text(encoding="utf-8")


def _js_val(v):
    v = v.strip().rstrip(",")
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    if v == "true":
        return True
    if v == "false":
        return False
    try:
        return int(v)
    except ValueError:
        return float(v)


def _js_flat_objects(block):
    """解析一层扁平的 JS 对象字面量 { mode: { k: v, ... }, ... } 为 dict。"""
    out = {}
    for key, body in re.findall(r"(\w+):\s*\{(.*?)\}", block, flags=re.S):
        out[key] = {k: _js_val(v) for k, v in re.findall(r'(\w+):\s*("[^"]*"|[^,\n}]+)', body)}
    return out


def test_caption_segment_markers():
    from py.caption_segments import COSTUME_MARKER, OCCL_BACK, OCCL_FRONT, POSE_MARKER
    pose = _js("editor/pose.js")
    assert f'export const POSE_MARKER = "{POSE_MARKER}"' in pose, "POSE_MARKER 双语漂移"
    assert f'export const COSTUME_MARKER = "{COSTUME_MARKER}"' in pose, "COSTUME_MARKER 双语漂移"
    model = _js("editor/model.js")
    assert f'export const OCCL_FRONT = "{OCCL_FRONT}"' in model, "OCCL_FRONT 双语漂移"
    assert f'export const OCCL_BACK = "{OCCL_BACK}"' in model, "OCCL_BACK 双语漂移"


def test_pose_costume_modes():
    from py.inject_costume import COSTUME_MODES
    from py.inject_pose import POSE_MODES
    pose = _js("editor/pose.js")
    assert f'export const POSE_MODES = {repr(list(POSE_MODES)).replace(chr(39), chr(34))}' in pose
    assert f'export const COSTUME_MODES = {repr(list(COSTUME_MODES)).replace(chr(39), chr(34))}' in pose


def test_costume_keywords():
    from py.inject_costume import _COSTUME_OBJECT_KEYWORDS
    m = re.search(r"export const COSTUME_KEYWORDS = \[([^\]]*)\]", _js("editor/pose.js"))
    assert m, "pose.js 找不到 COSTUME_KEYWORDS"
    js = [x.strip().strip('"') for x in m.group(1).split(",") if x.strip()]
    assert js == list(_COSTUME_OBJECT_KEYWORDS), "COSTUME_KEYWORDS 双语漂移"


def test_official_key_sets():
    from py.compile_prompt import ITEM_KEYS, SUMMARY_KEYS, TOP_KEYS, VALID_CANVAS, VALID_LABELS
    model = _js("editor/model.js")
    consts = {k: {x.strip().strip('"') for x in arr.split(",")}
              for k, arr in re.findall(r"const (\w+) = \[([^\]]*)\]", model)}
    assert consts["TOP_KEYS"] == set(TOP_KEYS), "TOP_KEYS 双语漂移"
    assert consts["SUMMARY_KEYS"] == set(SUMMARY_KEYS), "SUMMARY_KEYS 双语漂移"
    assert consts["ITEM_KEYS"] == set(ITEM_KEYS), "ITEM_KEYS 双语漂移"
    assert consts["VALID_LABELS"] == set(VALID_LABELS), "VALID_LABELS 双语漂移"
    assert consts["VALID_CANVAS"] == set(VALID_CANVAS), "VALID_CANVAS 双语漂移"


def test_reverse_mode_presets():
    from nodes.image_reverse import MODE_PRESETS, TEMPERATURE_PRESETS
    rm = _js("editor/reverse_modes.js")
    mv = re.search(r"export const MODE_VALUES = \{(.*?)\n\};", rm, flags=re.S)
    mt = re.search(r"export const MODE_TEMPERATURES = \{(.*?)\n\};", rm, flags=re.S)
    assert mv and mt, "reverse_modes.js 预设结构变了，请同步更新本对拍测试"
    js_values = _js_flat_objects(mv.group(1))
    js_temps = _js_flat_objects(mt.group(1))
    assert set(js_values) == set(MODE_PRESETS), "反推模式档位双语不一致"
    for mode, preset in MODE_PRESETS.items():
        for k, v in preset.items():
            assert js_values[mode].get(k) == v, f"MODE_VALUES.{mode}.{k} 双语漂移: {js_values[mode].get(k)!r} != {v!r}"
    for mode, temps in TEMPERATURE_PRESETS.items():
        for p, v in temps.items():
            assert js_temps[mode].get(p) == v, f"MODE_TEMPERATURES.{mode}.{p} 双语漂移"


def test_fnv_signature_golden():
    """前后端各自实现同一 FNV-1a；任一侧算法漂移此黄金值即红。"""
    from py.llm_client import source_signature
    assert source_signature("MOSS测试abc123") == "fnv1a:26c84941"
