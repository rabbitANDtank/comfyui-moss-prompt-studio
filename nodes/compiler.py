"""节点 3：MOSS Prompt Compiler —— 官方口径编译，输出接原生 MOSS-Image 节点。

宽高规则（三种输入形态）：
- layout 草稿：始终用草稿自带的 width/height（bbox 就是这些像素，连线和 widget 都不覆盖）。
- caption / 已编译：JSON 不含尺寸，优先用 canvas_width/canvas_height 连线；
  未连线才用本节点 width/height widget。
"""
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
    DESCRIPTION = (
        "宽高规则：layout 草稿始终用草稿 width/height；caption/已编译形态优先用 "
        "canvas_width/canvas_height 连线，未连线才用本节点 width/height widget。"
        "canvas_width/height 未接时会沿 draft 上游自动连接 Editor 的 width/height 输出；"
        "接好后 width/height widget 会自动跟随显示连线值。"
    )
    RETURN_TYPES = ("STRING", "STRING", "INT", "INT", "STRING")
    RETURN_NAMES = ("prompt", "negative", "width", "height", "clip_text")
    OUTPUT_TOOLTIPS = (
        "caption 形态为官方纯文本；layout/已编译形态为单行紧凑 JSON；直接接 MOSS-Image 文本编码节点",
        "官方推荐负向提示词（可改）",
        "layout 取草稿宽高；caption/已编译优先取 canvas_width 连线，未连线才用本节点 width widget",
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
                                  "tooltip": "caption/已编译形态的兜底宽度：接了 canvas_width 就用连线值，未接才用此值；layout 形态忽略此值，始终用草稿 width"}),
                "height": ("INT", {"default": 1024, "min": 64, "max": 15360, "step": 16,
                                   "tooltip": "caption/已编译形态的兜底高度：接了 canvas_height 就用连线值，未接才用此值；layout 形态忽略此值，始终用草稿 height"}),
                "clip_include_negative": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "clip_text 输出口是否在正向 JSON 后拼接负向提示词（拼接格式：正向, 负向）",
                }),
            },
            "optional": {
                "canvas_width": ("INT", {
                    "forceInput": True,
                    "tooltip": "接 Editor 的 width 输出：caption/已编译形态优先用它作为生图宽（官方要求 16 倍数）；layout 形态始终用草稿 width",
                }),
                "canvas_height": ("INT", {
                    "forceInput": True,
                    "tooltip": "接 Editor 的 height 输出：caption/已编译形态优先用它作为生图高（官方要求 16 倍数）；layout 形态始终用草稿 height",
                }),
            },
        }

    def run(self, draft, negative, width, height, clip_include_negative=False,
            canvas_width=None, canvas_height=None):
        s = (draft or "").strip()
        if not s:
            raise SniffError("draft 为空")
        obj = strict_loads(s)
        kind = detect(obj)

        if kind == "caption":
            cap = obj.get("caption")
            if not isinstance(cap, str) or not cap.strip():
                raise SniffError("caption 必须是非空字符串")
            # 官方 caption 编译产物是纯文本（compile_prompt.py 的 caption 分支）；
            # 不要包 {"caption":...} JSON，否则与官方口径不一致。
            prompt = cap
            # caption / 已编译形态的 JSON 里没有尺寸：优先用 Editor 连线过来的宽高，否则用本节点 widget。
            w, h = _resolve_size(canvas_width, canvas_height, width, height)
        elif kind == "draft":
            prompt = compile_draft(obj)
            # layout 的框是草稿像素：尺寸必须跟草稿走，连线值只在 caption/已编译形态生效
            w, h = int(obj["width"]), int(obj["height"])
        else:  # compiled：已是模型输入形态，校验后原样紧凑重排
            prompt = validate_compiled(obj)
            w, h = _resolve_size(canvas_width, canvas_height, width, height)
        clip_text = prompt + ", " + negative if clip_include_negative else prompt
        return (prompt, negative, w, h, clip_text)


def _resolve_size(canvas_width, canvas_height, width, height):
    """caption/已编译形态的尺寸：连线值优先，否则回落 widget（两者都必须是 16 倍数）。"""
    w = _linked_dim(canvas_width, "canvas_width") if canvas_width is not None else _linked_dim(width, "width")
    h = _linked_dim(canvas_height, "canvas_height") if canvas_height is not None else _linked_dim(height, "height")
    return w, h


def _linked_dim(v, name: str) -> int:
    """校验从 Editor 连线进来的尺寸：官方要求 16 的正整数倍。"""
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0 or v % 16 != 0:
        raise SniffError(f"{name} 必须是 16 的正整数倍（当前 {v!r}）；请接 Editor 的 width/height 输出")
    return int(v)
