"""节点 1：MOSS Prompt Editor —— 粘贴源归一 + 画布编辑结果出口 + 可选 AI 拆解。

AI 拆解：勾选 ai_draft 且输入是散文/caption 时，调用内网 LLM（与翻译节点同一套
OpenAI 兼容栈）把散文拆成官方 layout 草稿（summary + items 带 bbox），经完整校验后
通过 ui.mps_draft 写回前端 edits widget → 画布出现可编辑的框。
已经是 layout/已编译形态时 ai_draft 不生效（直接透传）。
"""
from __future__ import annotations

import json

try:
    from ..py.sniff import sniff
    from ..py.translate import (
        AGENT_DEFAULT,
        DEFAULT_AI_DRAFT_PROMPT,
        agent_md_widget_options,
        load_config,
        prose_to_layout,
        resolve_endpoint,
    )
except ImportError:  # 兼容非包方式导入
    from py.sniff import sniff
    from py.translate import (
        AGENT_DEFAULT,
        DEFAULT_AI_DRAFT_PROMPT,
        agent_md_widget_options,
        load_config,
        prose_to_layout,
        resolve_endpoint,
    )


def _snap16(v, lo=64, hi=15360):
    v = int(v)
    return max(lo, min(hi, round(v / 16) * 16))


class MOSSEditor:
    CATEGORY = "MOSS Prompt Studio"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "INT", "INT")
    RETURN_NAMES = ("draft", "width", "height")
    OUTPUT_TOOLTIPS = (
        "编辑后的草稿 JSON",
        "当前画布宽：layout 取草稿 width；caption 取 canvas_width（16 倍数）",
        "当前画布高：layout 取草稿 height；caption 取 canvas_height（16 倍数）",
    )
    # 允许作为输出根节点：画布工具栏的"⚡拆解"按钮只提交本节点也能执行
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, source, canvas_width, canvas_height, edits, ai_draft=False, **kwargs):
        import hashlib

        text = (edits or "").strip() or (source or "")
        if ai_draft and '"items"' not in text:
            # 散文+拆解模式：每次都真执行（按钮可反复触发，不受缓存影响）
            return float("nan")
        # 其余情况：内容哈希，保持与默认缓存行为一致
        return hashlib.md5(f"{text}|{canvas_width}|{canvas_height}".encode("utf-8")).hexdigest()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "source": ("STRING", {
                    "default": "", "multiline": True,
                    "tooltip": "粘贴上游产出的中文提示词：layout 草稿/已编译 JSON 自动识别；纯文本散文在 ai_draft 开启时自动拆解为布局，否则按 caption 处理",
                }),
                "canvas_width": ("INT", {
                    "default": 1024, "min": 64, "max": 15360, "step": 16,
                    "tooltip": "画布尺寸：AI 拆解的目标构图尺寸；粘贴已编译 JSON 时兼作反解尺寸；caption 形态下同时作为本节点 width 输出（接 Compiler 的 canvas_width）",
                }),
                "canvas_height": ("INT", {
                    "default": 1024, "min": 64, "max": 15360, "step": 16,
                    "tooltip": "画布尺寸：AI 拆解的目标构图尺寸；粘贴已编译 JSON 时兼作反解尺寸；caption 形态下同时作为本节点 height 输出（接 Compiler 的 canvas_height）",
                }),
                "edits": ("STRING", {
                    "default": "", "multiline": True,
                    "tooltip": "画布编辑结果（自动写回，也可手改）；非空时优先生效于 source；AI 拆解结果也会写回这里",
                }),
                "ai_draft": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "AI 拆解：输入是散文/caption 时调用 LLM 拆成带 bbox 的 layout 草稿（已是 layout 则忽略）。拆解结果 Queue 后自动写回 edits 并显示画布",
                }),
                "provider": (["mimo", "deepseek", "custom"], {"default": "mimo"}),
                "base_url": ("STRING", {"default": "", "tooltip": "OpenAI 兼容地址；留空读 config.json；deepseek 预设线上 API"}),
                "model": ("STRING", {"default": "", "tooltip": "留空读 config.json / 预设"}),
                "api_key": ("STRING", {"default": "EMPTY"}),
                "temperature": ("FLOAT", {"default": 0.3, "min": 0.0, "max": 2.0, "step": 0.05}),
                "thinking": (["off", "low", "medium", "high"], {
                    "default": "off",
                    "tooltip": "拆解比翻译更需要推理，效果不好可开 low/medium；MiMo 只有开/关（low/medium/high 同效），DeepSeek off 会显式关闭思考",
                }),
                "timeout": ("INT", {"default": 180, "min": 10, "max": 1200}),
                "ai_prompt": ("STRING", {
                    "default": DEFAULT_AI_DRAFT_PROMPT,
                    "multiline": True,
                    "tooltip": "拆解契约 prompt（可整体改写）；清空恢复内建默认",
                }),
            },
            "optional": {
                "agent_md": (agent_md_widget_options(), {
                    "default": AGENT_DEFAULT,
                    "tooltip": "注入口⓪（追加注入）：默认读插件根目录 AGENT.md；可选 agents/ 目录下的 .md（下拉选择，新文件刷新页面后出现）；每次执行自动重读，改文件即刻生效；(不注入) 完全关闭",
                }),
                "underlay": ("IMAGE", {"tooltip": "底图（通常回接生成图）用于画布对比"}),
            },
        }

    def run(self, source, canvas_width, canvas_height, edits,
            ai_draft=False, provider="mimo", base_url="", model="", api_key="EMPTY",
            temperature=0.3, thinking="off", timeout=180, ai_prompt="",
            agent_md=AGENT_DEFAULT, underlay=None):
        # edits 非空即优先（画布写回 / AI 拆解结果 / 高级用户手改）；清空则回落到 source
        text = (edits or "").strip() or (source or "")
        info = sniff(text, canvas_width, canvas_height)
        ui = {}

        if ai_draft and info["kind"] == "caption":
            cfg = load_config()
            url, mdl = resolve_endpoint(provider, base_url, model, thinking, cfg)
            key = (api_key or "").strip()
            if key in ("", "EMPTY"):
                key = ((cfg.get(provider) or {}).get("api_key") or "").strip()
            draft = prose_to_layout(
                info["draft"]["caption"],
                width=_snap16(canvas_width), height=_snap16(canvas_height),
                base_url=url, api_key=key, model=mdl,
                temperature=temperature, thinking=thinking, provider=provider,
                timeout=int(timeout), system_prompt=ai_prompt, agent_md=agent_md,
            )
            info = {"kind": "layout", "draft": draft}
            ui["mps_draft"] = json.dumps(draft, ensure_ascii=False, indent=2)

        out = json.dumps(info["draft"], ensure_ascii=False, indent=2)
        # 与草稿同源的宽高：layout 用草稿尺寸；caption 用本节点 canvas_width/height，
        # 接给 Compiler 的 canvas_width/height 输入即可保证生图尺寸与画布一致。
        if info["kind"] == "layout":
            out_w, out_h = int(info["draft"]["width"]), int(info["draft"]["height"])
        else:
            out_w, out_h = _snap16(canvas_width), _snap16(canvas_height)

        if underlay is not None:
            img = _save_underlay(underlay)
            if img is not None:
                ui.setdefault("images", []).append(img)
        if ui:
            return {"ui": ui, "result": (out, out_w, out_h)}
        return (out, out_w, out_h)


def _save_underlay(images):
    """把 underlay 张量存成 temp PNG，供前端 /view 拉取。失败不阻塞节点。"""
    try:
        import os
        import time

        import folder_paths
        import numpy as np
        from PIL import Image

        arr = (255.0 * images[0].detach().float().cpu().numpy()).clip(0, 255).astype("uint8")
        img = Image.fromarray(arr)
        # 长边压到 1800，控制 data 体积
        img.thumbnail((1800, 1800))
        name = f"moss_underlay_{int(time.time() * 1000)}.png"
        img.save(os.path.join(folder_paths.get_temp_directory(), name))
        return {"filename": name, "subfolder": "", "type": "temp"}
    except Exception:
        return None
