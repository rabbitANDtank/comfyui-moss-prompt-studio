"""AGENT.md 指令文件体系：列出/选择/安全读取托管块，并追加注入系统提示词。"""
from __future__ import annotations

import os
import stat

try:
    from .llm_client import TranslationError, _plugin_root
except ImportError:  # 兼容非包方式导入
    from llm_client import TranslationError, _plugin_root

# ---------- AGENT.md 追加注入（语义移植自 Codex-X 的 Append 模式）----------
# 持久化的用户指令文件：内容包在托管块标记里，追加在基础系统提示词
# （默认契约 / widget 改写后的契约）之后，不替换任何现有内容。
# 文件来源二选一：插件根目录 AGENT.md（默认），或 agents/ 目录下由节点下拉选择的 .md。
# 用户可在文件里放 BEGIN/END 标记圈定"要注入的部分"（Codex-X 托管块同款防御）。
AGENT_MD_FILENAME = "AGENT.md"
AGENT_DIR = "agents"
AGENT_DEFAULT = "(默认 AGENT.md)"
AGENT_OFF = "(不注入)"
AGENT_BEGIN = "<!-- MOSS-STUDIO:AGENT:BEGIN -->"
AGENT_END = "<!-- MOSS-STUDIO:AGENT:END -->"
MAX_AGENT_FILE_BYTES = 64 * 1024


def _agent_md_root_path() -> str:
    env = os.environ.get("COMFY_MOSS_STUDIO_AGENT_MD")
    return env or os.path.join(_plugin_root(), AGENT_MD_FILENAME)


def list_agent_md_files() -> list:
    """列出 agents/ 目录下可下拉选择的 .md 文件（LoadImage 列图片的同款交互）。"""
    d = os.path.join(_plugin_root(), AGENT_DIR)
    if not os.path.isdir(d):
        return []
    return sorted(
        f for f in os.listdir(d)
        if f.lower().endswith(".md")
        and os.path.isfile(os.path.join(d, f))
        and not os.path.islink(os.path.join(d, f))
    )


def agent_md_widget_options() -> list:
    """节点下拉框选项：默认根目录 / 关闭 / agents/ 下的文件。

    末尾带一个空串选项：旧工作流/自动恢复的前端可能存出 agent_md=''，
    校验器要求组合值必须在列表内，空串在列表里才能过（语义=默认）。
    """
    return [AGENT_DEFAULT, AGENT_OFF] + list_agent_md_files() + [""]


def _read_agent_file(path: str) -> str:
    """读单个指令文件并应用托管块标记规则；不存在返回 ""。

    安全约束：拒绝符号链接和非普通文件，并限制大小（防任意文件读取/超大文件塞进提示词）。
    """
    if not os.path.exists(path):
        return ""
    if os.path.islink(path):
        raise TranslationError(f"指令文件不能是符号链接: {path}")
    try:
        st = os.stat(path)
    except OSError as e:
        raise TranslationError(f"指令文件读取失败 [{path}]: {e}") from e
    if not stat.S_ISREG(st.st_mode):
        raise TranslationError(f"指令文件必须是普通文件: {path}")
    if st.st_size > MAX_AGENT_FILE_BYTES:
        raise TranslationError(
            f"指令文件过大 [{os.path.basename(path)}]: {st.st_size} 字节，"
            f"上限 {MAX_AGENT_FILE_BYTES} 字节"
        )
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_AGENT_FILE_BYTES + 1)
    except OSError as e:
        raise TranslationError(f"指令文件读取失败 [{path}]: {e}") from e
    if len(raw) > MAX_AGENT_FILE_BYTES:
        raise TranslationError(
            f"指令文件过大 [{os.path.basename(path)}]: 上限 {MAX_AGENT_FILE_BYTES} 字节"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise TranslationError(f"指令文件读取失败 [{path}]: {e}") from e
    nb, ne = text.count(AGENT_BEGIN), text.count(AGENT_END)
    if nb or ne:
        if nb != 1 or ne != 1 or text.index(AGENT_BEGIN) > text.index(AGENT_END):
            raise TranslationError(
                f"指令文件 [{os.path.basename(path)}] 中的 MOSS-STUDIO 托管标记不完整或重复："
                f"{AGENT_BEGIN} / {AGENT_END} 需各恰好出现一次且 BEGIN 在前"
            )
        text = text[text.index(AGENT_BEGIN) + len(AGENT_BEGIN): text.index(AGENT_END)]
    return text.strip()


def load_agent_md(selection: str = "") -> str:
    """按节点下拉选择解析注入来源（每次调用重读盘，改文件即刻生效）。

    - AGENT_OFF → 不注入
    - AGENT_DEFAULT / 空 → 插件根目录 AGENT.md（旧行为）
    - 其他 → agents/ 目录下同名文件（basename 防路径穿越；不存在则报错并列出可选项）
    """
    sel = (selection or "").strip()
    if sel == AGENT_OFF:
        return ""
    if not sel or sel == AGENT_DEFAULT:
        return _read_agent_file(_agent_md_root_path())
    name = os.path.basename(sel)  # 防路径穿越：只认文件名
    base = os.path.realpath(os.path.join(_plugin_root(), AGENT_DIR))
    path = os.path.join(base, name)
    real = os.path.realpath(path)
    try:
        inside = os.path.commonpath([base, real]) == base
    except ValueError:
        inside = False
    if not inside or os.path.islink(path):
        raise TranslationError(f"指令文件必须位于 agents/ 目录内且不能是符号链接: {name}")
    if not os.path.isfile(path):
        raise TranslationError(
            f"指令文件不存在: agents/{name}（现有: {list_agent_md_files() or '无'}；"
            f"下拉列表在刷新页面后更新，如同 LoadImage 的新图片）"
        )
    return _read_agent_file(path)


def append_agent_md(system_prompt: str, selection: str = "") -> str:
    """把选定的指令文件内容以托管块形式追加在系统提示词之后（追加注入，非替换）。"""
    extra = load_agent_md(selection)
    if not extra:
        return system_prompt
    return f"{system_prompt.rstrip()}\n\n{AGENT_BEGIN}\n{extra}\n{AGENT_END}"
