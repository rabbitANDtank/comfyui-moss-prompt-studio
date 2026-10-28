"""节点 3：MOSS Prompt Compiler —— 官方口径编译，输出接原生 MOSS-Image 节点。"""
from __future__ import annotations

try:
    from ..py.compile_prompt import compile_draft, validate_compiled
    from ..py.sniff import SniffError, detect, strict_loads
except ImportError:  # 兼容非包方式导入
    from py.compile_prompt import compile_draft, validate_compiled
    from py.sniff import SniffError, detect, strict_loads

DEFAULT_NEGATIVE = "nsfw, worst quality, bad anatomy, watermark, jpeg artifact"


class MOSSCompiler:
    CATEGORY = "MOSS Prompt Studio"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING", "INT", "INT", "STRING")
    RETURN_NAMES = ("prompt", "negative", "width", "height", "clip_text")
    OUTPUT_TOOLTIPS = (
        "单行紧凑 JSON，接 MOSS-Image 文本编码节点的正向提示词",
        "官方推荐负向提示词（可改）",
        "layout 取草稿宽高；caption 用下方 widget 兜底",
        "同上",
        "给 CLIP 文本编码器的文本口：默认等于 prompt；勾选 clip_include_negative 时在其后拼接负向提示词",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "draft": ("STRING", {"forceInput": True, "tooltip": "草稿 JSON（中文经 Translator 转英文后接入；Ctrl+B 旁路翻译节点可中文直连）"}),
                "negative": ("STRING", {"default": DEFAULT_NEGATIVE, "multiline": True}),
                "width": ("INT", {"default": 1024, "min": 64, "max": 15360, "step": 16,
                                  "tooltip": "仅 caption 形态使用（caption JSON 不含尺寸）"}),
                "height": ("INT", {"default": 1024, "min": 64, "max": 15360, "step": 16, "tooltip": "同上"}),
                "clip_include_negative": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "clip_text 输出口是否在正向 JSON 后拼接负向提示词（拼接格式：正向, 负向）",
                }),
            },
        }

    def run(self, draft, negative, width, height, clip_include_negative=False):
        s = (draft or "").strip()
        if not s:
            raise SniffError("draft 为空")
        obj = strict_loads(s)
        kind = detect(obj)

        if kind == "caption":
            cap = obj.get("caption")
            if not isinstance(cap, str) or not cap.strip():
                raise SniffError("caption 必须是非空字符串")
            prompt = json_dumps_compact({"caption": cap})
            w, h = int(width), int(height)
        elif kind == "draft":
            prompt = compile_draft(obj)
            w, h = int(obj["width"]), int(obj["height"])
        else:  # compiled：已是模型输入形态，校验后原样紧凑重排
            prompt = validate_compiled(obj)
            w, h = int(width), int(height)
        clip_text = prompt + ", " + negative if clip_include_negative else prompt
        return (prompt, negative, w, h, clip_text)


def json_dumps_compact(o) -> str:
    import json

    return json.dumps(o, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
