"""节点 2：MOSS Prompt Translator —— 中文草稿 → 英文草稿（内网 LLM，OpenAI 兼容）。

提示词注入口 ×3：
  ① system_prompt widget（默认内建翻译契约，可整体改写）
  ② extra_instructions 输入口（连线动态注入，追加到对话末尾）
  ③ extra_body widget（原始 JSON 合并进请求体，兜底 provider 专属参数）
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict

try:
    from ..py.dims import snap16
    from ..py.sniff import sniff
    from ..py.translate import (
        AGENT_DEFAULT,
        DEFAULT_SYSTEM_PROMPT,
        TranslationError,
        agent_md_widget_options,
        build_body,
        load_agent_md,
        load_config,
        resolve_api_key,
        resolve_endpoint,
        translate,
    )
except ImportError:  # 兼容非包方式导入
    from py.dims import snap16
    from py.sniff import sniff
    from py.translate import (
        AGENT_DEFAULT,
        DEFAULT_SYSTEM_PROMPT,
        TranslationError,
        agent_md_widget_options,
        build_body,
        load_agent_md,
        load_config,
        resolve_api_key,
        resolve_endpoint,
        translate,
    )


# 译文缓存：key = 翻译请求指纹（规范化草稿 + 端点/模型/请求体/提示注入），
# value = 成功返回的英文草稿 JSON。只有 reuse_translation 开启时才读取；
# 关闭时仍会写入，方便“关闭强制重译 → 打开复用新译文”。
_TRANSLATION_CACHE_MAX = 64
_TRANSLATION_CACHE = OrderedDict()
_TRANSLATION_CACHE_LOCK = threading.Lock()


def clear_translation_cache() -> None:
    """测试/热重载用：清空进程内译文缓存。"""
    with _TRANSLATION_CACHE_LOCK:
        _TRANSLATION_CACHE.clear()


class MOSSTranslator:
    CATEGORY = "MOSS Prompt Studio"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("draft_en",)

    @classmethod
    def IS_CHANGED(cls, draft, provider, base_url, model, api_key, temperature, thinking,
                   timeout, system_prompt, extra_body, agent_md=AGENT_DEFAULT,
                   extra_instructions=None, reuse_translation=False,
                   canvas_width=1024, canvas_height=1024, **kwargs):
        """把外部文件内容也纳入缓存键：AGENT.md/config.json 改动、key 轮换都要重新执行。"""
        import hashlib
        import json

        try:
            agent_text = load_agent_md(agent_md)
        except Exception as e:  # 文件标记错误也要让缓存失效，执行时再报明确错误
            agent_text = f"ERR:{e}"
        try:
            cfg_text = json.dumps(load_config(), sort_keys=True, ensure_ascii=False)
        except Exception as e:
            cfg_text = f"ERR:{e}"
        payload = json.dumps({
            "draft": draft, "provider": provider, "base_url": base_url, "model": model,
            "api_key": api_key, "temperature": temperature, "thinking": thinking,
            "timeout": timeout, "system_prompt": system_prompt, "extra_body": extra_body,
            "agent_md": agent_md, "extra_instructions": extra_instructions,
            "agent_text": agent_text, "config": cfg_text,
            "reuse_translation": bool(reuse_translation),
            "canvas_width": canvas_width, "canvas_height": canvas_height,
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
                "draft": ("STRING", {"forceInput": True, "tooltip": "中文草稿 JSON（通常来自 Editor 节点）"}),
                "provider": (["mimo", "deepseek", "minimax", "custom"], {
                    "default": "mimo",
                    "tooltip": "手动切换；无自动 failover。custom 可填任意 OpenAI 兼容 URL（含 Ollama / LM Studio）",
                }),
                "base_url": ("STRING", {
                    "default": "",
                    "tooltip": "OpenAI 兼容地址，如 http://内网:8000/v1；留空则读 config.json；deepseek 预设 https://api.deepseek.com/v1",
                }),
                "model": ("STRING", {
                    "default": "",
                    "tooltip": "留空则读 config.json / provider 预设（deepseek 默认 deepseek-chat）",
                }),
                "api_key": ("STRING", {
                    "default": "EMPTY",
                    "tooltip": "留空/EMPTY 则读 config.json；内网免鉴权服务可保持 EMPTY",
                }),
                "temperature": ("FLOAT", {"default": 0.3, "min": 0.0, "max": 2.0, "step": 0.05}),
                "thinking": (["off", "low", "medium", "high"], {
                    "default": "off",
                    "tooltip": "off 会显式关闭思考（deepseek: reasoning_effort=none；mimo: thinking.type=disabled）。deepseek 档位 low→low/medium→high/high→max；mimo 只有开/关，low/medium/high 同为开启；custom 非 off 发 reasoning_effort，不认就用 extra_body 改",
                }),
                "timeout": ("INT", {"default": 180, "min": 10, "max": 1200}),
                "system_prompt": ("STRING", {
                    "default": DEFAULT_SYSTEM_PROMPT,
                    "multiline": True,
                    "tooltip": "注入口①：翻译契约，可整体改写；清空则恢复内建默认",
                }),
                "extra_body": ("STRING", {
                    "default": "", "multiline": True,
                    "tooltip": '注入口③：原始 JSON 合并进请求体（最后合并，stream 会被强制 false），如 {"chat_template_kwargs":{"thinking":true}}',
                }),
            },
            "optional": {
                "agent_md": (agent_md_widget_options(), {
                    "default": AGENT_DEFAULT,
                    "tooltip": "注入口⓪（追加注入）：默认读插件根目录 AGENT.md；可选 agents/ 目录下的 .md（像 LoadImage 一样下拉选择，新文件刷新页面后出现）；每次执行自动重读，改文件即刻生效；(不注入) 完全关闭",
                }),
                "extra_instructions": ("STRING", {
                    "forceInput": True,
                    "tooltip": "注入口②：连线注入的逐次附加指令，追加到对话末尾",
                }),
                "reuse_translation": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "复用译文缓存：开启后，相同翻译输入（中文草稿/模型/思考/提示注入等）直接复用上次成功的英文译文，不再调用 LLM，适合反复出图找最优图；改草稿或相关设置会自动重译。关闭时强制执行一次翻译并刷新缓存（用于强制重译）。",
                }),
                # 已编译 JSON 不含尺寸，反解中心原点坐标时需要真实画布大小；
                # 追加在 reuse_translation 之后，旧工作流 widgets_values 顺序不错位。
                "canvas_width": ("INT", {
                    "default": 1024, "min": 64, "max": 15360, "step": 16,
                    "tooltip": "画布宽：粘贴已编译 JSON（abs_centralized/yxyx）时用于反解坐标，需与原图一致（默认 1024）；caption/草稿形态不影响译文",
                }),
                "canvas_height": ("INT", {
                    "default": 1024, "min": 64, "max": 15360, "step": 16,
                    "tooltip": "画布高：粘贴已编译 JSON 时用于反解坐标，需与原图一致（默认 1024）；caption/草稿形态不影响译文",
                }),
            },
        }

    def run(self, draft, provider, base_url, model, api_key, temperature,
            thinking, timeout, system_prompt, extra_body, agent_md=AGENT_DEFAULT,
            extra_instructions=None, reuse_translation=False,
            canvas_width=1024, canvas_height=1024):
        # 已编译 JSON 不含画布尺寸：按节点的 canvas_width/height 反解中心原点坐标。
        # 非 1024 画布必须由调用方给出，否则反解出的 bbox 会越界并在校验阶段失败。
        cw, ch = snap16(canvas_width), snap16(canvas_height)
        src = sniff(draft, cw, ch)  # caption / layout 草稿 / 已编译反解

        cfg = load_config()
        url, mdl = resolve_endpoint(provider, base_url, model, thinking, cfg)
        # 只有目标端点就是该 provider 的 config/默认地址时才回退配置 key，
        # 防止工作流把 base_url 改到任意地址后把 config.json 的 key 发出去。
        key, _auth = resolve_api_key(provider, base_url or url, api_key, cfg)
        body = build_body(mdl, temperature, thinking, provider, extra_body)
        sys_prompt = (system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT

        # 翻译请求指纹：只含影响译文的输入（不含 api_key/timeout）。
        # 用 sniff 规范化后的 src，避免草稿仅空白/格式变化导致缓存失效。
        try:
            agent_text = load_agent_md(agent_md)
        except Exception as e:  # 文件错误交给 translate() 报明确错误；不写缓存
            agent_text = f"ERR:{e}"
        cache_payload = json.dumps({
            "src": src, "provider": provider, "url": url, "model": mdl,
            "body": body, "system_prompt": sys_prompt,
            "agent_text": agent_text, "extra_instructions": extra_instructions or "",
            # 已编译输入的反解坐标随画布尺寸变化，尺寸也必须进缓存键
            "canvas_width": cw, "canvas_height": ch,
        }, ensure_ascii=False, sort_keys=True, default=str)
        cache_key = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()
        if reuse_translation:
            with _TRANSLATION_CACHE_LOCK:
                cached = _TRANSLATION_CACHE.get(cache_key)
                if cached is not None:
                    _TRANSLATION_CACHE.move_to_end(cache_key)
            if cached is not None:
                print(f"[MOSS Studio] Translator 复用缓存译文 (key={cache_key[:8]})")
                return (cached,)

        try:
            out = translate(
                src,
                base_url=url,
                api_key=key,
                body=body,
                system_prompt=sys_prompt,
                extra_instructions=extra_instructions or "",
                timeout=int(timeout),
                agent_md=agent_md,
            )
        except TranslationError:
            raise
        except Exception as e:  # noqa: BLE001 —— 其他异常也以红字形式暴露给用户
            raise RuntimeError(f"翻译节点执行失败: {e}") from e
        out_text = json.dumps(out, ensure_ascii=False, indent=2)
        # 无论开关是否开启都写入；关闭时的运行就是一次“强制重译 + 刷新缓存”。
        with _TRANSLATION_CACHE_LOCK:
            _TRANSLATION_CACHE[cache_key] = out_text
            _TRANSLATION_CACHE.move_to_end(cache_key)
            while len(_TRANSLATION_CACHE) > _TRANSLATION_CACHE_MAX:
                _TRANSLATION_CACHE.popitem(last=False)
        return (out_text,)
