"""节点 1：MOSS Prompt Editor —— 粘贴源归一 + 画布编辑结果出口 + 可选 AI 拆解。

AI 拆解：勾选 ai_draft 且输入是散文/caption 时，调用内网 LLM（与翻译节点同一套
OpenAI 兼容栈）把散文拆成官方 layout 草稿（summary + items 带 bbox），经完整校验后
通过 ui.mps_draft 写回前端 edits widget → 画布出现可编辑的框。
已经是 layout/已编译形态时 ai_draft 不生效（直接透传）。
"""
from __future__ import annotations

import json

try:
    from ..py.dims import snap16
    from ..py.sniff import sniff
    from ..py.translate import (
        AGENT_DEFAULT,
        COSTUME_MODES,
        DEFAULT_AI_DRAFT_PROMPT,
        POSE_MODES,
        agent_md_widget_options,
        apply_costume_to_draft,
        apply_pose_to_draft,
        load_agent_md,
        load_config,
        prose_to_layout,
        resolve_api_key,
        resolve_editor_text,
        resolve_endpoint,
        source_signature,
    )
except ImportError:  # 兼容非包方式导入
    from py.dims import snap16
    from py.sniff import sniff
    from py.translate import (
        AGENT_DEFAULT,
        COSTUME_MODES,
        DEFAULT_AI_DRAFT_PROMPT,
        POSE_MODES,
        agent_md_widget_options,
        apply_costume_to_draft,
        apply_pose_to_draft,
        load_agent_md,
        load_config,
        prose_to_layout,
        resolve_api_key,
        resolve_editor_text,
        resolve_endpoint,
        source_signature,
    )


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
                   pose_in="", pose_in_2="", pose_mode="自动", apply_pose=-1,
                   costume_in="", costume_in_2="", costume_mode="自动", apply_costume=-1,
                   provider="mimo", base_url="", model="", api_key="EMPTY",
                   temperature=0.3, thinking="off", timeout=180, ai_prompt="",
                   agent_md=AGENT_DEFAULT, **kwargs):
        import hashlib

        if underlay is not None:
            # 底图内容无法便宜地哈希；有底图时强制重跑，避免换图后仍显示旧预览
            return float("nan")
        # ⚡拆解按钮带 force_split=true：每次点击都要真正执行
        if ai_draft and force_split:
            return float("nan")
        # 📥姿势/📥服装按钮带 apply_* != -1（-2 自动 / >=0 指定目标）：绕过缓存保证 ui 写回必达
        if isinstance(apply_pose, int) and apply_pose != -1:
            return float("nan")
        if isinstance(apply_costume, int) and apply_costume != -1:
            return float("nan")
        # LLM 配置与 AGENT.md/config.json 实际内容也要进缓存键（与 Translator 同款）：
        # 否则改 provider/model/thinking/ai_prompt 后普通 Queue 会命中旧缓存，新配置不生效。
        try:
            agent_text = load_agent_md(agent_md)
        except Exception as e:  # 文件标记错误也要让缓存失效，执行时再报明确错误
            agent_text = f"ERR:{e}"
        try:
            cfg_text = json.dumps(load_config(), sort_keys=True, ensure_ascii=False)
        except Exception as e:
            cfg_text = f"ERR:{e}"
        # 缓存键同时纳入 source 和 edits：只哈希 edits 会让“换了 source 但 edits
        # 非空”命中旧缓存，新散文永远不会被解析。姿势输入同理：换了姿势要让
        # 普通队列的缓存失效（实际重拆仍需 ⚡拆解，见 pose_in tooltip）。
        payload = json.dumps(
            {
                "source": source or "", "edits": edits or "",
                "edits_source_sig": edits_source_sig or "",
                "canvas_width": canvas_width, "canvas_height": canvas_height,
                "ai_draft": bool(ai_draft),
                "pose_in": pose_in or "", "pose_in_2": pose_in_2 or "",
                "pose_mode": pose_mode or "自动",
                "costume_in": costume_in or "", "costume_in_2": costume_in_2 or "",
                "costume_mode": costume_mode or "自动",
                "provider": provider, "base_url": base_url, "model": model,
                "api_key": api_key, "temperature": temperature,
                "thinking": thinking, "timeout": timeout, "ai_prompt": ai_prompt,
                "agent_md": agent_md, "agent_text": agent_text, "config": cfg_text,
            },
            ensure_ascii=False, sort_keys=True, default=str,
        )
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
                # 姿势注入（v1.7.x）：pose_mode/apply_pose 在前保持 v1.7.0 工作流的
                # widgets_values 尾部定位不错位；pose_in/pose_in_2 为 v1.7.1 追加的
                # 可点选/可手填 widget（连线时连线值优先，旧 forceInput 连线依旧生效）
                "pose_mode": (list(POSE_MODES), {
                    "default": "自动",
                    "tooltip": "姿势注入闸门：自动=拆解 LLM 按人数套用（1人→pose_in；2人→双参考分人/双人姿势整体/单人姿势给主角色）；强制双人=散文人数含糊时手动确认按 2 人套用；关闭=忽略姿势输入（免拔线做 A/B 对照）",
                }),
                "apply_pose": ("INT", {
                    "default": -1, "min": -2, "max": 63, "hidden": True, "socketless": True,
                    "tooltip": "内部：📥姿势 按钮提交时的注入指令。-1=不注入（普通 Queue 默认）；-2=自动分配（双参考按左右、双人姿势给两人、单姿势给最突出角色）；>=0=只注入该下标的角色（画布选中目标）",
                }),
                "pose_in": ("STRING", {
                    "default": "", "multiline": True,
                    "placeholder": "姿势参考①：点工具栏 🎯姿势① 选择，或粘贴/连线任意动作描述",
                    "tooltip": "姿势参考①：点工具栏 🎯姿势① 从图库选（需另装 comfyui-pose-gallery），也可手填动作描述或连线 STRING（如 Pose Gallery.description，连线值优先）。仅 ⚡拆解/📥注入时生效：替代角色 caption 中的动作措辞，服装/外貌/表情保留。双人且填了 pose_in_2 时本输入给 bbox 更靠左的角色；双人姿势则两人整体套用。拆解写回后换姿势需再点 ⚡拆解 或 📥姿势",
                }),
                "pose_in_2": ("STRING", {
                    "default": "", "multiline": True,
                    "placeholder": "姿势参考②：双人分别指定时给另一角色，点 🎯姿势② 选择",
                    "tooltip": "姿势参考②：双人分别指定姿势时给另一角色（与 pose_in 配对）；点 🎯姿势② 选择或手填/连线",
                }),
                # 服装注入（v1.8）：追加在全部姿势控件之后，保证旧工作流 widgets_values
                # 的尾部定位不错位。仅消费 STRING（Costume Gallery.description 或手填）。
                "costume_mode": (list(COSTUME_MODES), {
                    "default": "自动",
                    "tooltip": "服装注入闸门：自动=拆解 LLM 按人数套用；关闭=忽略服装输入（免拔线做 A/B 对照）。服装按角色分别套用，没有姿势的“强制双人”概念",
                }),
                "apply_costume": ("INT", {
                    "default": -1, "min": -2, "max": 63, "hidden": True, "socketless": True,
                    "tooltip": "内部：📥服装 按钮提交时的注入指令。-1=不注入（普通 Queue 默认）；-2=自动分配（双参考按左右、单参考给最突出角色）；>=0=只注入该下标的角色（画布选中目标）",
                }),
                "costume_in": ("STRING", {
                    "default": "", "multiline": True,
                    "placeholder": "服装参考①：连接 Costume Gallery 的 description，或粘贴任意服装描述",
                    "tooltip": "服装参考①：把 comfyui-costume-gallery 的 description（分类 / 关键词…）连到这里，或手填/连线任意服装描述（连线值优先）。仅 ⚡拆解（LLM 展开）/📥服装（本地短语）时生效；会替换角色 caption 里的服装措辞，保留外貌/姿势/表情。双人且填了 costume_in_2 时本输入给 bbox 更靠左的角色。注意：不要接该节点的 image 输出，也不要接 Editor.underlay",
                }),
                "costume_in_2": ("STRING", {
                    "default": "", "multiline": True,
                    "placeholder": "服装参考②：双人分别指定时给另一角色",
                    "tooltip": "服装参考②：双人分别指定服装时给另一角色（与 costume_in 配对）；手填或连接第二个 Costume Gallery 的 description",
                }),
            },
        }

    def run(self, source, canvas_width, canvas_height, edits,
            ai_draft=False, provider="mimo", base_url="", model="", api_key="EMPTY",
            temperature=0.3, thinking="off", timeout=180, ai_prompt="",
            agent_md=AGENT_DEFAULT, underlay=None, reverse_in=None,
            replace_edits=True, last_applied_sig="", edits_source_sig="",
            force_split=False, pose_in=None, pose_in_2=None, pose_mode="自动",
            apply_pose=-1, costume_in=None, costume_in_2=None, costume_mode="自动",
            apply_costume=-1):
        # reverse_in / replace_edits / last_applied_sig 只供前端反推写回使用，后端不参与解析
        # source/edits 优先级：edits 只对写入时的那份 source 生效；换源后以 source 为准。
        text = resolve_editor_text(source, edits, edits_source_sig)
        info = sniff(text, canvas_width, canvas_height)
        ui = {}

        # 📥姿势/📥服装 本地注入：不调 LLM，把图库描述短语写进已拆解画布的角色
        # caption，注入后过官方 validate_draft 再经 ui.mps_draft 写回（与拆解同一条
        # 写回通道）。apply_*= -1 不注入；-2 自动分配；>=0 只注入该下标角色。
        # 两者可同时生效（先姿势后服装），由前端按钮分别提交。
        pose_apply = isinstance(apply_pose, int) and apply_pose != -1
        costume_apply = isinstance(apply_costume, int) and apply_costume != -1
        if pose_apply or costume_apply:
            if info["kind"] != "layout":
                what = "姿势/服装" if pose_apply and costume_apply else ("姿势" if pose_apply else "服装")
                raise ValueError(
                    f"注入{what}需要拆解完成的画布（layout 形态）；当前输入是散文/caption，请先点 ⚡拆解")
            draft = info["draft"]
            if pose_apply:
                draft = apply_pose_to_draft(
                    draft, pose_in or "", pose_in_2 or "",
                    mode=pose_mode or "自动",
                    target_index=(apply_pose if apply_pose >= 0 else -1))
            if costume_apply:
                draft = apply_costume_to_draft(
                    draft, costume_in or "", costume_in_2 or "",
                    mode=costume_mode or "自动",
                    target_index=(apply_costume if apply_costume >= 0 else -1))
            info = {"kind": "layout", "draft": draft}
            ui["mps_draft"] = json.dumps(draft, ensure_ascii=False, indent=2)
            # 结果自带 source 归属：前端用它与 currentSourceSig() 比较，判断迟到
            # 结果是否属于当前 source（P1-3/P1-4），不再只依赖内存里的 pending 槽位。
            # 与 resolve_editor_text / 前端 currentSourceSig 一致：按 strip 后的 source 签名
            ui["mps_src_sig"] = source_signature((source or "").strip())

        elif ai_draft and info["kind"] == "caption":
            cfg = load_config()
            url, mdl = resolve_endpoint(provider, base_url, model, thinking, cfg)
            # 只在目标端点是该 provider 的 config/默认地址时回退配置 key
            key, _auth = resolve_api_key(provider, base_url or url, api_key, cfg)
            draft = prose_to_layout(
                info["draft"]["caption"],
                width=snap16(canvas_width), height=snap16(canvas_height),
                base_url=url, api_key=key, model=mdl,
                temperature=temperature, thinking=thinking, provider=provider,
                timeout=int(timeout), system_prompt=ai_prompt, agent_md=agent_md,
                pose_a=pose_in or "", pose_b=pose_in_2 or "",
                pose_mode=pose_mode or "自动",
                costume_a=costume_in or "", costume_b=costume_in_2 or "",
                costume_mode=costume_mode or "自动",
            )
            info = {"kind": "layout", "draft": draft}
            ui["mps_draft"] = json.dumps(draft, ensure_ascii=False, indent=2)
            # 与 resolve_editor_text / 前端 currentSourceSig 一致：按 strip 后的 source 签名
            ui["mps_src_sig"] = source_signature((source or "").strip())

        out = json.dumps(info["draft"], ensure_ascii=False, indent=2)
        # 与草稿同源的宽高：layout 用草稿尺寸；caption 用本节点 canvas_width/height，
        # 接给 Compiler 的 canvas_width/height 输入即可保证生图尺寸与画布一致。
        if info["kind"] == "layout":
            out_w, out_h = int(info["draft"]["width"]), int(info["draft"]["height"])
        else:
            out_w, out_h = snap16(canvas_width), snap16(canvas_height)

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
