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
        resolve_api_key,
        resolve_editor_text,
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
        resolve_api_key,
        resolve_editor_text,
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
    def IS_CHANGED(cls, source, canvas_width, canvas_height, edits, ai_draft=False,
                   underlay=None, reverse_in=None, replace_edits=True,
                   last_applied_sig="", edits_source_sig="", force_split=False,
                   **kwargs):
        import hashlib

        if underlay is not None:
            # 底图内容无法便宜地哈希；有底图时强制重跑，避免换图后仍显示旧预览
            return float("nan")
        # ⚡拆解按钮带 force_split=true：每次点击都要真正执行
        if ai_draft and force_split:
            return float("nan")
        # 缓存键同时纳入 source 和 edits：只哈希 edits 会让“换了 source 但 edits
        # 非空”命中旧缓存，新散文永远不会被解析。
        payload = json.dumps(
            {
                "source": source or "", "edits": edits or "",
                "edits_source_sig": edits_source_sig or "",
                "canvas_width": canvas_width, "canvas_height": canvas_height,
                "ai_draft": bool(ai_draft),
            },
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

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
                    "tooltip": "画布编辑结果（自动写回，也可手改）；仅当它对应的是当前 source 时优先，换 source 后以 source 为准（隐藏的 edits_source_sig 记录归属）；AI 拆解结果也会写回这里",
                }),
                "ai_draft": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "AI 拆解：输入是散文/caption 时调用 LLM 拆成带 bbox 的 layout 草稿（已是 layout 则忽略）。结果 Queue 后自动写回 edits 并显示画布；普通 Queue 按输入哈希缓存，重复 Queue 不重复计费；工具栏 ⚡拆解 每次都会强制重拆",
                }),
                "provider": (["mimo", "deepseek", "minimax", "custom"], {"default": "mimo"}),
                "base_url": ("STRING", {"default": "", "tooltip": "OpenAI 兼容地址；留空读 config.json；custom 可填任意兼容地址（含 Ollama / LM Studio）"}),
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
                "underlay": ("IMAGE", {
                    "tooltip": "底图（通常回接生成图）。注意：同一工作流里直接回接会形成依赖环；请先生成图，再 SaveImage → LoadImage 接 underlay，或另开一次对比运行",
                }),
                "reverse_in": ("STRING", {
                    "forceInput": True,
                    "tooltip": "可选：接 MOSS 图像反推节点的输出；不接时 Editor 照常使用 source/edits。prose 写入 source；draft 写入 source 并关 ai_draft；canvas 写入 edits 直接出画布。注意：反推结果在本次执行结束后才写回前端，本节点及下游在本次 Queue 中仍使用提交时的旧数据，需要再 Queue 一次完整链路",
                }),
                "replace_edits": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "反推换图时是否覆盖已手动修改的画布；关闭则保留手改并在反推节点提示（首次接反推且画布已有内容时也会保留）",
                }),
                "last_applied_sig": ("STRING", {
                    "default": "", "hidden": True, "socketless": True,
                    "tooltip": "内部：上次已应用的反推签名（前端维护，后端忽略）",
                }),
                "edits_source_sig": ("STRING", {
                    "default": "", "hidden": True, "socketless": True,
                    "tooltip": "内部：edits 写入时对应 source 的签名（前端维护；后端据此判断换源后是否忽略旧画布）",
                }),
                "force_split": ("BOOLEAN", {
                    "default": False, "hidden": True, "socketless": True,
                    "tooltip": "内部：⚡拆解按钮提交时强制重新拆解；普通 Queue 不设置，按输入哈希缓存",
                }),
            },
        }

    def run(self, source, canvas_width, canvas_height, edits,
            ai_draft=False, provider="mimo", base_url="", model="", api_key="EMPTY",
            temperature=0.3, thinking="off", timeout=180, ai_prompt="",
            agent_md=AGENT_DEFAULT, underlay=None, reverse_in=None,
            replace_edits=True, last_applied_sig="", edits_source_sig="",
            force_split=False):
        # reverse_in / replace_edits / last_applied_sig 只供前端反推写回使用，后端不参与解析
        # source/edits 优先级：edits 只对写入时的那份 source 生效；换源后以 source 为准。
        text = resolve_editor_text(source, edits, edits_source_sig)
        info = sniff(text, canvas_width, canvas_height)
        ui = {}

        if ai_draft and info["kind"] == "caption":
            cfg = load_config()
            url, mdl = resolve_endpoint(provider, base_url, model, thinking, cfg)
            # 只在目标端点是该 provider 的 config/默认地址时回退配置 key
            key, _auth = resolve_api_key(provider, base_url or url, api_key, cfg)
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
