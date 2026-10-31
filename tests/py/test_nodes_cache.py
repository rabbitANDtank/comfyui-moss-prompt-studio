"""editor/translator/compiler 节点行为与译文缓存 / IS_CHANGED 失效 / 旧工作流 widget 顺序。"""
from helpers import (CUP, COMPILED_POSTER, DRAFT_ZH, GOOD_EN, MockHandler,  # noqa: F401
                     _expect, _run_translator, _start_mock, _with_config)

import copy
import json
import os
import tempfile

from py.compile_prompt import compile_draft  # noqa: F401
from py.sniff import SniffError  # noqa: F401
from py.translate import (AGENT_DEFAULT, resolve_editor_text,  # noqa: F401
                          source_signature)
from nodes.compiler import MOSSCompiler  # noqa: F401
from nodes.editor import MOSSEditor  # noqa: F401
from nodes.image_reverse import MOSSImageReverse  # noqa: F401
from nodes.translator import MOSSTranslator, clear_translation_cache  # noqa: F401


# ---------- 节点 ----------


def test_editor_node():
    src = json.dumps(CUP, ensure_ascii=False)
    out, w, h = MOSSEditor().run(src, 1536, 1024, "")
    assert json.loads(out) == CUP and (w, h) == (1536, 1024), "canvas 与草稿一致时不缩放"
    # edits 优先（必须带归属签名；换源后以 source 为准）
    edited = json.loads(src); edited["summary"]["caption"] += " 补一句。"
    out2, _, _ = MOSSEditor().run(
        src, 1024, 1024, json.dumps(edited, ensure_ascii=False),
        edits_source_sig=source_signature(src),
    )
    assert json.loads(out2)["summary"]["caption"].endswith("补一句。")
    # 已编译输入走反解
    out3, w3, h3 = MOSSEditor().run(json.dumps(COMPILED_POSTER, ensure_ascii=False), 1024, 1024, "")
    assert json.loads(out3)["width"] == 1024 and (w3, h3) == (1024, 1024)
    # 纯文本输入 → caption 形态，宽高输出取 canvas widget（并对齐 16）
    out4, w4, h4 = MOSSEditor().run("一段纯文本中文提示词。", 832, 1216, "")
    assert json.loads(out4) == {"caption": "一段纯文本中文提示词。"} and (w4, h4) == (832, 1216)
    _, w5, h5 = MOSSEditor().run("一段纯文本中文提示词。", 830, 1210, "")
    assert (w5, h5) == (832, 1216), "caption 宽高输出应为 16 的倍数"
    _expect(SniffError, MOSSEditor().run, '{"foo":1}', 1024, 1024, "")


def test_compiler_node():
    node = MOSSCompiler()
    # caption 形态：官方纯文本输出 + widget 兜底尺寸
    p, n, w, h, ct = node.run('{"caption":"你好"}', "neg", 832, 1216)
    assert (p, n, w, h) == ('你好', "neg", 832, 1216)
    assert ct == p, "clip_text 默认不含负向"
    # 勾选拼接负向
    _, _, _, _, ct2 = node.run('{"caption":"你好"}', "neg", 832, 1216, True)
    assert ct2 == '你好, neg'
    # layout 形态：编译 + 草稿尺寸
    p, n, w, h, ct = node.run(json.dumps(CUP, ensure_ascii=False), "neg", 512, 512)
    assert json.loads(p)["items"][0]["bbox"] == [-288, -592, 352, 32]
    assert (w, h) == (1536, 1024)
    assert ct == p
    # 已编译形态：校验后原样紧凑重排
    p, _, w, h, _ = node.run(json.dumps(COMPILED_POSTER), "neg", 640, 640)
    assert json.loads(p) == COMPILED_POSTER and (w, h) == (640, 640)
    # 连线 Editor 宽高：caption/已编译形态优先于 widget
    _, _, w, h, _ = node.run('{"caption":"你好"}', "neg", 1024, 1024, False, 1536, 1024)
    assert (w, h) == (1536, 1024)
    _, _, w, h, _ = node.run(json.dumps(COMPILED_POSTER), "neg", 640, 640, False, 832, 1216)
    assert (w, h) == (832, 1216)
    # layout 形态无视连线值，始终跟草稿尺寸
    _, _, w, h, _ = node.run(json.dumps(CUP, ensure_ascii=False), "neg", 512, 512, False, 512, 512)
    assert (w, h) == (1536, 1024)
    # 连线值必须是 16 的倍数
    _expect(SniffError, node.run, '{"caption":"你好"}', "neg", 1024, 1024, False, 1000, 1024)
    # widget 兜底值也必须过 16 倍数校验
    _expect(SniffError, node.run, '{"caption":"你好"}', "neg", 1000, 1024)


# ---------- 译文缓存（reuse_translation / canvas 尺寸进缓存键） ----------


def test_translator_reuses_cached_translation_when_enabled():
    """reuse_translation=True：相同翻译输入第二次直接复用，不再请求 LLM。"""
    clear_translation_cache()
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(GOOD_EN, ensure_ascii=False),
                             json.dumps(GOOD_EN, ensure_ascii=False)]
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        out1 = _run_translator(url, reuse_translation=True)[0]
        out2 = _run_translator(url, reuse_translation=True)[0]
        assert out1 == out2
        assert MockHandler.hits == 1, "第二次应复用缓存，不再请求 LLM"
    finally:
        srv.shutdown()
        clear_translation_cache()


def test_translator_cache_invalidates_on_draft_change():
    """草稿变化 → 缓存 key 变化 → 重新翻译。"""
    clear_translation_cache()
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(GOOD_EN, ensure_ascii=False)] * 2
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        _run_translator(url, reuse_translation=True)
        draft2 = copy.deepcopy(DRAFT_ZH)
        draft2["summary"]["caption"] = "另一张极简海报，蓝色山脉。"
        MOSSTranslator().run(
            json.dumps(draft2, ensure_ascii=False), "custom", url, "mock",
            "EMPTY", 0.3, "off", 30, "", "", None, reuse_translation=True,
        )
        assert MockHandler.hits == 2, "草稿变化应重新翻译"
    finally:
        srv.shutdown()
        clear_translation_cache()


def test_translator_reuse_off_forces_fresh_and_refreshes_cache():
    """关闭时强制重译并刷新缓存；再打开复用最近一次译文。"""
    clear_translation_cache()
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(GOOD_EN, ensure_ascii=False)] * 3
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        _run_translator(url, reuse_translation=False)
        _run_translator(url, reuse_translation=False)
        _run_translator(url, reuse_translation=True)
        assert MockHandler.hits == 2, "关闭时每次都翻译；打开后复用关闭时最后一次结果"
    finally:
        srv.shutdown()
        clear_translation_cache()


def test_translator_is_changed_tracks_reuse_switch():
    """开关本身要进 IS_CHANGED，切换后节点才会重新执行以决定读缓存/强制翻译。"""
    args = ('{"caption":"x"}', "custom", "http://x/v1", "m", "EMPTY", 0.3, "off", 30, "", "")
    h0 = MOSSTranslator.IS_CHANGED(*args, AGENT_DEFAULT, None, reuse_translation=False)
    h1 = MOSSTranslator.IS_CHANGED(*args, AGENT_DEFAULT, None, reuse_translation=True)
    assert h0 != h1
    assert MOSSTranslator.IS_CHANGED(*args, AGENT_DEFAULT, None, reuse_translation=True) == h1


def test_translator_reuse_widget_is_appended():
    """新开关必须追加在旧 widget 之后，旧工作流 widgets_values 不错位。"""
    req = [k for k in MOSSTranslator.INPUT_TYPES()["required"] if k != "draft"]
    opt = [k for k in MOSSTranslator.INPUT_TYPES()["optional"] if k != "extra_instructions"]
    # canvas_width/height 追加在 reuse_translation 之后（译已编译 JSON 时反解坐标用）
    assert (req + opt)[-4:] == ["agent_md", "reuse_translation",
                                "canvas_width", "canvas_height"]
    assert (req + opt)[-1] == "canvas_height"


def test_translator_compiled_non_1024_canvas():
    """Translator 的已编译输入必须按 canvas_width/height 反解，不能固定 1024。

    1536x1024 草稿编译后，中心原点 bbox 超出 1024 反解范围：旧实现会先调 LLM，
    再在 check_translation 的完整边界校验阶段失败（白跑 3 次）。修复后应一次成功。
    """
    clear_translation_cache()
    compiled = compile_draft(CUP)  # 1536x1024 → 单行已编译 JSON
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(CUP, ensure_ascii=False)]
        (out,) = MOSSTranslator().run(
            compiled, "custom", f"http://127.0.0.1:{srv.server_address[1]}/v1", "mock",
            "EMPTY", 0.3, "off", 30, "", "", None,
            reuse_translation=False, canvas_width=1536, canvas_height=1024,
        )
        assert json.loads(out) == CUP
        assert MockHandler.hits == 1, "非 1024 已编译输入应在一次调用内通过校验"
        sent = MockHandler.requests[0]
        user = sent["messages"][1]["content"]
        draft_sent = json.loads(user[user.index("{"):])
        assert (draft_sent["width"], draft_sent["height"]) == (1536, 1024)
        for it in draft_sent["items"]:
            x0, y0, x1, y1 = it["bbox"]
            assert 0 <= x0 < x1 <= 1536, f"反解 x 越界: {it['bbox']}"
            assert 0 <= y0 < y1 <= 1024, f"反解 y 越界: {it['bbox']}"
    finally:
        srv.shutdown()
        clear_translation_cache()


def test_translator_canvas_dimensions_affect_cache_and_is_changed():
    """canvas_width/height 必须进 IS_CHANGED 与翻译缓存键。"""
    args = (json.dumps(DRAFT_ZH, ensure_ascii=False), "custom", "http://x/v1", "m",
            "EMPTY", 0.3, "off", 30, "", "")
    h_small = MOSSTranslator.IS_CHANGED(*args, AGENT_DEFAULT, None, False, 1024, 1024)
    h_large = MOSSTranslator.IS_CHANGED(*args, AGENT_DEFAULT, None, False, 1536, 1024)
    assert h_small != h_large, "画布尺寸变化必须让节点重新执行"
    assert MOSSTranslator.IS_CHANGED(*args, AGENT_DEFAULT, None, False,
                                     1024, 1024) == h_small

    clear_translation_cache()
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(GOOD_EN, ensure_ascii=False)] * 2
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        _run_translator(url, reuse_translation=True)  # 默认 1024x1024
        MOSSTranslator().run(
            json.dumps(DRAFT_ZH, ensure_ascii=False), "custom", url, "mock",
            "EMPTY", 0.3, "off", 30, "", "", None,
            reuse_translation=True, canvas_width=1536, canvas_height=1024,
        )
        assert MockHandler.hits == 2, "画布尺寸变化后不得命中旧译文缓存"
        # 相同尺寸与输入 → 复用缓存，不再请求 LLM
        MOSSTranslator().run(
            json.dumps(DRAFT_ZH, ensure_ascii=False), "custom", url, "mock",
            "EMPTY", 0.3, "off", 30, "", "", None,
            reuse_translation=True, canvas_width=1536, canvas_height=1024,
        )
        assert MockHandler.hits == 2
    finally:
        srv.shutdown()
        clear_translation_cache()


# ---------- IS_CHANGED 追踪 LLM 配置 / AGENT.md / config.json ----------


def test_editor_is_changed_tracks_llm_config():
    """改 provider/model/思考档/温度/拆解契约/AGENT.md 必须让普通 Queue 缓存失效。"""
    base = dict(source="散文", canvas_width=1024, canvas_height=1024, edits="",
                ai_draft=True)
    h0 = MOSSEditor.IS_CHANGED(**base)
    variants = (
        {"provider": "deepseek"},
        {"base_url": "http://127.0.0.1:9/v1"},
        {"model": "deepseek-chat"},
        {"api_key": "test-key"},
        {"temperature": 0.9},
        {"thinking": "high"},
        {"timeout": 60},
        {"ai_prompt": "COMPLETELY DIFFERENT CONTRACT"},
        {"agent_md": "other.md"},
    )
    for kw in variants:
        assert MOSSEditor.IS_CHANGED(**base, **kw) != h0, f"改 {kw} 必须让缓存失效"
    assert MOSSEditor.IS_CHANGED(**base) == h0, "相同输入必须保持相同哈希"

    # AGENT.md 实际文本变化（同一选择项）也要失效
    with tempfile.TemporaryDirectory() as td:
        md = os.path.join(td, "AGENT.md")
        old_env = os.environ.get("COMFY_MOSS_STUDIO_AGENT_MD")
        os.environ["COMFY_MOSS_STUDIO_AGENT_MD"] = md
        try:
            with open(md, "w", encoding="utf-8") as f:
                f.write("指令 A")
            h_a = MOSSEditor.IS_CHANGED(**base)
            with open(md, "w", encoding="utf-8") as f:
                f.write("指令 B")
            h_b = MOSSEditor.IS_CHANGED(**base)
            assert h_a != h_b, "AGENT.md 内容变化必须让缓存失效"
        finally:
            if old_env is None:
                os.environ.pop("COMFY_MOSS_STUDIO_AGENT_MD", None)
            else:
                os.environ["COMFY_MOSS_STUDIO_AGENT_MD"] = old_env

    # config.json 内容变化同样纳入缓存键
    h_cfg1 = _with_config({"probe_allow_private": False}, lambda: MOSSEditor.IS_CHANGED(**base))
    h_cfg2 = _with_config({"probe_allow_private": True}, lambda: MOSSEditor.IS_CHANGED(**base))
    assert h_cfg1 != h_cfg2, "config.json 内容变化必须让缓存失效"


# ---------- source 签名优先级与旧工作流 widget 顺序 ----------


def test_editor_source_signature_priority():
    src = json.dumps(CUP, ensure_ascii=False)
    edited_obj = json.loads(src)
    edited_obj["summary"]["caption"] += " 补一句。"
    edited = json.dumps(edited_obj, ensure_ascii=False)

    # 签名匹配：edits 优先（画布/手改/拆解结果）
    out, _, _ = MOSSEditor().run(src, 1536, 1024, edited,
                                 edits_source_sig=source_signature(src))
    assert json.loads(out)["summary"]["caption"].endswith("补一句。")

    # 签名不匹配（换源）或缺失：source 优先，旧画布不能遮住新散文
    for sig in (source_signature("另一份 source"), ""):
        out, _, _ = MOSSEditor().run(src, 1536, 1024, edited, edits_source_sig=sig)
        assert json.loads(out) == CUP, f"sig={sig!r} 时应以 source 为准"

    # 只有 edits 没有 source：仍用 edits
    out, _, _ = MOSSEditor().run("", 1024, 1024, edited, edits_source_sig="whatever")
    assert json.loads(out)["summary"]["caption"].endswith("补一句。")

    assert resolve_editor_text(src, edited, source_signature(src)) == edited
    assert resolve_editor_text(src, edited, source_signature("x")) == src
    assert resolve_editor_text(src, edited, "") == src
    assert resolve_editor_text("", edited, "x") == edited
    assert resolve_editor_text("", "", "x") == ""

    # IS_CHANGED 同时纳入 source 与 edits：换 source 必须失效缓存
    base = dict(canvas_width=1024, canvas_height=1024, edits=edited,
                edits_source_sig=source_signature(src))
    h1 = MOSSEditor.IS_CHANGED(source=src, **base)
    assert h1 == MOSSEditor.IS_CHANGED(source=src, **base)
    h2 = MOSSEditor.IS_CHANGED(source=src + "\n", **base)
    assert h1 != h2
    # 普通 ai_draft 不再每次 NaN（同输入可缓存）；⚡拆解 force_split 才强制执行
    h3 = MOSSEditor.IS_CHANGED(source="散文", canvas_width=1024, canvas_height=1024,
                               edits="", ai_draft=True)
    assert h3 == MOSSEditor.IS_CHANGED(source="散文", canvas_width=1024,
                                       canvas_height=1024, edits="", ai_draft=True)
    assert str(MOSSEditor.IS_CHANGED(source="散文", canvas_width=1024,
                                     canvas_height=1024, edits="", ai_draft=True,
                                     force_split=True)) == "nan"


def test_reverse_legacy_widget_order_and_optional():
    req = list(MOSSImageReverse.INPUT_TYPES()["required"])
    optional_map = MOSSImageReverse.INPUT_TYPES()["optional"]
    opt = list(optional_map)
    # 旧工作流 widgets_values_named 的顺序（最后一项 DOM widget 无输入）
    old_order = [
        "provider", "base_url", "model", "api_key", "temperature", "thinking",
        "timeout", "canvas_width", "canvas_height", "max_items",
        "system_prompt", "extra_body", "agent_md",
    ]
    # forceInput 不产生 widget；按 required + optional 展开节点控件顺序
    widget_order = [k for k in req if k not in ("image",)] + [
        k for k in opt if k != "extra_instructions"
    ]
    assert widget_order[:len(old_order)] == old_order, \
        f"旧 widgets_values 索引错位: {widget_order[:len(old_order)]}"
    assert opt.index("reverse_mode") > opt.index("agent_md")
    for k in ("include_text", "caption_detail", "tag_detail", "max_tokens",
              "image_detail", "image_max_side"):
        assert k in opt and opt.index(k) > opt.index("agent_md")
    assert MOSSImageReverse.OUTPUT_NODE is True, "⚡反推按钮需要节点可作为输出根"
    assert optional_map["enabled"][1]["default"] is False, "未启用时完整 Queue 不得调用 LLM"
    assert optional_map["reverse_mode"][1]["default"] == "standard", "新节点默认 standard"
    assert opt.index("enabled") > opt.index("image_max_side")

    # enabled=False：立即返回，不解析图片、不发网络请求（image=None 也不报错）
    disabled = MOSSImageReverse().run(
        None, "custom", "http://127.0.0.1:1/v1", "m", "EMPTY",
        enabled=False,
    )
    assert disabled == ("", "", "")

    # 模拟旧 autosave：14 个值，索引 9=旧 max_items=6，索引 13=DOM 空串
    legacy_values = ["mimo", "", "", "EMPTY", 0.3, "off", 180, 0, 0, 6,
                     "system prompt", "", AGENT_DEFAULT, ""]
    mapped = dict(zip(widget_order, legacy_values))
    assert mapped["max_items"] == 6
    assert mapped["system_prompt"] == "system prompt"
    assert mapped["extra_body"] == ""
    assert mapped["agent_md"] == AGENT_DEFAULT
    assert mapped["reverse_mode"] == "", "旧 DOM 占位应落到 reverse_mode，前端纠正为 standard"
    # 无前端（API/无头）加载旧工作流时 "" 也必须能通过 ComfyUI 组合值校验
    assert "" in MOSSImageReverse.INPUT_TYPES()["optional"]["reverse_mode"][0]


def test_editor_legacy_widget_order():
    old_order = [
        "source", "canvas_width", "canvas_height", "edits", "ai_draft",
        "provider", "base_url", "model", "api_key", "temperature", "thinking",
        "timeout", "ai_prompt", "agent_md", "replace_edits", "last_applied_sig",
    ]
    req = list(MOSSEditor.INPUT_TYPES()["required"])
    opt = list(MOSSEditor.INPUT_TYPES()["optional"])
    # forceInput 输入不生成 widget；pose_mode/apply_pose 保持 v1.7.0 尾部定位，
    # pose_in/pose_in_2 是 v1.7.1 追加在其后的新 widget
    widget_order = list(req) + [k for k in opt
                                if k not in ("underlay", "reverse_in")]
    assert widget_order[:len(old_order)] == old_order, \
        f"Editor 旧 widgets_values 索引错位: {widget_order[:len(old_order)]}"
    assert widget_order[len(old_order):] == [
        "edits_source_sig", "force_split",
        "pose_mode", "apply_pose", "pose_in", "pose_in_2",
        # v1.8 服装注入：全部追加在姿势控件之后，旧工作流索引不变
        "costume_mode", "apply_costume", "costume_in", "costume_in_2",
    ]


def test_editor_layout_rescales_to_canvas_setting():
    """layout 画布尺寸遵循 Editor 的 canvas 设定（绿框）：draft 尺寸不同时
    等比重缩放（bbox 同步），出图尺寸与画布一致（v1.9.0-beta0 用户确认语义）。"""
    d = json.loads(json.dumps(CUP, ensure_ascii=False))  # 1536x1024
    src = json.dumps(d, ensure_ascii=False)
    out, w, h = MOSSEditor().run(src, 768, 512, "")      # canvas 减半
    r = json.loads(out)
    assert (w, h) == (768, 512), "输出宽高应为 canvas 设定"
    assert r["width"] == 768 and r["height"] == 512
    for i, it in enumerate(r["items"]):
        b0 = d["items"][i]["bbox"]
        expect = [round(v * 0.5) for v in b0]  # 等比 0.5（_repair_ai_draft 的缩放口径）
        got = it["bbox"]
        assert abs(got[0] - expect[0]) <= 1 and abs(got[2] - expect[2]) <= 1,             f"bbox 应等比缩放: {b0} -> {got}"
    # canvas 与草稿一致时不缩放（回归）
    out2, w2, h2 = MOSSEditor().run(src, 1536, 1024, "")
    assert (w2, h2) == (1536, 1024) and json.loads(out2)["items"][0]["bbox"] == d["items"][0]["bbox"]
