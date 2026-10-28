"""MOSS Prompt Studio

中文草稿 → 画布可视化编辑 → LLM 翻译（MiMo/DeepSeek）→ 官方口径编译 → 原生 MOSS-Image 节点。
"""
from .nodes import MOSSCompiler, MOSSEditor, MOSSTranslator

__version__ = "1.5.0"

NODE_CLASS_MAPPINGS = {
    "MOSSEditor": MOSSEditor,
    "MOSSTranslator": MOSSTranslator,
    "MOSSCompiler": MOSSCompiler,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MOSSEditor": "MOSS 提示词编辑器 (Editor)",
    "MOSSTranslator": "MOSS 提示词翻译器 中→英 (Translator)",
    "MOSSCompiler": "MOSS 提示词编译器 (Compiler)",
}

WEB_DIRECTORY = "./web"

__all__ = ["__version__", "NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
