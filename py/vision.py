"""图像反推核心：ComfyUI IMAGE → 视觉模型 → 官方 layout 草稿。

官方 moss-image-prompt-enhancer skill 只做“文字想法 → 提示词”，没有图片输入；
这里用同一套 layout 契约（width/height + abs/xyxy + summary/items）约束视觉模型，
再做本地归一化/修复与严格校验，失败带错误反馈重试。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json

try:
    from .compile_prompt import validate_draft
    from .translate import (
        MAX_RETRY,
        PermanentError,
        TranslationError,
        _forgive_llm_sloppiness,
        _repair_ai_draft,
        append_agent_md,
        chat,
        extract_json,
        sanitize_secret,
    )
except ImportError:  # 兼容非包方式导入
    from compile_prompt import validate_draft
    from translate import (
        MAX_RETRY,
        PermanentError,
        TranslationError,
        _forgive_llm_sloppiness,
        _repair_ai_draft,
        append_agent_md,
        chat,
        extract_json,
        sanitize_secret,
    )


# image_detail 挡位 → provider 实际发送的 detail 值（探测与真实反推共用）。
# MiMo 的 image_url schema 没有 detail 字段 → None（省略）。
# DeepSeek 只接受 low/high；MiniMax 接受 low/default/high；
# custom（OpenAI 兼容/Ollama/网关）不用 "default"（非标准值），中档回退 "auto"。
IMAGE_DETAIL_TIERS = {
    "low": {"deepseek": "low", "minimax": "low", "custom": "low", "quality": 70},
    "medium_low": {"deepseek": "high", "minimax": "default", "custom": "auto", "quality": 78},
    "medium": {"deepseek": "high", "minimax": "default", "custom": "auto", "quality": 84},
    "medium_high": {"deepseek": "high", "minimax": "high", "custom": "high", "quality": 88},
    "high": {"deepseek": "high", "minimax": "high", "custom": "high", "quality": 92},
    "ultra": {"deepseek": "high", "minimax": "high", "custom": "high", "quality": 96},
}
IMAGE_DETAIL_TIERS_LIST = list(IMAGE_DETAIL_TIERS)


def image_detail_for(provider: str, tier: str):
    """返回该 provider 应发送的 image_url.detail；None 表示省略该字段。"""
    provider = (provider or "custom").strip().lower()
    row = IMAGE_DETAIL_TIERS.get(tier) or IMAGE_DETAIL_TIERS["high"]
    if provider == "mimo":
        return None
    return row.get(provider, row["custom"])


DEFAULT_VISION_PROMPT = """You analyze ONE reference image and reverse-engineer a MOSS-Image layout draft.
Return ONE strict JSON object, raw JSON only, no code fences, no explanations.

Coordinate system:
- Use a normalized 0-1000 grid for bboxes: [x_min, y_min, x_max, y_max], top-left origin.
- Set "width": 1000 and "height": 1000 in the draft; the host rescales to the real canvas.
- x_min < x_max and y_min < y_max, all values within 0..1000.

Schema (exactly these keys):
{
  "width": 1000,
  "height": 1000,
  "bbox_coord_space": "abs",
  "bbox_format": "xyxy",
  "summary": {
    "caption": "<one-paragraph Chinese description of the whole image: subject, composition, lighting, color mood, style>",
    "meta": ["<1-3 short English medium/genre tags>"],
    "canvas": "single",
    "palette": ["#RRGGBB", "..."]
  },
  "items": [
    {
      "label": "object" | "character" | "text",
      "caption": "<Chinese description of that element's visible appearance>",
      "bbox": [x_min, y_min, x_max, y_max],
      "text": "<ONLY for label=text: exact visible characters>",
      "palette": ["#RRGGBB"],
      "tags": ["..."]
    }
  ]
}

Rules:
- items: 1 to {MAX_ITEMS} distinct visible elements. Prefer the main subject(s), key props, and visible text; do not fragment the image into tiny parts.
- label "character" for people or creatures, "object" for props/scenery, "text" for visible lettering.
- For every label="text" item, "text" is mandatory and must reproduce the visible characters EXACTLY in their original language, including punctuation and line breaks (use \\n). Never translate, paraphrase, romanize, or invent text.
- A text item's caption describes typography (font, weight, color, placement), not the words.
- Ignore watermarks, stock-photo credits, and incidental logos: do not describe them, and do not create text items for them.
- Item captions and summary.caption are Chinese. summary.meta tags are English.
- palette: 3-5 dominant hex colors from the image; omit if not meaningful.
- Do not add unknown keys: no id, type, description, scene_type, elements, panel, subject, obj, watermark, confidence.
- If the image has no distinct element (e.g. a plain texture), return one "object" item covering the main subject with bbox [0, 0, 1000, 1000].
- No markdown, no comments, no trailing commas.
"""


LABEL_ALIASES = {
    "obj": "object",
    "object": "object",
    "subject": "object",
    "thing": "object",
    "item": "object",
    "prop": "object",
    "person": "character",
    "people": "character",
    "human": "character",
    "figure": "character",
    "man": "character",
    "woman": "character",
    "child": "character",
    "character": "character",
    "text": "text",
    "title": "text",
    "caption": "text",
    "typography": "text",
    "lettering": "text",
    "logo": "text",
}


def encode_image(image, max_side: int = 1536, quality: int = 90):
    """ComfyUI IMAGE[H,W,C] float → (base64, mime, bytes, sent_w, sent_h)。"""
    import numpy as np
    from PIL import Image

    arr = (image.detach().float().cpu().numpy() * 255.0).clip(0, 255).astype("uint8")
    if arr.ndim == 3 and arr.shape[-1] == 4:
        arr = arr[..., :3]
    img = Image.fromarray(arr)
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    raw = buf.getvalue()
    return (
        base64.b64encode(raw).decode("ascii"),
        "image/jpeg",
        len(raw),
        img.size[0],
        img.size[1],
    )


def image_signature(image_b64: str, params: dict) -> str:
    """图片内容 + 参数 → 去重签名（同一结果重复 Queue 时不覆盖手改）。"""
    h = hashlib.md5()
    h.update(image_b64.encode("ascii"))
    h.update(json.dumps(params, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()


# caption_detail 挡位定义（详细定义只放在代码里，节点只显示挡位名）：
#   off          极简：summary.caption ≤10 汉字，元素 caption ≤8 汉字；只写主体/动作，不写风格/光线/材质/氛围。
#   low          简短：summary.caption ≤30 汉字一句话；元素 caption ≤20 汉字；只写可见外观。
#   medium_low   中低：summary.caption 一句（≤50 汉字），带关键构图/颜色；元素 caption 一句。
#   medium       中：summary.caption 一段（2-3 句）；元素 caption 1-2 句，含外观与位置。
#   medium_high  中高：summary.caption 一段（3-4 句），含构图/光线/材质；元素 caption 2 句。
#   high         高：summary.caption 详细一段（4-6 句），含构图/光线/材质/风格；元素 caption 2-3 句。
#   ultra        极致：summary.caption 多句详细段，含构图/光线/材质/镜头/色彩/氛围/细节；元素 caption 3-4 句。
CAPTION_DETAIL_INSTRUCTIONS = {
    "off": "summary.caption: at most 10 Chinese characters, only the main subject/action; "
           "each item caption: at most 8 Chinese characters; no style, lighting, material or mood.",
    "low": "summary.caption: one short sentence (<=30 Chinese characters); "
           "each item caption: <=20 Chinese characters; only visible appearance.",
    "medium_low": "summary.caption: one sentence (<=50 Chinese characters) with key composition/colors; "
                  "each item caption: one sentence.",
    "medium": "summary.caption: one paragraph (2-3 sentences); "
              "each item caption: 1-2 concrete visual sentences.",
    "medium_high": "summary.caption: one paragraph (3-4 sentences) with composition, lighting and material; "
                   "each item caption: 2 sentences.",
    "high": "summary.caption: a detailed paragraph (4-6 sentences) with composition, lighting, material and style; "
            "each item caption: 2-3 sentences.",
    "ultra": "summary.caption: a very detailed multi-sentence paragraph with composition, lighting, materials, "
             "camera, color, mood and fine details; each item caption: 3-4 sentences.",
}

# tag_detail 挡位定义：
#   none   不输出 summary.meta / summary.palette / item.tags / item.palette（本地也会剥掉）。
#   basic  只保留 summary.meta 1-2 个短标签；不输出 palette/item.tags（本地也会剥掉）。
#   full   summary.meta 1-3 个短标签 + summary.palette 3-5 个 hex；元素 tags/palette 可选。
_TAG_DETAIL_INSTRUCTIONS = {
    "none": "Do not output summary.meta, summary.palette, item tags or item palette.",
    "basic": "summary.meta: 1-2 short English tags. Do not output palette or item tags.",
    "full": "summary.meta: 1-3 short English tags. summary.palette: 3-5 hex colors. "
            "Item tags/palette are optional.",
}


def build_style_instructions(max_items: int, include_text: bool,
                             caption_detail: str, tag_detail: str) -> str:
    """按反推模式追加输出风格约束（max_items / 文字 / 描述长度 / 标签详细度）。"""
    cap = CAPTION_DETAIL_INSTRUCTIONS.get(
        caption_detail, CAPTION_DETAIL_INSTRUCTIONS["medium_low"]
    )
    tags = _TAG_DETAIL_INSTRUCTIONS.get(tag_detail, _TAG_DETAIL_INSTRUCTIONS["full"])
    text_rule = (
        "Create text items only for visible lettering; preserve exact characters."
        if include_text else
        "Do NOT create text items and do not reproduce any lettering, watermarks, logos or credits."
    )
    return (
        "Output style for this request:\n"
        f"- items: at most {max_items}; merge small elements, never exceed this count.\n"
        f"- {text_rule}\n"
        f"- {cap}\n"
        f"- {tags}\n"
        "- Ignore watermarks, stock-photo credits and incidental logos; never create text items for them."
    )


def normalize_vision_draft(obj, width: int, height: int, max_items: int,
                           fallback_caption: str = "",
                           include_text: bool = True, tag_detail: str = "full"):
    """视觉模型输出 → 官方草稿：label 映射、坐标缩放/夹取、text 降级、数量截断、严格校验。"""
    obj = _forgive_llm_sloppiness(obj)
    if not isinstance(obj, dict):
        raise TranslationError("反推输出必须是 JSON 对象")

    items = obj.get("items")
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                lab = str(it.get("label") or "").strip().lower()
                it["label"] = LABEL_ALIASES.get(lab, lab)

    # 模型按 0-1000 归一化输出但漏了宽高时补 1000；_repair_ai_draft 会按目标画布缩放
    for key in ("width", "height"):
        v = obj.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            obj[key] = 1000

    out = _repair_ai_draft(obj, width, height, fallback_caption=fallback_caption)
    items = out.get("items")
    if isinstance(items, list):
        if not include_text:
            # 简洁/手动关闭文字时，直接丢弃 text 元素（不让模型输出可见文字）
            out["items"] = [it for it in items
                            if not (isinstance(it, dict) and it.get("label") == "text")]
            items = out["items"]
        if len(items) > max_items:
            out["items"] = items[:max_items]
        for it in out["items"]:
            if not isinstance(it, dict):
                continue
            # text 缺 visible 原文时降级为 object，避免整轮重试
            if it.get("label") == "text" and not str(it.get("text") or "").strip():
                it["label"] = "object"
                it.pop("text", None)
    if not isinstance(out.get("items"), list) or not out["items"]:
        cap = str((out.get("summary") or {}).get("caption") or fallback_caption or "").strip()
        if cap:
            out["items"] = [{"label": "object", "caption": cap,
                             "bbox": [0, 0, width, height]}]

    if tag_detail in ("none", "basic"):
        s = out.get("summary")
        if isinstance(s, dict):
            s.pop("palette", None)
            if tag_detail == "none":
                s.pop("meta", None)
        for it in out.get("items", []):
            if isinstance(it, dict):
                it.pop("tags", None)
                it.pop("palette", None)
    validate_draft(out)
    return out


def reverse_image_to_prompt(image_b64: str, mime: str, *, width: int, height: int,
                            max_items: int, include_text: bool = True,
                            caption_detail: str = "medium_low", tag_detail: str = "full",
                            image_detail=None, base_url: str, api_key: str,
                            body: dict, system_prompt: str = "",
                            extra_instructions: str = "", agent_md: str = "",
                            timeout: int = 180):
    """视觉模型 → 官方 layout 草稿 dict（含本地修复、严格校验、带错重试）。"""
    sys_prompt = (system_prompt or "").strip() or DEFAULT_VISION_PROMPT
    sys_prompt = sys_prompt.replace("{MAX_ITEMS}", str(max_items))
    sys_prompt = append_agent_md(sys_prompt, agent_md)
    sys_prompt += "\n\n" + build_style_instructions(
        max_items, include_text, caption_detail, tag_detail
    )
    user = (f"Target canvas: {width} x {height}\nMax items: {max_items}\n"
            "Reverse-engineer the attached image into the layout draft.")
    if (extra_instructions or "").strip():
        user += "\n\nAdditional per-request instructions:\n" + extra_instructions.strip()

    image_url = {"url": f"data:{mime};base64,{image_b64}"}
    # MiMo 的 image_url schema 没有 detail 字段；DeepSeek 没有 default；传 None 时省略。
    if image_detail in ("low", "default", "high", "auto", "original"):
        image_url["detail"] = image_detail
    image_part = {"type": "image_url", "image_url": image_url}
    text_part = {"type": "text", "text": user}
    base_messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": [text_part, image_part]},
    ]
    last_err = None
    last_raw = ""
    for attempt in range(1 + MAX_RETRY):
        body["messages"] = list(base_messages)
        try:
            raw = chat(base_url, api_key, body, timeout,
                       tag=f"vision attempt={attempt + 1}/{1 + MAX_RETRY}")
            last_raw = raw
            out = normalize_vision_draft(
                extract_json(raw), width, height, max_items,
                include_text=include_text, tag_detail=tag_detail,
            )
            return out
        except PermanentError:
            raise
        except Exception as e:  # HTTP / JSON / 草稿校验 统一重试
            safe_err = sanitize_secret(str(e), api_key, limit=300)
            print(f"[MOSS Studio] 图像反推 attempt={attempt + 1} 失败: {safe_err} "
                  f"| 输出开头: {last_raw[:160]!r}", flush=True)
            last_err = e
            if attempt < MAX_RETRY:
                base_messages = base_messages + [
                    {"role": "assistant", "content": last_raw[:2000]},
                    {"role": "user",
                     "content": f"上一次输出被拒绝：{safe_err}\n请修复以上问题，仅输出修正后的完整 layout 草稿 JSON。"},
                ]
    raise TranslationError(
        f"图像反推失败（已重试 {MAX_RETRY} 次）: {sanitize_secret(str(last_err), api_key)}"
    )
