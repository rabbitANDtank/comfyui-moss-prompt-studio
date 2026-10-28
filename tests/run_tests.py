"""离线测试（不依赖 ComfyUI 运行环境）。

运行:  python tests/run_tests.py
覆盖: 官方口径编译(含官方文档给出的期望坐标) / 三形态识别与反解往返 /
      节点行为 / 翻译结构完整性校验 / endpoint 与请求体组装 /
      本地 mock 服务下的翻译 HTTP 成功-重试-失败路径。
"""
import copy
import http.server
import json
import os
import sys
import threading
import traceback

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from py.compile_prompt import DraftError, compile_draft, validate_compiled, validate_draft  # noqa: E402
from py.sniff import SniffError, sniff, strict_loads  # noqa: E402
from py.translate import (  # noqa: E402
    DEFAULT_AI_DRAFT_PROMPT,
    TranslationError,
    build_body,
    check_translation,
    extract_json,
    prose_to_layout,
    resolve_endpoint,
)
from nodes.compiler import MOSSCompiler  # noqa: E402
from nodes.editor import MOSSEditor  # noqa: E402
from nodes.translator import MOSSTranslator  # noqa: E402

# ---------- 官方示例数据 ----------

CUP = {  # 官方 skills/references/layout.md 的示例草稿
    "width": 1536, "height": 1024,
    "bbox_coord_space": "abs", "bbox_format": "xyxy",
    "summary": {
        "meta": ["product campaign", "studio photography"], "canvas": "single",
        "caption": "A warm studio campaign for a handmade ceramic cup, with an ivory backdrop, soft side lighting and restrained dark-green typography. Keep the ceramic texture tactile and the composition calm.",
    },
    "items": [
        {"label": "object", "caption": "An ivory ceramic cup with a rounded handle, fine speckled glaze and a soft contact shadow, photographed at a three-quarter angle.", "bbox": [176, 224, 800, 864]},
        {"label": "text", "caption": "Large dark-green Chinese title in crisp, moderately heavy lettering, with generous spacing and no backing panel.", "text": "把日常，过成诗。", "bbox": [928, 272, 1408, 448]},
        {"label": "text", "caption": "Smaller dark-green Chinese supporting copy, comfortably legible on two lines.", "text": "一杯温暖\n一刻从容", "bbox": [928, 512, 1376, 656]},
    ],
}

COMPILED_POSTER = {  # 官方 README 的模型输入示例（1024x1024）
    "summary": {"meta": ["poster"], "canvas": "single",
                "caption": "A minimalist travel poster with a cream background, a blue mountain illustration, and the title EXPLORE at the top."},
    "items": [
        {"label": "text", "caption": "Large dark-blue uppercase title centered at the top.", "text": "EXPLORE", "bbox": [-416, -384, -288, 384]},
        {"label": "object", "caption": "A flat blue mountain illustration in the lower center.", "bbox": [-192, -288, 384, 288]},
    ],
    "bbox_coord_space": "abs_centralized",
    "bbox_format": "yxyx",
}

DRAFT_ZH = {
    "width": 1024, "height": 1024,
    "bbox_coord_space": "abs", "bbox_format": "xyxy",
    "summary": {"meta": ["海报"], "canvas": "single",
                "caption": "一张极简旅行海报，米色背景，蓝色山脉插画，顶部标题。"},
    "items": [
        {"label": "object", "caption": "下中部一座扁平蓝色山脉插画。", "bbox": [128, 384, 896, 896]},
        {"label": "text", "caption": "顶部深蓝色大写标题。", "text": "探索世界", "bbox": [256, 96, 768, 224]},
    ],
}

GOOD_EN = {
    "width": 1024, "height": 1024,
    "bbox_coord_space": "abs", "bbox_format": "xyxy",
    "summary": {"meta": ["poster"], "canvas": "single",
                "caption": "A minimalist travel poster with a cream background, a blue mountain illustration, and a title at the top."},
    "items": [
        {"label": "object", "caption": "A flat blue mountain illustration in the lower center.", "bbox": [128, 384, 896, 896]},
        {"label": "text", "caption": "A dark-blue uppercase title at the top.", "text": "探索世界", "bbox": [256, 96, 768, 224]},
    ],
}


def _expect(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


# ---------- 编译器 ----------

def test_compile_official_examples():
    s = compile_draft(CUP)
    assert "\n" not in s, "编译产物必须是单行"
    out = json.loads(s)
    assert out["bbox_coord_space"] == "abs_centralized" and out["bbox_format"] == "yxyx"
    assert "width" not in out and "height" not in out
    assert out["items"][0]["bbox"] == [-288, -592, 352, 32], "官方 layout.md 给出的期望值"
    assert out["items"][1]["bbox"] == [-240, 160, -64, 640], "官方 test_compile_prompt.py 断言值"
    assert out["items"][1]["text"] == "把日常，过成诗。"


def test_validate_rejects():
    import copy

    _expect(DraftError, validate_draft, {**CUP, "width": 1000})          # 非 16 倍数
    _expect(DraftError, validate_draft, {**CUP, "extra": 1})             # 未知顶层字段
    bad = copy.deepcopy(CUP); bad["items"][0]["id"] = "x"
    _expect(DraftError, validate_draft, bad)                             # 未知 item 字段
    bad = copy.deepcopy(CUP); bad["items"][0]["bbox"] = [0, 0, 9999, 900]
    _expect(DraftError, validate_draft, bad)                             # 越界
    bad = copy.deepcopy(CUP); del bad["items"][1]["text"]
    _expect(DraftError, validate_draft, bad)                             # text 元素缺 text
    bad = copy.deepcopy(CUP); bad["items"][0]["text"] = "不该有"
    _expect(DraftError, validate_draft, bad)                             # 非 text 元素带 text
    bad = copy.deepcopy(CUP); bad["items"][0]["caption"] = "  "
    _expect(DraftError, validate_draft, bad)                             # 空 caption
    bad = copy.deepcopy(CUP); bad["items"] = []
    _expect(DraftError, validate_draft, bad)                             # 空 items
    _expect(DraftError, validate_compiled, {**COMPILED_POSTER, "width": 1024})  # 编译态不该有 width


def test_sniff_forms_and_roundtrip():
    # caption 形态
    info = sniff('{"caption":"你好，世界"}')
    assert info == {"kind": "caption", "draft": {"caption": "你好，世界"}}
    # 草稿形态原样
    info = sniff(json.dumps(CUP, ensure_ascii=False))
    assert info["kind"] == "layout" and info["draft"]["width"] == 1536
    # 已编译形态反解（1024x1024）→ 再编译应与原编译完全一致
    info = sniff(json.dumps(COMPILED_POSTER, ensure_ascii=False), 1024, 1024)
    assert info["draft"]["items"][0]["bbox"] == [128, 96, 896, 224]
    again = json.loads(compile_draft(info["draft"]))
    assert again == COMPILED_POSTER, "反解→重编译应还原"
    # 坏输入
    _expect(SniffError, sniff, "")
    _expect(SniffError, sniff, '{"caption":"a","caption":"b"}')     # 重复键
    _expect(SniffError, sniff, '{"caption": NaN}')                  # NaN
    _expect(SniffError, sniff, '{"foo": 1}')                        # 形态不识别
    _expect(SniffError, strict_loads, '{"a":1,"a":2}')
    # 纯文本散文 → 自动包 caption 壳
    info = sniff("电影感特摄动作插画，一场激烈战斗。无文字，无水印。")
    assert info == {"kind": "caption", "draft": {"caption": "电影感特摄动作插画，一场激烈战斗。无文字，无水印。"}}


# ---------- 节点 ----------

def test_editor_node():
    src = json.dumps(CUP, ensure_ascii=False)
    (out,) = MOSSEditor().run(src, 1024, 1024, "")
    assert json.loads(out) == CUP
    # edits 优先
    edited = json.loads(src); edited["summary"]["caption"] += " 补一句。"
    (out2,) = MOSSEditor().run(src, 1024, 1024, json.dumps(edited, ensure_ascii=False))
    assert json.loads(out2)["summary"]["caption"].endswith("补一句。")
    # 已编译输入走反解
    (out3,) = MOSSEditor().run(json.dumps(COMPILED_POSTER, ensure_ascii=False), 1024, 1024, "")
    assert json.loads(out3)["width"] == 1024
    # 纯文本输入 → caption 形态
    (out4,) = MOSSEditor().run("一段纯文本中文提示词。", 1024, 1024, "")
    assert json.loads(out4) == {"caption": "一段纯文本中文提示词。"}
    _expect(SniffError, MOSSEditor().run, '{"foo":1}', 1024, 1024, "")


def test_compiler_node():
    node = MOSSCompiler()
    # caption 形态：紧凑输出 + widget 兜底尺寸
    p, n, w, h, ct = node.run('{"caption":"你好"}', "neg", 832, 1216)
    assert (p, n, w, h) == ('{"caption":"你好"}', "neg", 832, 1216)
    assert ct == p, "clip_text 默认不含负向"
    # 勾选拼接负向
    _, _, _, _, ct2 = node.run('{"caption":"你好"}', "neg", 832, 1216, True)
    assert ct2 == '{"caption":"你好"}, neg'
    # layout 形态：编译 + 草稿尺寸
    p, n, w, h, ct = node.run(json.dumps(CUP, ensure_ascii=False), "neg", 512, 512)
    assert json.loads(p)["items"][0]["bbox"] == [-288, -592, 352, 32]
    assert (w, h) == (1536, 1024)
    assert ct == p
    # 已编译形态：校验后原样紧凑重排
    p, _, w, h, _ = node.run(json.dumps(COMPILED_POSTER), "neg", 640, 640)
    assert json.loads(p) == COMPILED_POSTER and (w, h) == (640, 640)


# ---------- 翻译：结构校验 / endpoint / 请求体 ----------

def test_check_translation():
    src = sniff(json.dumps(DRAFT_ZH, ensure_ascii=False))
    check_translation(src, json.loads(json.dumps(GOOD_EN)))  # 合格翻译直接通过

    import copy

    bad = copy.deepcopy(GOOD_EN); bad["items"][0]["bbox"] = [129, 384, 896, 896]
    _expect(TranslationError, check_translation, src, bad)          # bbox 被动
    bad = copy.deepcopy(GOOD_EN); bad["items"][1]["text"] = "EXPLORE"
    _expect(TranslationError, check_translation, src, bad)          # 可见文字被译
    bad = copy.deepcopy(GOOD_EN); bad["items"][0]["tags"] = ["photo"]
    _expect(TranslationError, check_translation, src, bad)          # 私自加字段
    bad = copy.deepcopy(GOOD_EN); bad["items"] = bad["items"][:1]
    _expect(TranslationError, check_translation, src, bad)          # 丢元素
    bad = copy.deepcopy(GOOD_EN); bad["width"] = 2048
    _expect(TranslationError, check_translation, src, bad)          # 宽高被动
    bad = copy.deepcopy(GOOD_EN); bad["summary"]["canvas"] = "collage"
    _expect(TranslationError, check_translation, src, bad)          # 枚举被动
    _expect(TranslationError, check_translation, src, {"caption": "x"})  # 形态漂移


def test_extract_json():
    fenced = "<think>思考过程…</think>\n```json\n" + json.dumps(GOOD_EN, ensure_ascii=False) + "\n```"
    assert extract_json(fenced)["width"] == 1024
    raw = "好的，结果是：\n" + json.dumps(GOOD_EN, ensure_ascii=False)
    assert extract_json(raw)["width"] == 1024
    _expect(TranslationError, extract_json, "完全不是 JSON")


def test_extract_json_repairs():
    """真实故障回归：模型把 caption 里的英文引号原样输出（未转义），以及结尾缺 ] }。"""
    # 1) caption 内文含未转义双引号（20" pipe 场景，对应线上 Expecting ',' delimiter）
    good = copy.deepcopy(GOOD_EN)
    good["items"][0]["caption"] = 'A flat blue mountain, the so-called "twin peaks" style, in the lower center.'
    valid = json.dumps(good, ensure_ascii=False, indent=1)
    hand_written = valid.replace('\\"twin peaks\\"', '"twin peaks"')  # 模拟模型手写时忘转义
    assert hand_written != valid, "测试样本没构造出未转义引号"
    out = extract_json(hand_written)
    assert '"twin peaks"' in out["items"][0]["caption"]

    # 2) 结尾缺最后的 ] }（模型提前收笔）
    valid = json.dumps(GOOD_EN, ensure_ascii=False, indent=1)
    idx = valid.rindex("]")
    cut = valid[:idx]  # 去掉 "]\n}"，以 items 最后一项的 } 结尾
    out = extract_json(cut)
    assert len(out["items"]) == len(GOOD_EN["items"])

    # 3) 只缺最外层 }
    cut2 = valid.rstrip()[:-1]
    out = extract_json(cut2)
    assert out["width"] == GOOD_EN["width"]

    # 4) 修复不了的真垃圾仍要报错（保留末尾片段提示）
    _expect(TranslationError, extract_json, "{{{{ 不是 JSON }}}}")


def test_resolve_endpoint_and_body():
    url, model = resolve_endpoint("deepseek", "", "", "off", {})
    assert url == "https://api.deepseek.com/v1" and model == "deepseek-chat"
    _, model = resolve_endpoint("deepseek", "", "", "high", {})  # 新阵容：不再切 reasoner，走 effort 参数
    assert model == "deepseek-chat"
    _, model = resolve_endpoint("deepseek", "", "deepseek-flash", "off", {})  # 显式尊重用户模型
    assert model == "deepseek-flash"
    url, model = resolve_endpoint("mimo", "", "", "off",
                                  {"mimo": {"base_url": "http://10.0.0.2:8000/v1", "model": "MiMo"}})
    assert url == "http://10.0.0.2:8000/v1" and model == "MiMo"
    _expect(TranslationError, resolve_endpoint, "custom", "", "", "off", {})

    b = build_body("m", 0.3, "high", "mimo", "")
    assert b["reasoning_effort"] == "high"
    assert b["max_tokens"] == 16384, "必须显式给足输出预算，防思考吃掉后正文截断"
    b = build_body("deepseek-flash", 0.3, "off", "deepseek", "")
    assert "reasoning_effort" not in b
    b = build_body("deepseek-flash", 0.3, "medium", "deepseek", "")
    assert b["reasoning_effort"] == "high", "medium 应映射到 DeepSeek 的 high 档"
    b = build_body("deepseek-flash", 0.3, "high", "deepseek", "")
    assert b["reasoning_effort"] == "max"
    b = build_body("m", 0.3, "off", "custom",
                   '{"temperature": 0.9, "chat_template_kwargs": {"thinking": true}}')
    assert b["temperature"] == 0.9 and b["chat_template_kwargs"]["thinking"] is True
    _expect(TranslationError, build_body, "m", 0.3, "off", "custom", "not json")


# ---------- 翻译：mock HTTP ----------

class MockHandler(http.server.BaseHTTPRequestHandler):
    queue = []
    hits = 0

    def do_POST(self):
        type(self).hits += 1
        content = type(self).queue.pop(0) if type(self).queue else "garbage not json"
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _start_mock():
    MockHandler.queue = []
    MockHandler.hits = 0
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _run_translator(base_url):
    return MOSSTranslator().run(
        json.dumps(DRAFT_ZH, ensure_ascii=False), "custom", base_url, "mock",
        "EMPTY", 0.3, "off", 30, "", "", None,
    )


def test_palette_meta_roundtrip():
    """palette/meta 模块：编译保留、翻译校验 hex 逐字不变、描述色名可译。"""
    src_draft = copy.deepcopy(DRAFT_ZH)
    src_draft["summary"]["palette"] = ["#F4EFE6", "#4A3327", "#1F3D2B"]
    src_draft["items"][0]["palette"] = ["#4A3327"]

    # 1) 草稿校验与编译都接受并保留 palette/meta
    validate_draft(src_draft)
    compiled = compile_draft(src_draft)
    c = json.loads(compiled)
    assert c["summary"]["palette"] == ["#F4EFE6", "#4A3327", "#1F3D2B"]
    assert c["summary"]["meta"] == src_draft["summary"]["meta"]
    assert c["items"][0]["palette"] == ["#4A3327"]
    assert "width" not in c and "height" not in c

    # 2) 翻译校验：hex 被改动 → 拒绝（大小写不敏感视为相同）
    from py.sniff import sniff
    src_info = sniff(json.dumps(src_draft, ensure_ascii=False), 1536, 1024)
    out_ok = copy.deepcopy(src_draft)
    out_ok["summary"]["caption"] = "A warm ceramic mug poster."
    check_translation(src_info, out_ok)  # 原样 hex → 通过
    out_case = copy.deepcopy(out_ok)
    out_case["summary"]["palette"] = ["#f4efe6", "#4A3327", "#1F3D2B"]  # 仅大小写不同 → 通过
    check_translation(src_info, out_case)
    out_bad = copy.deepcopy(out_ok)
    out_bad["summary"]["palette"] = ["#FFFFFF", "#4A3327", "#1F3D2B"]  # 颜色被改 → 拒绝
    _expect(TranslationError, check_translation, src_info, out_bad)
    out_bad2 = copy.deepcopy(out_ok)
    out_bad2["items"][0]["palette"] = ["#000000"]
    _expect(TranslationError, check_translation, src_info, out_bad2)

    # 3) 非 hex 的描述色名允许翻译
    src2 = copy.deepcopy(src_draft)
    src2["summary"]["palette"] = ["warm beige", "dark green"]
    info2 = sniff(json.dumps(src2, ensure_ascii=False), 1536, 1024)
    out2 = copy.deepcopy(src2)
    out2["summary"]["palette"] = ["暖米色", "深绿色"]
    check_translation(info2, out2)


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


def test_http_first_try_success():
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(GOOD_EN, ensure_ascii=False)]
        (out,) = _run_translator(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        assert json.loads(out) == GOOD_EN and MockHandler.hits == 1
    finally:
        srv.shutdown()


def test_http_retry_success():
    srv = _start_mock()
    try:
        MockHandler.queue = [
            "抱歉我无法输出 JSON",  # 第一次失败
            "```json\n" + json.dumps(GOOD_EN, ensure_ascii=False) + "\n```",  # 带围栏的合格输出
        ]
        (out,) = _run_translator(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        assert json.loads(out) == GOOD_EN and MockHandler.hits == 2, "应带错重试一次后成功"
    finally:
        srv.shutdown()


def test_http_fail_after_retries():
    srv = _start_mock()
    try:
        _expect(TranslationError, _run_translator, f"http://127.0.0.1:{srv.server_address[1]}/v1")
        assert MockHandler.hits == 3, "1 次首发 + 2 次重试"
    finally:
        srv.shutdown()


def test_http_truncation_detected():
    class TruncHandler(MockHandler):
        def do_POST(self):
            body = json.dumps({"choices": [{"message": {"content": "半截输出……"}, "finish_reason": "length"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TruncHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        try:
            _run_translator(f"http://127.0.0.1:{srv.server_address[1]}/v1")
            assert False, "截断应触发明确报错"
        except TranslationError as e:
            assert "截断" in str(e)
    finally:
        srv.shutdown()


# ---------- 编辑器 AI 拆解 ----------

LAYOUT_ZH = {
    "width": 1024, "height": 1024,
    "bbox_coord_space": "abs", "bbox_format": "xyxy",
    "summary": {"meta": ["特摄"], "canvas": "single", "caption": "废弃工厂内的战斗，绿色逆光。"},
    "items": [
        {"label": "character", "caption": "成年女性战队战士向前突进，挥动能量剑。", "bbox": [256, 384, 704, 896]},
        {"label": "character", "caption": "酸性绿色黏液怪人从右上压迫。", "bbox": [576, 128, 960, 512]},
    ],
}


def _run_editor_ai(base_url, draft_widget="", ai=True):
    return MOSSEditor().run(
        "一段特摄战斗的纯文本提示词。", 1024, 1024, draft_widget,
        ai_draft=ai, provider="custom", base_url=base_url, model="mock",
        api_key="EMPTY", temperature=0.3, thinking="off", timeout=30, ai_prompt="",
    )


def test_ai_draft_success_and_writeback():
    srv = _start_mock()
    try:
        MockHandler.queue = ["```json\n" + json.dumps(LAYOUT_ZH, ensure_ascii=False) + "\n```"]
        ret = _run_editor_ai(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        assert isinstance(ret, dict) and "ui" in ret and "result" in ret
        assert json.loads(ret["result"][0]) == LAYOUT_ZH
        assert json.loads(ret["ui"]["mps_draft"]) == LAYOUT_ZH, "拆解结果必须随 ui 写回前端"
        assert MockHandler.hits == 1
    finally:
        srv.shutdown()


def test_ai_draft_retry_on_invalid_bbox():
    import copy

    srv = _start_mock()
    try:
        bad = copy.deepcopy(LAYOUT_ZH)
        bad["items"][0]["bbox"] = [0, 0, 9999, 500]  # 越界 → 校验拒绝
        MockHandler.queue = [json.dumps(bad, ensure_ascii=False), json.dumps(LAYOUT_ZH, ensure_ascii=False)]
        ret = _run_editor_ai(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        assert json.loads(ret["result"][0]) == LAYOUT_ZH and MockHandler.hits == 2
    finally:
        srv.shutdown()


def test_ai_draft_disabled_and_layout_passthrough():
    # ai_draft 关闭：纯文本仍按 caption 走
    (out,) = MOSSEditor().run("纯文本。", 1024, 1024, "", ai_draft=False)
    assert json.loads(out) == {"caption": "纯文本。"}
    # 输入已是 layout：ai_draft 不触发 LLM、不产生 ui
    ret = MOSSEditor().run(json.dumps(CUP, ensure_ascii=False), 1024, 1024, "", ai_draft=True,
                           provider="custom", base_url="http://127.0.0.1:1/v1", model="x",
                           api_key="EMPTY", temperature=0.3, thinking="off", timeout=5, ai_prompt="")
    assert isinstance(ret, tuple) and json.loads(ret[0])["width"] == 1536


TESTS = [
    test_compile_official_examples,
    test_validate_rejects,
    test_sniff_forms_and_roundtrip,
    test_editor_node,
    test_compiler_node,
    test_check_translation,
    test_extract_json,
    test_extract_json_repairs,
    test_palette_meta_roundtrip,
    test_agent_md_append_injection,
    test_agent_md_widget_selection,
    test_resolve_endpoint_and_body,
    test_http_first_try_success,
    test_http_retry_success,
    test_http_fail_after_retries,
    test_http_truncation_detected,
    test_ai_draft_success_and_writeback,
    test_ai_draft_retry_on_invalid_bbox,
    test_ai_draft_disabled_and_layout_passthrough,
]


def main():
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception:
            failed += 1
            print(f"[FAIL] {fn.__name__}")
            traceback.print_exc()
    print("=" * 46)
    print("ALL TESTS PASSED" if not failed else f"{failed} TEST(S) FAILED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
