"""节点 4：MOSS Image → Prompt —— 上传图经视觉 LLM 反推三种提示词。

输出三路（内容只有第 0 路不同，1/2 路是同一份中文 layout 草稿）：
- prose_zh : 中文散文（layout.summary.caption），接 Editor.reverse_in 写入 source
- draft_zh : 中文 layout 草稿 JSON，接 Editor.reverse_in 写入 source 并关 ai_draft
- canvas_zh: 同一份 layout JSON，接 Editor.reverse_in 写入 edits 直接出画布

缓存：不返回 NaN，图片变化由上游 LoadImage 的签名触发；IS_CHANGED 只哈希本节点
参数 + AGENT.md/config.json 内容，因此同一张图重复 Queue 不会重复调用视觉模型。
"""
from __future__ import annotations

import json

try:
    from ..py.dims import snap16
    from ..py.translate import (
        AGENT_DEFAULT,
        agent_md_widget_options,
        build_body,
        load_agent_md,
        load_config,
        resolve_api_key,
        resolve_endpoint,
    )
    from ..py.vision import (
        DEFAULT_VISION_PROMPT,
        IMAGE_DETAIL_TIERS,
        IMAGE_DETAIL_TIERS_LIST,
        encode_image,
        image_detail_for,
        image_signature,
        reverse_image_to_prompt,
    )
except ImportError:  # 兼容非包方式导入
    from py.dims import snap16
    from py.translate import (
        AGENT_DEFAULT,
        agent_md_widget_options,
        build_body,
        load_agent_md,
        load_config,
        resolve_api_key,
        resolve_endpoint,
    )
    from py.vision import (
        DEFAULT_VISION_PROMPT,
        IMAGE_DETAIL_TIERS,
        IMAGE_DETAIL_TIERS_LIST,
        encode_image,
        image_detail_for,
        image_signature,
        reverse_image_to_prompt,
    )


# caption_detail 详细定义见 py/vision.py 的 CAPTION_DETAIL_INSTRUCTIONS。
# 节点只显示挡位名；off 仍会输出极短的非空 caption（官方 schema 要求）。
CAPTION_DETAIL_OPTIONS = ["off", "low", "medium_low", "medium", "medium_high", "high", "ultra"]
TAG_DETAIL_OPTIONS = ["none", "basic", "full"]
_CAPTION_DETAIL_ALIASES = {"short": "low", "long": "medium_high"}  # 旧工作流兼容

# image_detail 挡位定义（provider 感知）：
#   low          DeepSeek low / MiniMax low /       MiMo 省略；JPEG q70；最快，只适合构图
#   medium_low   DeepSeek high / MiniMax default /  MiMo 省略；JPEG q78；约 1MP 布局
#   medium       DeepSeek high / MiniMax default /  MiMo 省略；JPEG q84；通用
#   medium_high  DeepSeek high / MiniMax high /     MiMo 省略；JPEG q88；文字/材质
#   high         DeepSeek high / MiniMax high /     MiMo 省略；JPEG q92；密集文字
#   ultra        DeepSeek high / MiniMax high /     MiMo 省略；JPEG q96；小字/最高成本
IMAGE_DETAIL_OPTIONS = IMAGE_DETAIL_TIERS_LIST
_REVERSE_MODES = ("concise", "standard", "detailed", "manual")

# image_max_side 挡位：对齐 MOSS-Image 0.25M–4M（16 倍数），取最长边。
# 512=0.25M / 720≈0.5M / 1024=1M / 1232≈1.5M / 1424=2M / 1536≈2.36M / 1728≈3M / 2000=4M。
# 注意 max_side 是最长边；实际像素面积 = W×H，会随长宽比变化。
IMAGE_MAX_SIDE_OPTIONS = ["512", "720", "1024", "1232", "1424", "1536", "1728", "2000"]
IMAGE_MAX_SIDE_VALUES = [int(x) for x in IMAGE_MAX_SIDE_OPTIONS]

# 各家 max_tokens 上限（官方文档）：
#   DeepSeek deepseek-flash: 393216（384K）；非思考默认 8K，思考默认 64K
#   MiMo mimo-v2.6-flash:  131072（128K，max_completion_tokens）
#   MiniMax M3.1-Flash:    524288（512K，max_completion_tokens；官方推荐 131072）
#   custom: 默认按 131072 保守上限
MAX_TOKENS_BY_PROVIDER = {
    "deepseek": 393216,
    "mimo": 131072,
    "minimax": 524288,
    "custom": 131072,
}

# temperature 只在 DeepSeek/MiMo 关闭思考时生效；开启思考时被服务端忽略。
# MiniMax 永远固定 1.0/0.95，传入无效，这里统一写 1.0 以保持语义清晰。
TEMPERATURE_PRESETS = {
    "concise": {"deepseek": 0.1, "mimo": 0.1, "minimax": 1.0, "custom": 0.2},
    "standard": {"deepseek": 0.2, "mimo": 0.2, "minimax": 1.0, "custom": 0.3},
    "detailed": {"deepseek": 0.3, "mimo": 0.3, "minimax": 1.0, "custom": 0.3},
}

# 非 manual 模式统一把思考压到最低：DeepSeek/MiMo 可直接关闭，MiniMax 关不掉、
# off 会映射到最低档 low。因此这些模式的 temperature 才真正生效（MiniMax 除外）。
MODE_PRESETS = {
    "concise": {
        "max_items": 4, "include_text": False, "caption_detail": "low",
        "tag_detail": "none", "max_tokens": 8192, "image_detail": "medium_low",
        "image_max_side": "1024", "thinking": "off", "temperature": 0.1,
    },
    "standard": {
        "max_items": 6, "include_text": True, "caption_detail": "medium_low",
        "tag_detail": "full", "max_tokens": 65536, "image_detail": "high",
        "image_max_side": "1536", "thinking": "off", "temperature": 0.2,
    },
    "detailed": {
        "max_items": 10, "include_text": True, "caption_detail": "medium_high",
        "tag_detail": "full", "max_tokens": 65536, "image_detail": "high",
        "image_max_side": "1728", "thinking": "off", "temperature": 0.3,
    },
}


def _clamp_int(v, lo, hi, default):
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def resolve_reverse_options(mode, *, provider="mimo", max_items=6,
                            include_text=True, caption_detail="medium_low",
                            tag_detail="full", max_tokens=0,
                            image_detail="high", image_max_side="1536",
                            thinking="off", temperature=0.3):
    """反推模式 → 生效参数；manual 才读细节 widget，其余用固定预设。"""
    provider = (provider or "mimo").strip().lower()
    mode = mode if mode in _REVERSE_MODES else "standard"
    if mode == "manual":
        cap = _CAPTION_DETAIL_ALIASES.get(str(caption_detail), str(caption_detail))
        cap = cap if cap in CAPTION_DETAIL_OPTIONS else "medium_low"
        tag = tag_detail if tag_detail in TAG_DETAIL_OPTIONS else "full"
        tier = image_detail if image_detail in IMAGE_DETAIL_OPTIONS else "high"
        try:
            side = int(image_max_side)
        except (TypeError, ValueError):
            side = 1536
        side = max(512, min(2048, side))
        try:
            temp = float(temperature)
        except (TypeError, ValueError):
            temp = 0.3
        think = thinking if thinking in ("off", "low", "medium", "high") else "off"
        out = {
            "max_items": _clamp_int(max_items, 1, 30, 6),
            "include_text": bool(include_text),
            "caption_detail": cap,
            "tag_detail": tag,
            "max_tokens": _clamp_int(max_tokens, 0, 524288, 0),
            "image_detail": tier,
            "image_max_side": side,
            "thinking": think,
            "temperature": max(0.0, min(2.0, temp)),
        }
    else:
        p = dict(MODE_PRESETS.get(mode, MODE_PRESETS["standard"]))
        out = {
            **p,
            "max_items": _clamp_int(p["max_items"], 1, 30, 6),
            "image_max_side": int(p["image_max_side"]),
            "temperature": TEMPERATURE_PRESETS.get(mode, {}).get(
                provider, p.get("temperature", 0.3)
            ),
        }

    limit = MAX_TOKENS_BY_PROVIDER.get(provider, 131072)
    if out["max_tokens"] > 0:
        out["max_tokens"] = min(out["max_tokens"], limit)
    # 本地 Ollama 常见 32K 上下文：非 manual 预设的 65536 会直接超限。
    # custom 的默认输出预算压到 16384（正文实际远小于此）；manual 尊重用户显式值。
    if provider == "custom" and mode != "manual" and out["max_tokens"] > 16384:
        out["max_tokens"] = 16384
    out["image_detail_api"] = image_detail_for(provider, out["image_detail"])
    out["image_quality"] = IMAGE_DETAIL_TIERS[out["image_detail"]]["quality"]
    out["mode"] = mode
    return out


class MOSSImageReverse:
    CATEGORY = "MOSS Prompt Studio"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("prose_zh", "draft_zh", "canvas_zh")
    OUTPUT_TOOLTIPS = (
        "输出①：中文散文（整段图像描述）。通常接入 MOSS 提示词编辑器（Editor）节点的 reverse_in 输入 → 写入 source；之后手动点 ⚡拆解（或开 ai_draft 后 Queue）才会拆解成画布",
        "输出②：中文 layout 草稿 JSON（带 bbox）。通常接入 MOSS 提示词编辑器（Editor）节点的 reverse_in 输入 → 写入 source 并自动关 ai_draft，直接进翻译/编译；本次执行后写回前端，需再 Queue 一次完整链路",
        "输出③：同一份 layout JSON（画布形态）。通常接入 MOSS 提示词编辑器（Editor）节点的 reverse_in 输入 → 直接写回 edits，画布立即出现可编辑框（本节点执行完即写回，无需再 Queue）",
    )
    # 可作为输出根（⚡反推 按钮只提交本节点），但 enabled 默认关闭：
    # 未勾选启用时，即使节点完全未连线、被完整 Queue 执行，也不会调用视觉 LLM。
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, image=None, provider="", base_url="", model="", api_key="",
                   temperature=0.3, thinking="off", timeout=180, canvas_width=0,
                   canvas_height=0, max_items=6, system_prompt="", extra_body="",
                   agent_md=AGENT_DEFAULT, extra_instructions=None,
                   reverse_mode="standard", include_text=True,
                   caption_detail="medium_low", tag_detail="full", max_tokens=0,
                   image_detail="high", image_max_side="1536", enabled=False,
                   **kwargs):
        """连线传入的 image 在这里是 None；图片变化交给上游节点签名，不自己 hash tensor。"""
        import hashlib

        try:
            agent_text = load_agent_md(agent_md)
        except Exception as e:  # 文件标记错误也要让缓存失效，执行时再报明确错误
            agent_text = f"ERR:{e}"
        try:
            cfg_text = json.dumps(load_config(), sort_keys=True, ensure_ascii=False)
        except Exception as e:
            cfg_text = f"ERR:{e}"
        payload = json.dumps({
            "provider": provider, "base_url": base_url, "model": model,
            "api_key": api_key, "temperature": temperature, "thinking": thinking,
            "timeout": timeout, "canvas_width": canvas_width,
            "canvas_height": canvas_height, "max_items": max_items,
            "reverse_mode": reverse_mode, "include_text": include_text,
            "caption_detail": caption_detail, "tag_detail": tag_detail,
            "max_tokens": max_tokens, "image_detail": image_detail,
            "image_max_side": image_max_side, "enabled": bool(enabled),
            "system_prompt": system_prompt, "extra_body": extra_body,
            "agent_md": agent_md, "extra_instructions": extra_instructions,
            "agent_text": agent_text, "config": cfg_text,
        }, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    @classmethod
    def VALIDATE_INPUTS(cls, agent_md=None):
        """绕过 agent_md 的动态 COMBO 校验（ComfyUI 会跳过本函数声明的输入）。

        agents/*.md 被删除/改名后，旧值不在下拉列表里，ComfyUI 默认会在进入 run
        之前报 "Value not in list"，节点内的友好报错不可达。真正存在性仍由
        run() 里的 load_agent_md 校验并给出中文提示。
        """
        return True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE", {
                    "tooltip": "接 ComfyUI 原生上传图像节点（LoadImage 等）；batch 只反推第一张",
                }),
                "provider": (["mimo", "deepseek", "minimax", "custom"], {
                    "default": "mimo",
                    "tooltip": "custom 可填任意 OpenAI 兼容 URL（含 Ollama / LM Studio），配合 base_url/model/api_key 使用",
                }),
                "base_url": ("STRING", {
                    "default": "",
                    "tooltip": "OpenAI 兼容地址；留空读 config.json。Ollama 填 http://127.0.0.1:11434/v1，LM Studio 填 http://127.0.0.1:1234/v1",
                }),
                "model": ("STRING", {
                    "default": "",
                    "tooltip": "留空读 config.json / 预设；Ollama 填准确 tag（如 qwen3-vl:4b）",
                }),
                "api_key": ("STRING", {
                    "default": "EMPTY",
                    "tooltip": "留空/EMPTY 读 config.json；本地 Ollama/LM Studio 保持 EMPTY",
                }),
                "temperature": ("FLOAT", {
                    "default": 0.3, "min": 0.0, "max": 2.0, "step": 0.05,
                    "socketless": True,
                    "tooltip": "仅 manual 可调；非 manual 按 provider 自动：DeepSeek/MiMo 关思考时 0.1–0.3（思考开启时 provider 忽略 temperature），MiniMax 固定 1（始终忽略）",
                }),
                "thinking": (["off", "low", "medium", "high"], {
                    "default": "off",
                    "socketless": True,
                    "tooltip": "仅 manual 可调；非 manual 固定 off（DeepSeek/MiMo 关闭思考；MiniMax 关不掉，off 映射最低档 low；custom 若默认思考可用 extra_body）",
                }),
                "timeout": ("INT", {
                    "default": 180, "min": 10, "max": 1200,
                    "tooltip": "视觉调用超时；本地 35B 模型首次加载可能较慢，可调大到 300~600",
                }),
                "canvas_width": ("INT", {
                    "default": 0, "min": 0, "max": 15360, "step": 16,
                    "tooltip": "目标画布宽；0=按输入图片实际宽度对齐 16",
                }),
                "canvas_height": ("INT", {
                    "default": 0, "min": 0, "max": 15360, "step": 16,
                    "tooltip": "目标画布高；0=按输入图片实际高度对齐 16",
                }),
                "max_items": ("INT", {
                    "default": 6, "min": 1, "max": 30, "step": 1,
                    "socketless": True,
                    "tooltip": "最多元素数（画框）；仅 manual 可调（concise 4 / standard 6 / detailed 10；manual 1–30）",
                }),
                "system_prompt": ("STRING", {
                    "default": DEFAULT_VISION_PROMPT,
                    "multiline": True,
                    "tooltip": "注入口①：反推契约 prompt，可整体改写；清空恢复内建默认",
                }),
                "extra_body": ("STRING", {
                    "default": "", "multiline": True,
                    "tooltip": "注入口③：原始 JSON 合并进请求体，最后合并（stream 会被强制 false），如 {\"reasoning_effort\":\"none\"}",
                }),
            },
            "optional": {
                "agent_md": (agent_md_widget_options(), {
                    "default": AGENT_DEFAULT,
                    "tooltip": "注入口⓪（追加注入）：默认读插件根目录 AGENT.md；可选 agents/ 下的 .md；(不注入) 关闭",
                }),
                # 以下 8 个模式参数（reverse_mode + 7 个细节）放在 optional 且位于 agent_md 之后：
                # 旧工作流 widgets_values 的前 13 个位置（provider…agent_md）保持不变，
                # 升级后不会把旧 max_items=6 读到 reverse_mode 等新控件里。
                "reverse_mode": (["concise", "standard", "detailed", "manual", ""], {
                    "default": "standard",
                    "tooltip": "反推模式：concise=简单图（4项/无文字/low/1024）；standard=首次跑图设置（6项/允许文字/medium_low/65536/1536）；detailed=详细（10项/medium_high/1728）；manual=显示并手动调下面所有输出参数（非 manual 时下方细节控件不生效，切换模式不会改写 manual 值）。末尾空值仅为兼容旧工作流（旧 DOM 占位），执行时按 standard",
                }),
                "include_text": ("BOOLEAN", {
                    "default": True, "socketless": True,
                    "tooltip": "是否输出画面文字（text 元素）；仅 manual 可调，concise 模式强制关闭；水印/图库署名始终忽略",
                }),
                "caption_detail": (CAPTION_DETAIL_OPTIONS, {
                    "default": "medium_low", "socketless": True,
                    "tooltip": "caption 描述长度挡位；仅 manual 可调：off/低/中低/中/中高/高/极致（详细定义见代码注释）",
                }),
                "tag_detail": (TAG_DETAIL_OPTIONS, {
                    "default": "full", "socketless": True,
                    "tooltip": "meta/palette/tags 详细度；仅 manual 可调：none（全剥）/ basic（只留 meta）/ full",
                }),
                "max_tokens": ("INT", {
                    "default": 0, "min": 0, "max": 524288, "step": 1024,
                    "socketless": True,
                    "tooltip": "输出 token 上限；0=插件默认（按 provider 由 build_body 决定，不是服务商默认）；manual 可调；上限 DeepSeek 393216 / MiMo 131072 / MiniMax 524288，超出按 provider clamp。custom 非 manual 统一压到 16384，避免本地 Ollama 32K 上下文超限",
                }),
                "image_detail": (IMAGE_DETAIL_OPTIONS, {
                    "default": "high", "socketless": True,
                    "tooltip": "视觉质量挡位（detail+JPEG 质量，provider 自动映射）：low/中低/中/中高/高/极致；MiMo 不发送 detail，custom 中档用 auto；详细定义见代码注释；仅 manual 可调",
                }),
                "image_max_side": (IMAGE_MAX_SIDE_OPTIONS, {
                    "default": "1536", "socketless": True,
                    "tooltip": "发送给视觉模型的最长边（MOSS 像素档 0.25M/0.5M/1M/1.5M/2M/2.36M/3M/4M）：512/720/1024/1232/1424/1536/1728/2000；仅 manual 可调",
                }),
                # 放在所有旧控件之后：旧工作流没有该值 → 默认 false，完整 Queue 不调用 LLM。
                # 需要反推时勾选（完整链路）或直接点节点上的 ⚡反推（单次执行，不改勾选）。
                "enabled": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "启用反推：默认关闭；关闭时节点即使被完整 Queue 执行也不调用视觉模型。接 Editor 后勾选并 Queue，或在节点上点 ⚡反推 单独执行一次（本次强制启用，不改这里的勾选）",
                }),
                "extra_instructions": ("STRING", {
                    "forceInput": True,
                    "tooltip": "注入口②：连线注入的逐次附加指令，追加到对话末尾",
                }),
            },
        }

    def run(self, image, provider, base_url, model, api_key, temperature=0.3,
            thinking="off", timeout=180, canvas_width=0, canvas_height=0,
            max_items=6, system_prompt="", extra_body="",
            agent_md=AGENT_DEFAULT, extra_instructions=None,
            reverse_mode="standard", include_text=True, caption_detail="medium_low",
            tag_detail="full", max_tokens=0, image_detail="high",
            image_max_side="1536", enabled=False):
        # 与 INPUT_TYPES 默认一致：未显式启用（含无头 API 漏传）时不解析图片、不发请求。
        # ⚡反推按钮会在单次 prompt 里显式传 enabled=true。
        if not enabled:
            return ("", "", "")
        opts = resolve_reverse_options(
            reverse_mode, provider=provider, max_items=max_items,
            include_text=include_text, caption_detail=caption_detail,
            tag_detail=tag_detail, max_tokens=max_tokens,
            image_detail=image_detail, image_max_side=image_max_side,
            thinking=thinking, temperature=temperature,
        )
        if image is None:
            raise ValueError("图像反推节点：image 输入为空，请接 LoadImage 等图像输出")
        first = image[0]
        shape = getattr(first, "shape", None)
        if shape is None or len(shape) < 2:
            raise ValueError("图像反推节点：无法读取输入图像尺寸")
        src_h, src_w = int(shape[0]), int(shape[1])
        tw = snap16(canvas_width) if int(canvas_width or 0) > 0 else snap16(src_w)
        th = snap16(canvas_height) if int(canvas_height or 0) > 0 else snap16(src_h)

        b64, mime, nbytes, _, _ = encode_image(
            first, max_side=opts["image_max_side"], quality=opts["image_quality"]
        )

        cfg = load_config()
        thinking_eff = opts["thinking"]
        url, mdl = resolve_endpoint(provider, base_url, model, thinking_eff, cfg)
        # 只在目标端点是该 provider 的 config/默认地址时回退配置 key，防止 key 外泄
        key, _auth = resolve_api_key(provider, base_url or url, api_key, cfg)
        body = build_body(mdl, opts["temperature"], thinking_eff, provider, extra_body)
        if opts["max_tokens"] > 0:
            if provider == "minimax":
                body["max_completion_tokens"] = opts["max_tokens"]
            else:
                body["max_tokens"] = opts["max_tokens"]
                if "max_completion_tokens" in body:
                    body["max_completion_tokens"] = opts["max_tokens"]
        agent_text = load_agent_md(agent_md)

        import time
        t0 = time.perf_counter()
        draft = reverse_image_to_prompt(
            b64, mime, width=tw, height=th, max_items=opts["max_items"],
            include_text=opts["include_text"], caption_detail=opts["caption_detail"],
            tag_detail=opts["tag_detail"], image_detail=opts["image_detail_api"],
            base_url=url, api_key=key, body=body, system_prompt=system_prompt,
            extra_instructions=extra_instructions or "", agent_md=agent_md,
            timeout=int(timeout),
        )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        prose = draft["summary"]["caption"]
        text = json.dumps(draft, ensure_ascii=False, indent=2)
        mode = opts.get("mode", reverse_mode)
        sig = image_signature(b64, {
            "provider": provider, "base_url": url, "model": mdl,
            "thinking": opts["thinking"], "reverse_mode": mode, "options": opts,
            "system_prompt": (system_prompt or "").strip() or DEFAULT_VISION_PROMPT,
            "agent_text": agent_text, "extra_body": extra_body,
            "extra_instructions": extra_instructions or "",
            "width": tw, "height": th,
        })
        items = draft["items"]
        payload = {
            "sig": sig,
            "mode": mode,
            "max_items": opts["max_items"],
            "caption_detail": opts["caption_detail"],
            "tag_detail": opts["tag_detail"],
            "max_tokens": opts["max_tokens"],
            "thinking": opts["thinking"],
            "temperature": opts["temperature"],
            "image_detail": opts["image_detail"],
            "image_max_side": opts["image_max_side"],
            "image_quality": opts["image_quality"],
            "prose": prose,
            "draft": text,
            "width": tw,
            "height": th,
            "items": len(items),
            "texts": sum(1 for it in items if it.get("label") == "text"),
            "characters": sum(1 for it in items if it.get("label") == "character"),
            "elapsed_ms": elapsed_ms,
            "image_bytes": nbytes,
            "batch": len(image),
        }
        ui = {"mps_reverse": json.dumps(payload, ensure_ascii=False)}
        return {"ui": ui, "result": (prose, text, text)}
