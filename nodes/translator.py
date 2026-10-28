"""节点 2：MOSS Prompt Translator —— 中文草稿 → 英文草稿（内网 LLM，OpenAI 兼容）。

提示词注入口 ×3：
  ① system_prompt widget（默认内建翻译契约，可整体改写）
  ② extra_instructions 输入口（连线动态注入，追加到对话末尾）
  ③ extra_body widget（原始 JSON 合并进请求体，兜底 provider 专属参数）
"""
from __future__ import annotations

import json

try:
    from ..py.sniff import sniff
    from ..py.translate import (
        AGENT_DEFAULT,
        DEFAULT_SYSTEM_PROMPT,
        TranslationError,
        agent_md_widget_options,
        build_body,
        load_config,
        resolve_endpoint,
        translate,
    )
except ImportError:  # 兼容非包方式导入
    from py.sniff import sniff
    from py.translate import (
        AGENT_DEFAULT,
        DEFAULT_SYSTEM_PROMPT,
        TranslationError,
        agent_md_widget_options,
        build_body,
        load_config,
        resolve_endpoint,
        translate,
    )


class MOSSTranslator:
    CATEGORY = "MOSS Prompt Studio"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("draft_en",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "draft": ("STRING", {"forceInput": True, "tooltip": "中文草稿 JSON（通常来自 Editor 节点）"}),
                "provider": (["mimo", "deepseek", "custom"], {
                    "default": "mimo",
                    "tooltip": "手动切换；无自动 failover",
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
                    "tooltip": '注入口③：原始 JSON 合并进请求体，最后合并可覆盖一切，如 {"chat_template_kwargs":{"thinking":true}}',
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
            },
        }

    def run(self, draft, provider, base_url, model, api_key, temperature,
            thinking, timeout, system_prompt, extra_body, agent_md=AGENT_DEFAULT,
            extra_instructions=None):
        src = sniff(draft)  # caption / layout 草稿；已编译格式按 1024 反解（仅译文，坐标不动）

        cfg = load_config()
        url, mdl = resolve_endpoint(provider, base_url, model, thinking, cfg)
        key = (api_key or "").strip()
        if key in ("", "EMPTY"):
            key = ((cfg.get(provider) or {}).get("api_key") or "").strip()
        body = build_body(mdl, temperature, thinking, provider, extra_body)
        sys_prompt = (system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT

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
        return (json.dumps(out, ensure_ascii=False, indent=2),)
