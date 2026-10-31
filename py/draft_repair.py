"""AI 拆解草稿的本地宽容与修复：数组归一、字段收敛、宽高/bbox 缩放夹取。"""
from __future__ import annotations

import math

def _forgive_llm_sloppiness(obj):
    """仅用于 AI 拆解产物：宽容 LLM 常见手误。

    - meta/tags/palette 被写成单个字符串 → 按逗号拆成数组（中英文逗号都认）
    - 数组元素过滤空白；归一后为空则删掉该可选字段（官方要求非空数组）
    """
    def coerce(container, key):
        v = container.get(key)
        if isinstance(v, str):
            parts = [x.strip() for x in v.replace("，", ",").split(",") if x.strip()] if v.strip() else []
        elif isinstance(v, list):
            parts = [str(x).strip() for x in v if str(x).strip()]
        else:
            return
        if parts:
            container[key] = parts
        else:
            container.pop(key, None)

    if not isinstance(obj, dict):
        return obj
    s = obj.get("summary")
    if isinstance(s, dict):
        for k in ("meta", "palette"):
            coerce(s, k)
    items = obj.get("items")
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                for k in ("tags", "palette"):
                    coerce(it, k)
    return obj


_AI_TOP_KEYS = {"width", "height", "bbox_coord_space", "bbox_format", "summary", "items"}
_AI_SUMMARY_KEYS = {"caption", "meta", "canvas", "palette"}
_AI_ITEM_KEYS = {"label", "caption", "bbox", "text", "tags", "palette"}


def _as_num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _repair_ai_draft(obj, width: int, height: int, fallback_caption: str = ""):
    """仅用于 AI 拆解：把模型的常见偏差就地修掉，避免为了一个坏框整轮重试。

    - 删除 schema 外的顶层/元素字段（模型常多加 text/type/description）
    - 宽高/坐标口径强制改成目标画布；模型给了别的宽高时按比例缩放 bbox
    - bbox 排序、缩放并夹取到画布内，保证 x0<x1、y0<y1
    - 非 text 元素误带的 text 字段删掉
    结构与必填字段（caption/label/text）仍交给官方校验；修不好就继续重试。
    """
    if not isinstance(obj, dict):
        return obj
    notes = []
    # 模型常把 summary 的字段直接放到顶层；先收好，删完多余字段后再重建 summary
    src_summary = obj.get("summary")
    stray = {k: obj.get(k) for k in _AI_SUMMARY_KEYS if k in obj}

    for k in list(obj):
        if k not in _AI_TOP_KEYS:
            obj.pop(k, None)
            notes.append(f"顶层.{k}")

    src_w, src_h = _as_num(obj.get("width")), _as_num(obj.get("height"))
    scale_x = width / src_w if src_w and src_w > 0 else 1.0
    scale_y = height / src_h if src_h and src_h > 0 else 1.0
    if src_w and src_h and (abs(src_w - width) > 1 or abs(src_h - height) > 1):
        notes.append(f"宽高 {src_w:g}x{src_h:g}→{width}x{height}")

    obj["width"], obj["height"] = int(width), int(height)
    obj["bbox_coord_space"], obj["bbox_format"] = "abs", "xyxy"

    s = obj.get("summary")
    if isinstance(s, dict):
        # 顶层散落的 caption/meta/canvas/palette 收回 summary
        for k, v in stray.items():
            if k not in s and v is not None:
                s[k] = v
                notes.append(f"summary.{k}←顶层")
        for k in list(s):
            if k not in _AI_SUMMARY_KEYS:
                s.pop(k, None)
                notes.append(f"summary.{k}")
        if fallback_caption and not str(s.get("caption") or "").strip():
            s["caption"] = fallback_caption.strip()
            notes.append("summary.caption 用原文兜底")
    else:
        # summary 整个缺失或不是对象：用顶层字段重建
        rebuilt = {}
        if isinstance(src_summary, str) and src_summary.strip():
            rebuilt["caption"] = src_summary.strip()
        for k, v in stray.items():
            if v is not None and k not in rebuilt:
                rebuilt[k] = v
        if not rebuilt.get("caption") and fallback_caption.strip():
            rebuilt["caption"] = fallback_caption.strip()
            notes.append("summary 用原文兜底")
        if rebuilt:
            obj["summary"] = rebuilt
            notes.append("summary 由顶层字段重建")

    items = obj.get("items")
    if isinstance(items, list):
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            for k in list(it):
                if k not in _AI_ITEM_KEYS:
                    it.pop(k, None)
                    notes.append(f"items[{i}].{k}")
            if it.get("label") != "text" and "text" in it:
                it.pop("text", None)
                notes.append(f"items[{i}].text")
            b = it.get("bbox")
            if not isinstance(b, list) or len(b) != 4:
                continue
            nums = [_as_num(v) for v in b]
            if any(v is None for v in nums):
                continue
            x0, y0, x1, y1 = nums
            x0, x1 = min(x0, x1) * scale_x, max(x0, x1) * scale_x
            y0, y1 = min(y0, y1) * scale_y, max(y0, y1) * scale_y
            ix0 = max(0, min(width - 1, int(round(x0))))
            ix1 = max(0, min(width, int(round(x1))))
            iy0 = max(0, min(height - 1, int(round(y0))))
            iy1 = max(0, min(height, int(round(y1))))
            if ix1 <= ix0:
                ix1 = min(width, ix0 + 8)
            if iy1 <= iy0:
                iy1 = min(height, iy0 + 8)
            nb = [ix0, iy0, ix1, iy1]
            if nb != b:
                notes.append(f"items[{i}].bbox")
            it["bbox"] = nb

    if notes:
        print("[MOSS Studio] AI 拆解已本地修复: " + ", ".join(notes[:8])
              + (" …" if len(notes) > 8 else ""), flush=True)
    return obj
