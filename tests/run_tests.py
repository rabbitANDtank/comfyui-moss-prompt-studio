"""离线测试（不依赖 ComfyUI 运行环境）。

运行:  python tests/run_tests.py
覆盖: 官方口径编译(含官方文档给出的期望坐标) / 三形态识别与反解往返 /
      节点行为 / 翻译结构完整性校验 / endpoint 与请求体组装 /
      本地 mock 服务下的翻译 HTTP 成功-重试-失败路径。
"""
import copy
import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import traceback

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from py.compile_prompt import DraftError, compile_draft, validate_compiled, validate_draft  # noqa: E402
from py.sniff import SniffError, sniff, strict_loads  # noqa: E402
from py.translate import (  # noqa: E402
    AGENT_DEFAULT,
    DEFAULT_AI_DRAFT_PROMPT,
    MAX_AGENT_FILE_BYTES,
    PermanentError,
    TranslationError,
    build_body,
    chat,
    check_translation,
    config_key_allowed,
    extract_json,
    prose_to_layout,
    resolve_endpoint,
    resolve_editor_text,
    sanitize_secret,
    source_signature,
)
from py.vision import (  # noqa: E402
    build_style_instructions,
    image_detail_for,
    normalize_vision_draft,
    reverse_image_to_prompt,
)
from py.vision_probe import (  # noqa: E402
    _post_once,
    evaluate_origin,
    run_probe,
)
from nodes.compiler import MOSSCompiler  # noqa: E402
from nodes.editor import MOSSEditor  # noqa: E402
from nodes.image_reverse import MOSSImageReverse, resolve_reverse_options  # noqa: E402
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
    badc = copy.deepcopy(COMPILED_POSTER); badc["items"][0]["bbox"] = [-416.0, -384, -288, 384]
    _expect(DraftError, validate_compiled, badc)                              # 编译态 bbox 必须是整数
    badc = copy.deepcopy(COMPILED_POSTER); badc["items"][0]["bbox"] = [-288, -384, -416, 384]
    _expect(DraftError, validate_compiled, badc)                              # y0 必须小于 y1
    badc = copy.deepcopy(COMPILED_POSTER); badc["items"][0]["bbox"] = [-416, 384, -288, -384]
    _expect(DraftError, validate_compiled, badc)                              # x0 必须小于 x1


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
    out, w, h = MOSSEditor().run(src, 1024, 1024, "")
    assert json.loads(out) == CUP and (w, h) == (1536, 1024), "layout 宽高输出跟草稿走"
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


# ---------- 翻译：结构校验 / endpoint / 请求体 ----------

def test_check_translation():
    src = sniff(json.dumps(DRAFT_ZH, ensure_ascii=False))
    check_translation(src, json.loads(json.dumps(GOOD_EN)))  # 合格翻译直接通过

    import copy

    bad = copy.deepcopy(GOOD_EN); bad["items"][0]["bbox"] = [129, 384, 896, 896]
    _expect(TranslationError, check_translation, src, bad)          # bbox 被动
    bad = copy.deepcopy(GOOD_EN); bad["items"][1]["text"] = "EXPLORE"
    _expect(TranslationError, check_translation, src, bad)          # 可见文字被译
    bad = copy.deepcopy(GOOD_EN); bad["summary"]["meta"] = [123]
    _expect(TranslationError, check_translation, src, bad)          # 数组元素类型非法
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

    # MiMo：thinking.type 二值开关；官方 v2.6 默认开启，off 必须显式 disabled
    b = build_body("m", 0.3, "high", "mimo", "")
    assert b["thinking"] == {"type": "enabled"} and "reasoning_effort" not in b
    assert b["max_tokens"] == 16384, "必须显式给足输出预算，防思考吃掉后正文截断"
    assert b["max_completion_tokens"] == 16384, "MiMo 用它限制思考+正文的总长度"
    b = build_body("m", 0.3, "off", "mimo", "")
    assert b["thinking"] == {"type": "disabled"}
    # DeepSeek：off 必须显式 none（不传时默认仍会思考）
    b = build_body("deepseek-flash", 0.3, "off", "deepseek", "")
    assert b["reasoning_effort"] == "none"
    b = build_body("deepseek-flash", 0.3, "medium", "deepseek", "")
    assert b["reasoning_effort"] == "high", "medium 应映射到 DeepSeek 的 high 档"
    b = build_body("deepseek-flash", 0.3, "high", "deepseek", "")
    assert b["reasoning_effort"] == "max"
    # custom：off 不额外发参数；非 off 发 reasoning_effort
    b = build_body("m", 0.3, "off", "custom", "")
    assert "reasoning_effort" not in b and "thinking" not in b
    b = build_body("m", 0.3, "high", "custom", "")
    assert b["reasoning_effort"] == "high"
    b = build_body("m", 0.3, "off", "custom",
                   '{"temperature": 0.9, "chat_template_kwargs": {"thinking": true}}')
    assert b["temperature"] == 0.9 and b["chat_template_kwargs"]["thinking"] is True
    # extra_body 最后合并，可覆盖 MiMo 的 thinking / max_completion_tokens
    b = build_body("m", 0.3, "off", "mimo",
                   '{"thinking": {"type": "enabled"}, "max_completion_tokens": 4096}')
    assert b["thinking"] == {"type": "enabled"} and b["max_completion_tokens"] == 4096
    _expect(TranslationError, build_body, "m", 0.3, "off", "custom", "not json")


# ---------- 翻译：mock HTTP ----------

class MockHandler(http.server.BaseHTTPRequestHandler):
    queue = []
    hits = 0
    status = 200

    def do_POST(self):
        type(self).hits += 1
        content = type(self).queue.pop(0) if type(self).queue else "garbage not json"
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _start_mock():
    MockHandler.queue = []
    MockHandler.hits = 0
    MockHandler.status = 200
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class SequenceHandler(http.server.BaseHTTPRequestHandler):
    """按顺序返回 (status, payload)，用于探测的“视觉失败→文本成功”分支。"""

    responses = []
    hits = 0

    def do_POST(self):
        type(self).hits += 1
        status, payload = (type(self).responses.pop(0)
                           if type(self).responses else
                           (500, {"error": {"message": "no response"}}))
        body = (payload if isinstance(payload, str)
                else json.dumps(payload, ensure_ascii=False)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _start_sequence(responses):
    SequenceHandler.responses = list(responses)
    SequenceHandler.hits = 0
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SequenceHandler)
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


def test_http_permanent_not_retried():
    """401/403/400 等永久错误不应重试 3 次。"""
    srv = _start_mock()
    try:
        MockHandler.status = 401
        MockHandler.queue = ["unauthorized"]
        _expect(PermanentError, _run_translator, f"http://127.0.0.1:{srv.server_address[1]}/v1")
        assert MockHandler.hits == 1, "永久错误只应请求一次"
    finally:
        MockHandler.status = 200
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
        assert json.loads(ret["result"][0]) == LAYOUT_ZH and ret["result"][1:] == (1024, 1024)
        assert json.loads(ret["ui"]["mps_draft"]) == LAYOUT_ZH, "拆解结果必须随 ui 写回前端"
        assert MockHandler.hits == 1
    finally:
        srv.shutdown()


def test_ai_draft_repairs_bad_bbox_and_extra_fields():
    """AI 拆解专用修复：越界框夹取、删除 schema 外字段，不再为一处小错整轮重试。"""
    import copy

    srv = _start_mock()
    try:
        bad = copy.deepcopy(LAYOUT_ZH)
        bad["items"][0]["bbox"] = [0, 0, 9999, 500]  # 越界 → 本地夹取
        bad["items"][0]["description"] = "extra"     # 元素级多余字段 → 删除
        bad["text"] = "oops"                         # 顶层多余字段 → 删除
        MockHandler.queue = [json.dumps(bad, ensure_ascii=False)]
        ret = _run_editor_ai(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        out = json.loads(ret["result"][0])
        assert MockHandler.hits == 1, "可本地修复的偏差不应触发重试"
        assert out["items"][0]["bbox"] == [0, 0, 1024, 500]
        assert "text" not in out and "description" not in out["items"][0]
    finally:
        srv.shutdown()


def test_ai_draft_repairs_missing_summary():
    """模型把 caption/meta/canvas 放到顶层、summary 整个缺失 → 本地重建 summary。"""
    import copy

    srv = _start_mock()
    try:
        bad = copy.deepcopy(LAYOUT_ZH)
        s = bad.pop("summary")
        bad["caption"] = s["caption"]
        for k in ("meta", "canvas", "palette"):
            if k in s:
                bad[k] = s[k]
        MockHandler.queue = [json.dumps(bad, ensure_ascii=False)]
        ret = _run_editor_ai(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        out = json.loads(ret["result"][0])
        assert MockHandler.hits == 1, "summary 缺失应本地重建，不重试"
        assert out["summary"]["caption"] == s["caption"]
        for k in ("meta", "canvas", "palette"):
            if k in s:
                assert out["summary"][k] == s[k]
    finally:
        srv.shutdown()


def test_ai_draft_falls_back_to_prose_for_empty_caption():
    """summary 在、但 caption 为空 → 用原始散文兜底，不重试。"""
    import copy

    srv = _start_mock()
    try:
        bad = copy.deepcopy(LAYOUT_ZH)
        bad["summary"]["caption"] = ""
        MockHandler.queue = [json.dumps(bad, ensure_ascii=False)]
        ret = _run_editor_ai(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        out = json.loads(ret["result"][0])
        assert MockHandler.hits == 1
        assert out["summary"]["caption"] == "一段特摄战斗的纯文本提示词。"
    finally:
        srv.shutdown()


def test_ai_draft_retry_on_unrepairable():
    """真正修不好的错误（items 为空）仍要走重试。"""
    import copy

    srv = _start_mock()
    try:
        bad = copy.deepcopy(LAYOUT_ZH)
        bad["items"] = []  # 空 items 无法本地修复 → 校验拒绝
        MockHandler.queue = [json.dumps(bad, ensure_ascii=False), json.dumps(LAYOUT_ZH, ensure_ascii=False)]
        ret = _run_editor_ai(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        assert json.loads(ret["result"][0]) == LAYOUT_ZH and MockHandler.hits == 2
    finally:
        srv.shutdown()


def test_ai_draft_disabled_and_layout_passthrough():
    # ai_draft 关闭：纯文本仍按 caption 走
    out, _, _ = MOSSEditor().run("纯文本。", 1024, 1024, "", ai_draft=False)
    assert json.loads(out) == {"caption": "纯文本。"}
    # 输入已是 layout：ai_draft 不触发 LLM、不产生 ui
    ret = MOSSEditor().run(json.dumps(CUP, ensure_ascii=False), 1024, 1024, "", ai_draft=True,
                           provider="custom", base_url="http://127.0.0.1:1/v1", model="x",
                           api_key="EMPTY", temperature=0.3, thinking="off", timeout=5, ai_prompt="")
    assert isinstance(ret, tuple) and json.loads(ret[0])["width"] == 1536 and ret[1:] == (1536, 1024)


# ---------- minimax / 视觉反推 / 连接测试 ----------

def test_minimax_endpoint_and_body():
    url, model = resolve_endpoint("minimax", "", "", "off", {})
    assert url == "https://api.minimax.cn/v1" and model == "MiniMax-M3.1-Flash"
    url, model = resolve_endpoint("minimax", "https://api.minimax.io/v1", "m", "off", {})
    assert url == "https://api.minimax.io/v1" and model == "m"

    b = build_body("MiniMax-M3.1-Flash", 0.3, "off", "minimax", "")
    assert "max_tokens" not in b and b["max_completion_tokens"] == 65536
    assert b["reasoning_effort"] == "low" and "thinking" not in b
    b = build_body("MiniMax-M3.1-Flash", 0.3, "high", "minimax", "")
    assert b["reasoning_effort"] == "max", "high 应映射到 MiniMax 的 max 档"
    b = build_body("MiniMax-M3.1-Flash", 0.3, "off", "minimax",
                   '{"max_completion_tokens": 2048}')
    assert b["max_completion_tokens"] == 2048, "extra_body 最后合并"


def test_minimax_business_error():
    class MiniMaxErrHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            payload = {"base_resp": {"status_code": 2022, "status_msg": "model is not exist"},
                       "choices": []}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MiniMaxErrHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base_url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        try:
            chat(base_url, "k", {"model": "MiniMax-M3.1-Flash"}, 5)
        except PermanentError as e:
            assert "2022" in str(e) and "not exist" in str(e)
        else:
            raise AssertionError("MiniMax base_resp 业务错误应抛 PermanentError")
    finally:
        srv.shutdown()


LAYOUT_1000 = {
    "width": 1000, "height": 1000,
    "bbox_coord_space": "abs", "bbox_format": "xyxy",
    "summary": {"caption": "一张中文海报。", "meta": ["poster"], "canvas": "single"},
    "items": [
        {"label": "person", "caption": "站立人物。", "bbox": [100, 100, 400, 900]},
        {"label": "obj", "caption": "杯子。", "bbox": [500, 400, 800, 800]},
        {"label": "text", "caption": "标题排版。", "bbox": [100, 50, 900, 120]},
        {"label": "thing", "caption": "多余元素。", "bbox": [10, 10, 20, 20]},
    ],
}


def test_vision_normalize():
    out = normalize_vision_draft(copy.deepcopy(LAYOUT_1000), 1024, 1024, 3)
    assert out["width"] == 1024 and out["height"] == 1024
    assert len(out["items"]) == 3, "max_items 应截断"
    assert out["items"][0]["label"] == "character", "person 应映射为 character"
    assert out["items"][1]["label"] == "object", "obj 应映射为 object"
    assert out["items"][2]["label"] == "object" and "text" not in out["items"][2], \
        "缺 text 的 text 元素应降级为 object"
    b = out["items"][0]["bbox"]
    assert b[0] < b[2] and b[1] < b[3] and b[2] <= 1024 and b[3] <= 1024
    # items 为空时补一个全画布 object
    empty = {"width": 1000, "height": 1000, "bbox_coord_space": "abs",
             "bbox_format": "xyxy", "summary": {"caption": "纯色纹理。"}, "items": []}
    out2 = normalize_vision_draft(empty, 1024, 1024, 6)
    assert len(out2["items"]) == 1 and out2["items"][0]["bbox"] == [0, 0, 1024, 1024]


def test_vision_reverse_mock():
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_1000, ensure_ascii=False)]
        out = reverse_image_to_prompt(
            "Zm9v", "image/jpeg", width=1024, height=1024, max_items=6,
            base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
            api_key="EMPTY", body=build_body("mock", 0.3, "off", "custom", ""),
            timeout=10,
        )
        assert out["width"] == 1024 and out["height"] == 1024
        assert out["items"][0]["label"] == "character"
        assert out["items"][1]["label"] == "object"
        assert MockHandler.hits == 1
    finally:
        srv.shutdown()


def test_vision_reverse_retry():
    srv = _start_mock()
    try:
        MockHandler.queue = ["不是 JSON", json.dumps(LAYOUT_1000, ensure_ascii=False)]
        out = reverse_image_to_prompt(
            "Zm9v", "image/jpeg", width=1024, height=1024, max_items=6,
            base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
            api_key="EMPTY", body=build_body("mock", 0.3, "off", "custom", ""),
            timeout=10,
        )
        assert out["width"] == 1024 and MockHandler.hits == 2
    finally:
        srv.shutdown()


def test_probe_success():
    srv = _start_sequence([(200, {
        "model": "mock",
        "choices": [{"message": {"content": "Red"}, "finish_reason": "stop"}],
    })])
    try:
        d = run_probe({
            "provider": "custom", "base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1",
            "model": "mock", "api_key": "EMPTY", "thinking": "off", "timeout": 10,
        })
        assert d["ok"] is True and d["vision_ok"] is True and d["text_ok"] is True
        assert d["vision_confirmed"] is True
        assert d["endpoint"].endswith("/v1/chat/completions")
        assert d["api_key_masked"] == ""
    finally:
        srv.shutdown()


def test_probe_vision_unsupported():
    srv = _start_sequence([
        (400, {"error": {"message": "image input not supported"}}),
        (200, {"choices": [{"message": {"content": "OK"}}]}),
    ])
    try:
        d = run_probe({
            "provider": "custom", "base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1",
            "model": "mock", "api_key": "EMPTY", "thinking": "off", "timeout": 10,
        })
        assert d["ok"] is False and d["error_type"] == "vision_unsupported"
        assert d["text_ok"] is True and d["vision_ok"] is False
        assert SequenceHandler.hits == 2
    finally:
        srv.shutdown()


def test_probe_auth_failed():
    srv = _start_sequence([(401, {"error": {"message": "invalid key"}})])
    try:
        d = run_probe({
            "provider": "custom", "base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1",
            "model": "mock", "api_key": "sk-test", "thinking": "off", "timeout": 10,
        })
        assert d["ok"] is False and d["error_type"] == "auth_failed" and d["status"] == 401
        assert SequenceHandler.hits == 1
    finally:
        srv.shutdown()


def test_probe_connection_refused():
    d = run_probe({
        "provider": "custom", "base_url": "http://127.0.0.1:1/v1",
        "model": "mock", "api_key": "EMPTY", "thinking": "off", "timeout": 5,
    })
    assert d["ok"] is False and d["error_type"] in ("connection_refused", "network_error")


def test_image_reverse_is_changed():
    a = MOSSImageReverse.IS_CHANGED(provider="mimo", model="m", system_prompt="x")
    b = MOSSImageReverse.IS_CHANGED(provider="mimo", model="m", system_prompt="x")
    c = MOSSImageReverse.IS_CHANGED(provider="deepseek", model="m", system_prompt="x")
    assert a == b and a != c


def test_image_reverse_node_run():
    """IMAGE → 视觉 mock → 三路输出 + ui 写回载荷（不依赖 ComfyUI/torch）。"""
    import numpy as np

    class FakeImg:
        def __init__(self, arr):
            self.arr = arr
            self.shape = arr.shape

        def detach(self): return self
        def float(self): return self
        def cpu(self): return self
        def numpy(self): return self.arr

    class FakeBatch:
        def __init__(self, arr):
            self.arr = arr
            self.shape = (1,) + arr.shape

        def __getitem__(self, i): return FakeImg(self.arr)
        def __len__(self): return 1

    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_1000, ensure_ascii=False)]
        out = MOSSImageReverse().run(
            FakeBatch(np.zeros((64, 64, 3), dtype="float32")),
            "custom", f"http://127.0.0.1:{srv.server_address[1]}/v1", "mock",
            "EMPTY", 0.3, "off", 10, 0, 0, 6, "", "", AGENT_DEFAULT,
            enabled=True,
        )
        assert isinstance(out, dict) and "ui" in out and "result" in out
        prose, draft, canvas = out["result"]
        assert prose == "一张中文海报。" and draft == canvas
        assert json.loads(draft)["width"] == 64 and json.loads(draft)["height"] == 64
        payload = json.loads(out["ui"]["mps_reverse"])
        assert payload["width"] == 64 and payload["items"] >= 1 and payload["sig"]
        assert "draft" in payload and "prose" in payload
    finally:
        srv.shutdown()


RAW_STYLE = {
    "width": 1000, "height": 1000,
    "bbox_coord_space": "abs", "bbox_format": "xyxy",
    "summary": {"caption": "一张海报。", "meta": ["poster"], "palette": ["#111111"]},
    "items": [
        {"label": "object", "caption": "主体。", "bbox": [100, 100, 800, 800],
         "tags": ["main"], "palette": ["#222222"]},
        {"label": "text", "caption": "标题。", "text": "HELLO", "bbox": [100, 50, 900, 150]},
    ],
}


def test_image_reverse_node_concise():
    """concise 模式整节点：无文字、无 meta/palette/tags、max_items=4。"""
    import numpy as np

    class FakeImg:
        def __init__(self, arr):
            self.arr = arr
            self.shape = arr.shape

        def detach(self): return self
        def float(self): return self
        def cpu(self): return self
        def numpy(self): return self.arr

    class FakeBatch:
        def __init__(self, arr):
            self.arr = arr
            self.shape = (1,) + arr.shape

        def __getitem__(self, i): return FakeImg(self.arr)
        def __len__(self): return 1

    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(RAW_STYLE, ensure_ascii=False)]
        out = MOSSImageReverse().run(
            FakeBatch(np.zeros((64, 64, 3), dtype="float32")),
            "custom", f"http://127.0.0.1:{srv.server_address[1]}/v1", "mock",
            "EMPTY", 0.3, "off", 10, 0, 0, 6, "", "", AGENT_DEFAULT, None, "concise",
            enabled=True,
        )
        _prose, draft, _canvas = out["result"]
        d = json.loads(draft)
        payload = json.loads(out["ui"]["mps_reverse"])
        assert payload["mode"] == "concise" and payload["max_items"] == 4
        assert payload["texts"] == 0
        assert "meta" not in d["summary"] and "palette" not in d["summary"]
        assert all(it["label"] != "text" for it in d["items"])
        assert all("tags" not in it and "palette" not in it for it in d["items"])
    finally:
        srv.shutdown()


def test_reverse_mode_presets():
    c = resolve_reverse_options("concise", provider="mimo")
    assert c["max_items"] == 4 and c["include_text"] is False
    assert c["caption_detail"] == "low" and c["tag_detail"] == "none"
    assert c["max_tokens"] == 8192 and c["image_max_side"] == 1024
    assert c["image_detail"] == "medium_low"
    assert c["image_detail_api"] is None, "MiMo 不发 detail 字段"
    assert c["image_quality"] == 78 and abs(c["temperature"] - 0.1) < 1e-9
    assert c["thinking"] == "off", "非 manual 模式思考压到最低"

    s = resolve_reverse_options("standard", provider="deepseek")
    assert s["max_items"] == 6 and s["include_text"] is True
    assert s["caption_detail"] == "medium_low" and s["tag_detail"] == "full"
    assert s["max_tokens"] == 65536 and s["image_max_side"] == 1536
    assert s["image_detail"] == "high" and s["image_detail_api"] == "high"
    assert s["image_quality"] == 92 and abs(s["temperature"] - 0.2) < 1e-9
    assert s["thinking"] == "off"

    d = resolve_reverse_options("detailed", provider="minimax")
    assert d["max_items"] == 10 and d["caption_detail"] == "medium_high"
    assert d["max_tokens"] == 65536 and d["image_max_side"] == 1728
    assert d["image_detail"] == "high" and d["image_detail_api"] == "high"
    assert abs(d["temperature"] - 1.0) < 1e-9, "MiniMax temperature 固定 1"
    assert d["thinking"] == "off", "MiniMax off 会映射最低档 low"

    m = resolve_reverse_options(
        "manual", provider="mimo", max_items=99, include_text=False,
        caption_detail="ultra", tag_detail="basic", max_tokens=500000,
        image_detail="ultra", image_max_side="2000", thinking="high",
        temperature=0.7,
    )
    assert m["max_items"] == 30 and m["include_text"] is False
    assert m["caption_detail"] == "ultra" and m["tag_detail"] == "basic"
    assert m["max_tokens"] == 131072, "MiMo 手动 token 上限 131072"
    assert m["image_detail"] == "ultra" and m["image_quality"] == 96
    assert m["image_max_side"] == 2000 and abs(m["temperature"] - 0.7) < 1e-9
    assert m["thinking"] == "high", "manual 保留用户思考强度"
    assert resolve_reverse_options(
        "manual", provider="mimo", thinking="invalid"
    )["thinking"] == "off"

    assert resolve_reverse_options(
        "manual", provider="deepseek", max_tokens=500000
    )["max_tokens"] == 393216
    assert resolve_reverse_options(
        "manual", provider="minimax", max_tokens=500000
    )["max_tokens"] == 500000
    assert resolve_reverse_options(
        "manual", provider="mimo", caption_detail="short"
    )["caption_detail"] == "low", "旧 short 别名"
    assert resolve_reverse_options(
        "manual", provider="mimo", caption_detail="long"
    )["caption_detail"] == "medium_high", "旧 long 别名"
    assert resolve_reverse_options("unknown")["max_items"] == 6, "未知模式回退 standard"
    assert resolve_reverse_options(6)["mode"] == "standard", "旧 max_items=6 误入 reverse_mode 时回退"

    assert resolve_reverse_options("concise", provider="minimax")["image_detail_api"] == "default"
    assert resolve_reverse_options("concise", provider="deepseek")["image_detail_api"] == "high"
    assert resolve_reverse_options("concise", provider="mimo")["image_detail_api"] is None


def test_vision_style_and_filters():
    low = build_style_instructions(4, False, "low", "none")
    assert "at most 4" in low
    assert "Do NOT create text items" in low
    assert "one short sentence" in low
    assert "Do not output summary.meta" in low
    ultra = build_style_instructions(10, True, "ultra", "full")
    assert "at most 10" in ultra and "3-5 hex colors" in ultra
    assert "Create text items" in ultra and "very detailed" in ultra

    out = normalize_vision_draft(
        copy.deepcopy(RAW_STYLE), 1024, 1024, 6,
        include_text=False, tag_detail="none",
    )
    assert all(it["label"] != "text" for it in out["items"])
    assert "meta" not in out["summary"] and "palette" not in out["summary"]
    assert all("tags" not in it and "palette" not in it for it in out["items"])

    out2 = normalize_vision_draft(
        copy.deepcopy(RAW_STYLE), 1024, 1024, 6,
        include_text=True, tag_detail="basic",
    )
    assert "meta" in out2["summary"] and "palette" not in out2["summary"]
    assert all("tags" not in it and "palette" not in it for it in out2["items"])
    assert any(it["label"] == "text" for it in out2["items"])


class RecordingHandler(http.server.BaseHTTPRequestHandler):
    """记录每次请求的 headers/JSON body，并按队列返回 (status, payload)。"""

    responses = []
    hits = 0
    bodies = []
    headers_seen = []

    def do_POST(self):
        type(self).hits += 1
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            type(self).bodies.append(json.loads(raw.decode("utf-8")))
        except Exception:
            type(self).bodies.append({})
        type(self).headers_seen.append({k.lower(): v for k, v in self.headers.items()})
        status, payload = (type(self).responses.pop(0)
                           if type(self).responses else
                           (500, {"error": {"message": "no response"}}))
        body = (payload if isinstance(payload, str)
                else json.dumps(payload, ensure_ascii=False)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _start_recording(responses):
    RecordingHandler.responses = list(responses)
    RecordingHandler.hits = 0
    RecordingHandler.bodies = []
    RecordingHandler.headers_seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class EchoKeyHandler(http.server.BaseHTTPRequestHandler):
    """401/500 响应体回显提交的 key，用于验证日志/异常脱敏。"""

    key = ""
    status = 401

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        payload = {"error": {"message": (
            f"Incorrect API key provided: {type(self).key}. "
            f"Authorization: Bearer {type(self).key}")}}
        body = json.dumps(payload).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _start_echo_key(status, key):
    EchoKeyHandler.key = key
    EchoKeyHandler.status = status
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EchoKeyHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        import time
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        time.sleep(2.0)
        body = b"{}"
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # 客户端已超时断开

    def log_message(self, *a):
        pass


def _temp_config(cfg: dict):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(cfg, f)
    f.close()
    return f.name


def _with_config(cfg, fn):
    old = os.environ.get("COMFY_MOSS_STUDIO_CONFIG")
    path = _temp_config(cfg)
    os.environ["COMFY_MOSS_STUDIO_CONFIG"] = path
    try:
        return fn()
    finally:
        os.environ.pop("COMFY_MOSS_STUDIO_CONFIG", None)
        if old is not None:
            os.environ["COMFY_MOSS_STUDIO_CONFIG"] = old
        os.unlink(path)


def test_sanitize_secret_redacts_key_and_urls():
    key = "sk-live-SECRET-1234567890"
    text = (f"Incorrect API key provided: {key} | Authorization: Bearer {key} | "
            f"https://user:{key}@host.example/v1?api_key={key}")
    out = sanitize_secret(text, key)
    assert key not in out
    assert "user:" not in out
    assert "api_key=" not in out
    # 即使调用方没传 key，也要按 Bearer/URL 规则兜底
    out2 = sanitize_secret("Authorization: Bearer abcdefghijklmnop https://u:p@x/y?token=z")
    assert "abcdefghijklmnop" not in out2 and "u:p@" not in out2
    assert "?***" in sanitize_secret("see https://x.example/v1?key=secret", "")


def test_chat_error_echoing_key_is_redacted():
    key = "sk-echo-SECRET-abcdef123456"
    srv = _start_echo_key(401, key)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            try:
                chat(f"http://127.0.0.1:{srv.server_address[1]}/v1", key,
                     {"model": "m", "stream": False}, 5)
                raise AssertionError("401 应抛 PermanentError")
            except PermanentError as e:
                msg = str(e)
        assert key not in msg, "异常文本不得回显 key"
        assert key not in buf.getvalue(), "控制台日志不得回显 key"
        assert "***" in msg
    finally:
        srv.shutdown()


def test_translator_final_error_redacts_key():
    key = "sk-retry-SECRET-abcdef123456"
    srv = _start_echo_key(500, key)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            try:
                MOSSTranslator().run(
                    json.dumps(DRAFT_ZH, ensure_ascii=False), "custom",
                    f"http://127.0.0.1:{srv.server_address[1]}/v1", "mock",
                    key, 0.3, "off", 10, "", "", None,
                )
                raise AssertionError("持续 500 应最终抛 TranslationError")
            except TranslationError as e:
                msg = str(e)
        assert key not in msg, "最终异常不得回显 key"
        assert key not in buf.getvalue(), "重试日志不得回显 key"
    finally:
        srv.shutdown()


def test_origin_evaluation():
    assert evaluate_origin({"Sec-Fetch-Site": "same-origin"}, "127.0.0.1:8000") == "same-origin"
    assert evaluate_origin({"Sec-Fetch-Site": "cross-site"}, "127.0.0.1:8000") == "cross-origin"
    assert evaluate_origin({"Sec-Fetch-Site": "same-site"}, "127.0.0.1:8000") == "cross-origin"
    assert evaluate_origin({"Origin": "http://127.0.0.1:8000"},
                           "127.0.0.1:8000") == "same-origin"
    assert evaluate_origin({"Origin": "https://evil.example"},
                           "127.0.0.1:8000") == "cross-origin"
    assert evaluate_origin({}, "127.0.0.1:8000") == "unknown"
    # Origin 与 Sec-Fetch-Site 同时出现时，浏览器声明的 cross-site 优先拒绝
    assert evaluate_origin({"Origin": "http://127.0.0.1:8000",
                            "Sec-Fetch-Site": "cross-site"},
                           "127.0.0.1:8000") == "cross-origin"


def test_probe_never_sends_config_key_to_foreign_base_url():
    key = "sk-config-SECRET-abcdef123456"
    srv = _start_recording([(200, {
        "model": "mock", "choices": [{"message": {"content": "Red"}}],
    })])
    local = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    try:
        # 1) 调用者 base_url 与 config 中该 provider 的地址不同 → 不得回退配置 key
        d = _with_config(
            {"custom": {"base_url": "https://configured.invalid/v1", "api_key": key}},
            lambda: run_probe({
                "provider": "custom", "base_url": local, "model": "mock",
                "api_key": "EMPTY", "thinking": "off", "timeout": 10,
            }),
        )
        assert d["auth_source"] == "none"
        assert "authorization" not in RecordingHandler.headers_seen[-1], \
            "配置 key 绝不能发到调用者指定的地址"

        # 2) base_url 与 config 一致，但非浏览器同源（allow_config_key=False）→ 仍不回退
        d = _with_config(
            {"custom": {"base_url": local, "api_key": key}},
            lambda: run_probe({
                "provider": "custom", "base_url": local, "model": "mock",
                "api_key": "EMPTY", "thinking": "off", "timeout": 10,
            }, allow_config_key=False),
        )
        assert d["auth_source"] == "none"
        assert "authorization" not in RecordingHandler.headers_seen[-1]

        # 3) 同源 + 地址匹配 → 才允许使用配置 key
        d = _with_config(
            {"custom": {"base_url": local, "api_key": key}},
            lambda: run_probe({
                "provider": "custom", "base_url": local, "model": "mock",
                "api_key": "EMPTY", "thinking": "off", "timeout": 10,
            }, allow_config_key=True),
        )
        assert d["auth_source"] == "config"
        assert RecordingHandler.headers_seen[-1]["authorization"] == f"Bearer {key}"
        assert key not in json.dumps(d, ensure_ascii=False), "响应不得包含明文 key"
        assert d["api_key_masked"]
    finally:
        srv.shutdown()

    assert config_key_allowed("deepseek", "https://api.deepseek.com/v1/", {})
    assert not config_key_allowed("deepseek", "https://evil.example/v1", {})
    assert config_key_allowed("custom", "", {"custom": {"base_url": "http://127.0.0.1:1/v1"}})


def test_probe_untrusted_private_target_blocked():
    d = run_probe({
        "provider": "custom", "base_url": "http://127.0.0.1:9/v1",
        "model": "mock", "api_key": "EMPTY", "thinking": "off", "timeout": 10,
    }, allow_config_key=False, allow_private_targets=False)
    assert d["ok"] is False and d["error_type"] == "forbidden_target"


def test_probe_error_classification():
    # MiniMax 400 + 2013 unknown model → model_or_endpoint_not_found（不再误补文本 ping）
    srv = _start_recording([(400, {"base_resp": {
        "status_code": 2013, "status_msg": "invalid params, unknown model",
    }})])
    try:
        d = run_probe({
            "provider": "custom", "base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1",
            "model": "nope", "api_key": "EMPTY", "thinking": "off", "timeout": 10,
        })
        assert d["error_type"] == "model_or_endpoint_not_found"
        assert RecordingHandler.hits == 1, "模型不存在不应再发文本 ping"
    finally:
        srv.shutdown()

    for status, expected, payload in (
        (403, "forbidden", {"error": {"message": "forbidden"}}),
        (429, "rate_limited", {"error": {"message": "slow down"}}),
        (500, "upstream_error", {"error": {"message": "boom"}}),
    ):
        srv = _start_recording([(status, payload)])
        try:
            d = run_probe({
                "provider": "custom", "base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1",
                "model": "mock", "api_key": "EMPTY", "thinking": "off", "timeout": 10,
            })
            assert d["ok"] is False and d["error_type"] == expected
        finally:
            srv.shutdown()

    # 真正“图片不支持”仍走 vision_unsupported
    srv = _start_recording([
        (400, {"error": {"message": "image input not supported"}}),
        (200, {"choices": [{"message": {"content": "OK"}}]}),
    ])
    try:
        d = run_probe({
            "provider": "custom", "base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1",
            "model": "mock", "api_key": "EMPTY", "thinking": "off", "timeout": 10,
        })
        assert d["error_type"] == "vision_unsupported" and d["text_ok"] is True
    finally:
        srv.shutdown()

    # 超时
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = _post_once(f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions",
                       "", {"model": "m"}, 0.5)
        assert d["ok"] is False and d["error_type"] == "timeout"
    finally:
        srv.shutdown()


def test_probe_image_detail_and_vision_warning():
    expected = {
        "mimo": None,
        "deepseek": "low",
        "minimax": "low",
        "custom": "low",
    }
    for provider, detail in expected.items():
        srv = _start_recording([(200, {
            "model": "mock", "choices": [{"message": {"content": "blue"}}],
        })])
        try:
            d = run_probe({
                "provider": provider,
                "base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1",
                "model": "mock", "api_key": "EMPTY", "thinking": "off", "timeout": 10,
            })
            body = RecordingHandler.bodies[-1]
            img = body["messages"][0]["content"][1]["image_url"]
            if detail is None:
                assert "detail" not in img, f"{provider} 不应发送 detail"
            else:
                assert img.get("detail") == detail, f"{provider} detail={img.get('detail')}"
            assert body["stream"] is False
            assert d["ok"] is True and d["vision_ok"] is True
            assert d["vision_confirmed"] is False
            assert d["warning"], "未识别测试图时必须给 warning，不能无条件成功"
        finally:
            srv.shutdown()

    # provider 映射表：custom 不用非标准的 "default"
    assert image_detail_for("mimo", "high") is None
    assert image_detail_for("deepseek", "medium_low") == "high"
    assert image_detail_for("minimax", "medium_low") == "default"
    assert image_detail_for("custom", "medium_low") == "auto"
    assert image_detail_for("custom", "medium") == "auto"
    assert image_detail_for("custom", "high") == "high"


def test_editor_source_signature_priority():
    src = json.dumps(CUP, ensure_ascii=False)
    edited_obj = json.loads(src)
    edited_obj["summary"]["caption"] += " 补一句。"
    edited = json.dumps(edited_obj, ensure_ascii=False)

    # 签名匹配：edits 优先（画布/手改/拆解结果）
    out, _, _ = MOSSEditor().run(src, 1024, 1024, edited,
                                 edits_source_sig=source_signature(src))
    assert json.loads(out)["summary"]["caption"].endswith("补一句。")

    # 签名不匹配（换源）或缺失：source 优先，旧画布不能遮住新散文
    for sig in (source_signature("另一份 source"), ""):
        out, _, _ = MOSSEditor().run(src, 1024, 1024, edited, edits_source_sig=sig)
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


def test_editor_stale_edits_ai_draft_uses_new_source():
    """换源 + 旧 layout edits + ai_draft：必须重新拆解新散文（不能用旧布局透传）。"""
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_ZH, ensure_ascii=False)]
        ret = MOSSEditor().run(
            "新的散文提示词。", 1024, 1024, json.dumps(LAYOUT_ZH, ensure_ascii=False),
            ai_draft=True, provider="custom",
            base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1", model="mock",
            api_key="EMPTY", temperature=0.3, thinking="off", timeout=10, ai_prompt="",
            edits_source_sig=source_signature("旧散文"),
        )
        assert isinstance(ret, dict) and "ui" in ret, "应以新散文触发 AI 拆解"
        assert MockHandler.hits == 1
        assert json.loads(ret["result"][0]) == LAYOUT_ZH
    finally:
        srv.shutdown()


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
    widget_order = list(req) + [k for k in opt
                                if k not in ("underlay", "reverse_in")]
    assert widget_order[:len(old_order)] == old_order, \
        f"Editor 旧 widgets_values 索引错位: {widget_order[:len(old_order)]}"
    assert widget_order[len(old_order):] == ["edits_source_sig", "force_split"]


def test_sniff_compiled_errors_are_friendly():
    _expect(SniffError, sniff, json.dumps({
        "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx",
    }))
    _expect(SniffError, sniff, json.dumps({
        "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx",
        "summary": {}, "items": [{"bbox": [0, 0, 1]}],
    }))
    _expect(SniffError, sniff, json.dumps({
        "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx",
        "summary": {}, "items": [1],
    }))
    _expect(SniffError, sniff, json.dumps({
        "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx",
        "summary": [], "items": [],
    }))


def test_token_stream_semantics():
    # stream 永远 false（extra_body 也不行；chat 不支持 SSE）
    for provider in ("mimo", "deepseek", "minimax", "custom"):
        assert build_body("m", 0.3, "off", provider, '{"stream": true}')["stream"] is False
    # 0 = 插件默认（build_body 决定），不是服务商默认
    b = build_body("m", 0.3, "off", "custom", "")
    assert b["max_tokens"] == 16384 and "max_completion_tokens" not in b
    b = build_body("m", 0.3, "off", "mimo", "")
    assert b["max_tokens"] == 16384 and b["max_completion_tokens"] == 16384
    b = build_body("m", 0.3, "off", "minimax", "")
    assert "max_tokens" not in b and b["max_completion_tokens"] == 65536
    assert resolve_reverse_options("manual", provider="deepseek", max_tokens=0)["max_tokens"] == 0
    # custom 非 manual 压到 16384（本地 Ollama 32K 上下文风险），manual 尊重用户值
    assert resolve_reverse_options("standard", provider="custom")["max_tokens"] == 16384
    assert resolve_reverse_options("detailed", provider="custom")["max_tokens"] == 16384
    assert resolve_reverse_options("manual", provider="custom", max_tokens=32768)["max_tokens"] == 32768


def test_route_same_origin_integration():
    """真实 aiohttp 路由：跨源拒绝；同源返回上游摘要；无法判断来源时剥离 detail。"""
    import asyncio
    import types as pytypes

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import server_routes

    target = _start_recording([(200, {
        "model": "mock", "choices": [{"message": {"content": "Red"}}],
    }), (200, {
        "model": "mock", "choices": [{"message": {"content": "Red"}}],
    })])
    routes = web.RouteTableDef()

    class FakeRouter:
        frozen = False

    fake = pytypes.SimpleNamespace(
        routes=routes,
        app=pytypes.SimpleNamespace(router=FakeRouter()),
    )
    fake_server_mod = pytypes.ModuleType("server")
    fake_server_mod.PromptServer = pytypes.SimpleNamespace(instance=fake)
    old_mod = sys.modules.get("server")
    old_registered = server_routes._REGISTERED
    sys.modules["server"] = fake_server_mod
    server_routes._REGISTERED = False
    try:
        server_routes.register_routes()

        async def run():
            app = web.Application()
            app.add_routes(routes)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                port = client.server.port
                body = {
                    "provider": "custom",
                    "base_url": f"http://127.0.0.1:{target.server_address[1]}/v1",
                    "model": "mock", "api_key": "EMPTY",
                    "thinking": "off", "timeout": 10,
                }
                r = await client.post(
                    "/moss_studio/test_connection", json=body,
                    headers={"Origin": "http://evil.example"},
                )
                assert r.status == 403
                assert (await r.json())["error_type"] == "origin_rejected"
                assert RecordingHandler.hits == 0, "跨源请求不应触达上游"

                r = await client.post(
                    "/moss_studio/test_connection", json=body,
                    headers={"Origin": f"http://127.0.0.1:{port}"},
                )
                data = await r.json()
                assert r.status == 200 and data["ok"] is True
                assert data.get("content_excerpt") == "Red"

                # 无 Origin/Sec-Fetch-Site（脚本调用）：允许请求，但不得访问本地/内网，
                # 也不回退配置 key；响应同样不返回上游 detail/content_excerpt。
                r = await client.post("/moss_studio/test_connection", json=body)
                data = await r.json()
                assert r.status == 200
                assert data["ok"] is False and data["error_type"] == "forbidden_target"
                assert "content_excerpt" not in data and "detail" not in data
                assert RecordingHandler.hits == 1, "无法判断来源的请求不应触达内网上游"
            finally:
                await client.close()

        asyncio.run(run())
    finally:
        server_routes._REGISTERED = old_registered
        if old_mod is not None:
            sys.modules["server"] = old_mod
        else:
            sys.modules.pop("server", None)
        target.shutdown()


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
    test_translator_is_changed_tracks_agent_md,
    test_agent_md_widget_selection,
    test_resolve_endpoint_and_body,
    test_http_first_try_success,
    test_http_retry_success,
    test_http_fail_after_retries,
    test_http_truncation_detected,
    test_http_permanent_not_retried,
    test_agent_md_file_limits,
    test_ai_draft_success_and_writeback,
    test_ai_draft_repairs_bad_bbox_and_extra_fields,
    test_ai_draft_repairs_missing_summary,
    test_ai_draft_falls_back_to_prose_for_empty_caption,
    test_ai_draft_retry_on_unrepairable,
    test_ai_draft_disabled_and_layout_passthrough,
    test_minimax_endpoint_and_body,
    test_minimax_business_error,
    test_vision_normalize,
    test_vision_reverse_mock,
    test_vision_reverse_retry,
    test_probe_success,
    test_probe_vision_unsupported,
    test_probe_auth_failed,
    test_probe_connection_refused,
    test_image_reverse_is_changed,
    test_image_reverse_node_run,
    test_image_reverse_node_concise,
    test_reverse_mode_presets,
    test_vision_style_and_filters,
    test_sanitize_secret_redacts_key_and_urls,
    test_chat_error_echoing_key_is_redacted,
    test_translator_final_error_redacts_key,
    test_origin_evaluation,
    test_probe_never_sends_config_key_to_foreign_base_url,
    test_route_same_origin_integration,
    test_probe_untrusted_private_target_blocked,
    test_probe_error_classification,
    test_probe_image_detail_and_vision_warning,
    test_editor_source_signature_priority,
    test_editor_stale_edits_ai_draft_uses_new_source,
    test_reverse_legacy_widget_order_and_optional,
    test_editor_legacy_widget_order,
    test_sniff_compiled_errors_are_friendly,
    test_token_stream_semantics,
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
