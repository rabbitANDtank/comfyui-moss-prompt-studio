"""AGENT.md 追加注入 / 文件限额 / 下拉选择 / VALIDATE_INPUTS 绕过 combo 校验。"""
from helpers import ROOT, _expect  # noqa: F401  (helpers 先导入以初始化 sys.path)

import os

from py.translate import (AGENT_DEFAULT, MAX_AGENT_FILE_BYTES,  # noqa: F401
                          TranslationError)
from nodes.editor import MOSSEditor  # noqa: F401
from nodes.image_reverse import MOSSImageReverse  # noqa: F401
from nodes.translator import MOSSTranslator  # noqa: F401

# 兼容垫片：原单文件位于 tests/run_tests.py，本文件有用例函数体通过 __file__
# 两级上溯反推仓库根目录（agents/ 目录定位）；拆分到 tests/py/ 后为保持函数体
# 零改动，将模块级 __file__ 指回原路径（dirname(dirname(__file__)) == ROOT）。
__file__ = os.path.join(ROOT, "tests", "run_tests.py")


def test_agent_md_append_injection():
    """AGENT.md 追加注入（语义移植自 Codex-X Append 模式）：追加而非替换，标记圈定片段。"""
    import tempfile
    from py.translate import append_agent_md, load_agent_md, AGENT_BEGIN, AGENT_END

    old = os.environ.get("COMFY_MOSS_STUDIO_AGENT_MD")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    try:
        # 1) 无标记 → 全文追加在系统提示词之后
        tmp.write("Always answer concisely.")
        tmp.close()
        os.environ["COMFY_MOSS_STUDIO_AGENT_MD"] = tmp.name
        out = append_agent_md("BASE CONTRACT")
        assert out.startswith("BASE CONTRACT") and AGENT_BEGIN in out and AGENT_END in out
        assert "Always answer concisely." in out
        assert out.index("BASE CONTRACT") < out.index(AGENT_BEGIN), "必须是追加，不能跑到基础契约前面"

        # 2) 有标记 → 只注入 BEGIN/END 之间的片段
        with open(tmp.name, "w", encoding="utf-8") as f:
            f.write(f"我的笔记不注入\n{AGENT_BEGIN}\nONLY THIS PART\n{AGENT_END}\n更多笔记\n")
        assert load_agent_md() == "ONLY THIS PART"
        assert "ONLY THIS PART" in append_agent_md("BASE") and "笔记" not in append_agent_md("BASE")

        # 3) 标记不成对 → 明确报错
        with open(tmp.name, "w", encoding="utf-8") as f:
            f.write(f"{AGENT_BEGIN}\nx\n{AGENT_BEGIN}\n{AGENT_END}\n")
        _expect(TranslationError, load_agent_md)
    finally:
        os.environ.pop("COMFY_MOSS_STUDIO_AGENT_MD", None)
        if old is not None:
            os.environ["COMFY_MOSS_STUDIO_AGENT_MD"] = old
        os.unlink(tmp.name)

    # 4) 文件不存在 → 原样返回，不注入
    os.environ["COMFY_MOSS_STUDIO_AGENT_MD"] = os.path.join(tempfile.gettempdir(), "moss_no_such_agent_md.md")
    try:
        assert append_agent_md("BASE CONTRACT") == "BASE CONTRACT"
    finally:
        os.environ.pop("COMFY_MOSS_STUDIO_AGENT_MD", None)
        if old is not None:
            os.environ["COMFY_MOSS_STUDIO_AGENT_MD"] = old


def test_agent_md_file_limits():
    """agents/ 下的符号链接和超大文件必须拒绝，防止任意文件读取/提示词爆炸。"""
    import tempfile

    from py.translate import load_agent_md
    agents_dir = os.path.join(ROOT, "agents")
    os.makedirs(agents_dir, exist_ok=True)
    big = os.path.join(agents_dir, "__test_big__.md")
    link = os.path.join(agents_dir, "__test_link__.md")
    target = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    try:
        target.write("outside")
        target.close()
        with open(big, "w", encoding="utf-8") as f:
            f.write("x" * (MAX_AGENT_FILE_BYTES + 1))
        _expect(TranslationError, load_agent_md, "__test_big__.md")
        try:
            os.symlink(target.name, link)
        except (OSError, NotImplementedError):
            pass  # Windows 无权限时不强求符号链接测试
        else:
            _expect(TranslationError, load_agent_md, "__test_link__.md")
    finally:
        for p in (big, link):
            if os.path.exists(p) or os.path.islink(p):
                os.unlink(p)
        os.unlink(target.name)


def test_translator_is_changed_tracks_agent_md():
    """AGENT.md 内容变化必须让 Translator 缓存失效（否则热重载/换词表不生效）。"""
    import tempfile

    old = os.environ.get("COMFY_MOSS_STUDIO_AGENT_MD")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    try:
        tmp.write("RULE A")
        tmp.close()
        os.environ["COMFY_MOSS_STUDIO_AGENT_MD"] = tmp.name
        args = ('{"caption":"x"}', "custom", "http://x/v1", "m", "EMPTY", 0.3, "off", 30, "", "")
        h1 = MOSSTranslator.IS_CHANGED(*args, AGENT_DEFAULT, None)
        with open(tmp.name, "w", encoding="utf-8") as f:
            f.write("RULE B")
        h2 = MOSSTranslator.IS_CHANGED(*args, AGENT_DEFAULT, None)
        assert h1 != h2, "AGENT.md 内容变了，缓存键必须变"
    finally:
        os.environ.pop("COMFY_MOSS_STUDIO_AGENT_MD", None)
        if old is not None:
            os.environ["COMFY_MOSS_STUDIO_AGENT_MD"] = old
        os.unlink(tmp.name)


def test_agent_md_widget_selection():
    """agent_md 下拉选择：默认根目录 / 不注入 / agents/ 文件 / 防穿越 / 每次重读盘。"""
    import tempfile
    from py.translate import (AGENT_DEFAULT, AGENT_OFF, append_agent_md, load_agent_md,
                              list_agent_md_files, agent_md_widget_options)

    agents_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents")
    os.makedirs(agents_dir, exist_ok=True)

    old = os.environ.get("COMFY_MOSS_STUDIO_AGENT_MD")
    root_tmp = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    sel_tmp_path = os.path.join(agents_dir, "__test_sel__.md")
    try:
        # 根目录默认（经 env 指到临时文件，避免污染真实 AGENT.md）
        root_tmp.write("ROOT RULE")
        root_tmp.close()
        os.environ["COMFY_MOSS_STUDIO_AGENT_MD"] = root_tmp.name
        assert load_agent_md(AGENT_DEFAULT) == "ROOT RULE"
        assert load_agent_md("") == "ROOT RULE", "空选择 = 默认（兼容旧工作流）"

        # 不注入
        assert load_agent_md(AGENT_OFF) == ""
        assert append_agent_md("BASE", AGENT_OFF) == "BASE"

        # agents/ 文件选择 + 每次执行重读盘（自动替换）
        with open(sel_tmp_path, "w", encoding="utf-8") as f:
            f.write("VERSION 1")
        assert load_agent_md("__test_sel__.md") == "VERSION 1"
        out = append_agent_md("BASE", "__test_sel__.md")
        assert "VERSION 1" in out and out.startswith("BASE")
        with open(sel_tmp_path, "w", encoding="utf-8") as f:
            f.write("VERSION 2")
        assert load_agent_md("__test_sel__.md") == "VERSION 2", "改文件必须即刻生效（无缓存）"

        # 下拉列表：agents/ 内 .md 出现，非 .md 不出现
        with open(os.path.join(agents_dir, "__test_sel__.txt"), "w") as f:
            f.write("x")
        opts = agent_md_widget_options()
        assert opts[0] == AGENT_DEFAULT and opts[1] == AGENT_OFF
        assert "__test_sel__.md" in opts and "__test_sel__.txt" not in opts
        assert set(list_agent_md_files()) <= set(opts)

        # 路径穿越被 basename 挡住 → 落到 agents/__test_sel__.md 而不是上级目录
        assert load_agent_md("../__test_sel__.md") == "VERSION 2"

        # 不存在的文件 → 明确报错
        _expect(TranslationError, load_agent_md, "no_such.md")
    finally:
        os.environ.pop("COMFY_MOSS_STUDIO_AGENT_MD", None)
        if old is not None:
            os.environ["COMFY_MOSS_STUDIO_AGENT_MD"] = old
        os.unlink(root_tmp.name)
        for p in (sel_tmp_path, os.path.join(agents_dir, "__test_sel__.txt")):
            if os.path.exists(p):
                os.unlink(p)


def test_agent_md_validate_inputs_bypasses_combo_layer():
    """三个 LLM 节点的 agent_md 是动态 COMBO（agents/*.md）。

    删除/改名文件后旧值不在下拉列表里，ComfyUI 默认在进入 run 之前报
    "Value not in list"，节点内的友好报错不可达。VALIDATE_INPUTS 必须在
    argspec 中声明 agent_md（ComfyUI 据此跳过该输入的 combo/min/max 校验），
    对任意字符串返回 True；真实存在性仍由 load_agent_md 在运行时友好报错。
    """
    import inspect

    from py.translate import load_agent_md

    deleted = "__test_deleted_agent__.md"
    for cls in (MOSSEditor, MOSSTranslator, MOSSImageReverse):
        fn = cls.VALIDATE_INPUTS
        argspec = inspect.getfullargspec(fn)
        # 对齐 ComfyUI execution.validate_inputs 的跳过条件：
        #   x not in validate_function_inputs and not validate_has_kwargs
        assert "agent_md" in argspec.args, \
            f"{cls.__name__}.VALIDATE_INPUTS 必须声明 agent_md 才能跳过 combo 校验"
        assert argspec.varkw is None, \
            f"{cls.__name__} 不要用 **kwargs，避免连带跳过其他输入的默认校验"
        assert fn(deleted) is True
        assert fn("任意/不存在 的.md") is True
        assert fn() is True, "缺省/旧工作流无 agent_md 时也必须通过"

    # 运行时照旧做真实存在性校验，并给出中文友好错误
    _expect(TranslationError, load_agent_md, deleted)
