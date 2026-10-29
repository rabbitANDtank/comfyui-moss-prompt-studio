"""提示词三形态自动识别与归一：caption / layout 草稿 / 已编译单行 JSON。"""
from __future__ import annotations

import json

try:
    from .compile_prompt import DraftError, validate_draft
except ImportError:  # 兼容非包方式导入（旧版 ComfyUI 加载器）
    from compile_prompt import DraftError, validate_draft


class SniffError(ValueError):
    """粘贴内容无法解析或形态无法识别。"""


def _no_dup_pairs(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise SniffError(f"JSON 中存在重复键: {k!r}")
        d[k] = v
    return d


def _reject_const(token):
    raise SniffError(f"JSON 中不允许出现 {token}（NaN/Infinity）")


def strict_loads(text: str):
    """严格 JSON 解析：拒绝重复键与 NaN/Infinity（对齐官方编译器行为）。"""
    try:
        return json.loads(text, object_pairs_hook=_no_dup_pairs, parse_constant=_reject_const)
    except SniffError:
        raise
    except json.JSONDecodeError as e:
        raise SniffError(f"不是合法 JSON: {e}") from e


def detect(obj) -> str:
    """识别 dict 形态：'caption' | 'draft' | 'compiled'；识别不了抛 SniffError。"""
    if not isinstance(obj, dict):
        raise SniffError("粘贴内容必须是 JSON 对象")
    keys = set(obj)
    if keys == {"caption"}:
        return "caption"
    if "items" in obj or "summary" in obj:
        cs = obj.get("bbox_coord_space")
        bf = obj.get("bbox_format")
        if cs == "abs" and bf == "xyxy":
            return "draft"
        if cs == "abs_centralized" and bf == "yxyx":
            return "compiled"
        raise SniffError(
            "无法识别的坐标口径: 需要 草稿(abs/xyxy) 或 已编译(abs_centralized/yxyx)，"
            f"当前 bbox_coord_space={cs!r}, bbox_format={bf!r}"
        )
    raise SniffError(
        '无法识别的 JSON 形态: 既不是 {"caption": ...} 也不是带 summary/items 的布局提示词'
    )


def _reverse_compiled(obj: dict, width: int, height: int) -> dict:
    """已编译（中心原点 yxyx）→ 草稿（左上原点 xyxy），按给定画布尺寸反解。"""
    summary = obj.get("summary")
    if not isinstance(summary, dict):
        raise SniffError("已编译提示词缺少 summary 对象")
    raw_items = obj.get("items")
    if not isinstance(raw_items, list):
        raise SniffError("已编译提示词的 items 必须是数组")
    items = []
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise SniffError(f"已编译提示词 items[{idx}] 必须是对象")
        bbox = raw.get("bbox")
        if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in bbox)):
            raise SniffError(
                f"已编译提示词 items[{idx}].bbox 必须是四个数字（yxyx 中心原点）"
            )
        it = dict(raw)
        y0, x0, y1, x1 = bbox
        it["bbox"] = [
            int(round(x0 + width / 2)),
            int(round(y0 + height / 2)),
            int(round(x1 + width / 2)),
            int(round(y1 + height / 2)),
        ]
        items.append(it)
    return {
        "width": width,
        "height": height,
        "bbox_coord_space": "abs",
        "bbox_format": "xyxy",
        "summary": dict(summary),
        "items": items,
    }


def sniff(text: str, default_width: int = 1024, default_height: int = 1024) -> dict:
    """把粘贴文本归一为 {"kind": "caption"|"layout", "draft": dict}。

    - caption: {"caption": str}
    - layout 草稿: 原样返回（含完整校验）
    - 已编译: 按 default_width/height 反解为草稿（只做结构校验，不查边界——
      真实画布尺寸未知，若反解后边界可疑，执行期完整校验会给出明确报错）
    - 纯文本（非 { 开头）：视作 caption 内容，自动包 {"caption": ...} 壳
    """
    s = (text or "").strip()
    if not s:
        raise SniffError("粘贴源为空")
    if not (s.startswith("{") or s.startswith("[")):
        # 纯文本散文 → caption 形态（官方 skill 对普通场景的产物就是一段连贯描述）
        return {"kind": "caption", "draft": {"caption": s}}
    obj = strict_loads(s)
    kind = detect(obj)

    if kind == "caption":
        cap = obj["caption"]
        if not isinstance(cap, str) or not cap.strip():
            raise SniffError("caption 必须是非空字符串")
        return {"kind": "caption", "draft": {"caption": cap}}

    if kind == "draft":
        validate_draft(obj)
        return {"kind": "layout", "draft": obj}

    d = _reverse_compiled(obj, int(default_width), int(default_height))
    try:
        validate_draft(d, check_bounds=False)
    except DraftError as e:
        raise SniffError(
            f"按 {default_width}x{default_height} 反解已编译 JSON 后结构不合法: {e}\n"
            "提示: 粘贴已编译格式时，请把 canvas_width/canvas_height 调成与原图一致。"
        ) from e
    return {"kind": "layout", "draft": d}
