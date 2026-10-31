"""MOSS Prompt Studio

中文草稿/图片 → 画布编辑/LLM 反推 → LLM 翻译（MiMo/DeepSeek/MiniMax）→ 官方口径编译 → 原生 MOSS-Image 节点。
"""
from .nodes import MOSSCompiler, MOSSEditor, MOSSImageReverse, MOSSTranslator

__version__ = "1.7.2"

NODE_CLASS_MAPPINGS = {
    "MOSSEditor": MOSSEditor,
    "MOSSTranslator": MOSSTranslator,
    "MOSSCompiler": MOSSCompiler,
    "MOSSImageReverse": MOSSImageReverse,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MOSSEditor": "MOSS 提示词编辑器 (Editor)",
    "MOSSTranslator": "MOSS 提示词翻译器 中→英 (Translator)",
    "MOSSCompiler": "MOSS 提示词编译器 (Compiler)",
    "MOSSImageReverse": "MOSS 图像反推提示词 (Image→Prompt)",
}

try:
    from .server_routes import register_routes
    register_routes()
except Exception as e:  # 非 ComfyUI 环境（离线测试）或路由已冻结时不影响节点加载
    print(f"[MOSS Studio] 连接测试路由未注册: {e}")

WEB_DIRECTORY = "./web"

__all__ = ["__version__", "NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
