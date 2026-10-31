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

from py.compile_prompt import DraftError, compile_bbox, compile_draft, validate_compiled, validate_draft  # noqa: E402
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
from nodes.translator import MOSSTranslator, clear_translation_cache  # noqa: E402

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
    # 官方允许空 meta/tags/palette 数组、允许非 text item 携带 text
    ok = copy.deepcopy(CUP)
    ok["summary"]["meta"] = []
    ok["summary"]["palette"] = []
    ok["items"][0]["tags"] = []
    ok["items"][0]["palette"] = []
    ok["items"][0]["text"] = "官方允许的附带文字"
    validate_draft(ok)
    compile_draft(ok)
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


def test_compiled_contract_matches_draft():
    """草稿与已编译形态的 text/meta/tags/palette 规则必须一致。

    validate_draft 允许空 meta/tags/palette 与非 text item 携带非空 text；
    validate_compiled 以前会拒绝后者，导致 compile_draft 的合法产物无法回环。
    """
    d = copy.deepcopy(CUP)
    d["summary"]["meta"] = []
    d["summary"]["palette"] = []
    d["items"][0]["tags"] = []
    d["items"][0]["palette"] = []
    d["items"][0]["text"] = "官方允许的附带文字"

    # 空数组与非 text item 的 text 在草稿形态合法
    validate_draft(d)
    compiled_str = compile_draft(d)
    compiled = json.loads(compiled_str)
    assert compiled["items"][0]["text"] == "官方允许的附带文字"
    assert compiled["summary"]["meta"] == [] and compiled["summary"]["palette"] == []
    assert compiled["items"][0]["tags"] == [] and compiled["items"][0]["palette"] == []

    # 已编译形态必须接受同样的字段，且无损回环
    assert validate_compiled(compiled) == compiled_str
    assert json.loads(validate_compiled(json.loads(compiled_str))) == compiled

    # 契约只放宽“非 text item 可携带 text”，空 text 仍必须拒绝
    bad = copy.deepcopy(compiled)
    bad["items"][0]["text"] = "  "
    _expect(DraftError, validate_compiled, bad)
    # text item 缺 text 仍必须拒绝
    bad = copy.deepcopy(compiled)
    del bad["items"][1]["text"]
    _expect(DraftError, validate_compiled, bad)


def test_compile_bbox_official_rounding():
    """官方先 round(v, 9) 再 floor/ceil；编译后塌缩的框必须拒绝。"""
    assert compile_bbox([0.9999999999, 0.9999999999, 2.0, 2.0], 16, 16) == [-7, -7, -6, -6]
    _expect(DraftError, compile_bbox,
            [0.0000000001, 0.0000000001, 0.0000000002, 0.0000000002], 16, 16)


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
    requests = []  # 收到的请求体（姿势注入等需要断言 system prompt 的用例）

    def do_POST(self):
        type(self).hits += 1
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            type(self).requests.append(json.loads(raw.decode("utf-8")))
        except Exception:
            type(self).requests.append(None)
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
    MockHandler.requests = []
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


def _run_translator(base_url, reuse_translation=False):
    return MOSSTranslator().run(
        json.dumps(DRAFT_ZH, ensure_ascii=False), "custom", base_url, "mock",
        "EMPTY", 0.3, "off", 30, "", "", None,
        reuse_translation=reuse_translation,
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


def test_http_first_try_success():
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(GOOD_EN, ensure_ascii=False)]
        (out,) = _run_translator(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        assert json.loads(out) == GOOD_EN and MockHandler.hits == 1
    finally:
        srv.shutdown()


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


# ---------- 姿势注入（Pose Gallery 可选接入） ----------

POSE_A = "全身单人・标准系 / 跪立 / 四肢着地・单腿后高抬 / 俯视"
POSE_B = "全身单人・标准系 / 站姿 / S形站姿・胯部侧顶 / 正面"


def _last_system_prompt():
    msgs = MockHandler.requests[-1]["messages"]
    assert msgs[0]["role"] == "system"
    return msgs[0]["content"]


def test_pose_directive_block_modes():
    from py.translate import POSE_MARKER, pose_directive_block
    # 无输入 / 关闭 → 空串：不接入姿势时 system prompt 与历史版本逐字节一致
    assert pose_directive_block("", "", "自动") == ""
    assert pose_directive_block("  ", None, "自动") == ""
    assert pose_directive_block(POSE_A, POSE_B, "关闭") == ""
    both = pose_directive_block(POSE_A, POSE_B, "自动")
    assert POSE_A in both and POSE_B in both and POSE_MARKER in both
    assert "further LEFT" in both and "双人" in both, "双参考须含左右分配与双人姿势规则"
    assert "body-orientation" in both and "（俯视）" in both, "视角须随姿势段注入且不得转为镜头指令"
    assert "REFERENCE WINS" in both and "never blend" in both, "冲突时参考姿势须完胜，禁止与散文动作融合"
    only_a = pose_directive_block(POSE_A, "", "自动")
    assert POSE_A in only_a and POSE_B not in only_a and "more prominent" in only_a
    only_b = pose_directive_block("", POSE_B, "自动")
    assert POSE_B in only_b and "Pose B" in only_b, "只接 pose_in_2 时按单一参考 Pose B 处理"
    forced = pose_directive_block(POSE_A, POSE_B, "强制双人")
    assert "exactly 2 characters" in forced


def test_editor_pose_inputs_definition():
    opt = MOSSEditor.INPUT_TYPES()["optional"]
    # v1.7.1：pose_in/pose_in_2 为可点选/可手填 widget（不再 forceInput），连线时连线值优先
    assert not opt["pose_in"][1].get("forceInput") and not opt["pose_in_2"][1].get("forceInput")
    assert opt["pose_in"][1].get("multiline") and opt["pose_in"][1].get("default") == ""
    modes, meta = opt["pose_mode"]
    assert list(modes) == ["自动", "强制双人", "关闭"] and meta["default"] == "自动"


def test_locate_pose_catalog():
    import sys, tempfile, os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from server_routes import locate_pose_catalog
    # 未安装 → None（不抛错）
    assert locate_pose_catalog(custom_nodes_dirs=[]) is None
    # 模拟已安装：custom_nodes/comfyui-pose-gallery/library/data/pose-zh.json
    with tempfile.TemporaryDirectory() as td:
        lib = _os.path.join(td, "comfyui-pose-gallery", "library", "data")
        _os.makedirs(lib)
        p = _os.path.join(lib, "pose-zh.json")
        open(p, "w", encoding="utf-8").write("[]")
        assert locate_pose_catalog(custom_nodes_dirs=[td]) == p


def test_prose_to_layout_pose_prompt_assembly():
    """无姿势 → system prompt 与历史逐字节一致；有姿势 → 末尾追加指令段。"""
    from py.translate import append_agent_md, pose_directive_block
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_ZH, ensure_ascii=False)] * 3

        def call(**kw):
            prose_to_layout("一段散文。", width=1024, height=1024,
                            base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                            api_key="EMPTY", model="m", temperature=0.3,
                            thinking="off", provider="custom", timeout=30, **kw)

        call()
        baseline = append_agent_md(DEFAULT_AI_DRAFT_PROMPT, "")
        assert _last_system_prompt() == baseline, "无姿势时 system prompt 不得有任何变化"

        call(pose_a=POSE_A, pose_b=POSE_B)
        sys_with_pose = _last_system_prompt()
        assert sys_with_pose == baseline + "\n\n" + pose_directive_block(POSE_A, POSE_B)
        assert POSE_A in sys_with_pose and POSE_B in sys_with_pose

        call(pose_a=POSE_A, pose_mode="关闭")
        assert _last_system_prompt() == baseline, "关闭档必须等价于未接入"
    finally:
        srv.shutdown()


def test_editor_run_passes_pose_to_llm():
    """Editor 节点全链路：pose_in/pose_in_2/pose_mode 正确送达拆解 LLM 的 system prompt。"""
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_ZH, ensure_ascii=False)] * 2
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1"

        def run_pose(**kw):
            return MOSSEditor().run(
                "一段特摄战斗的纯文本提示词。", 1024, 1024, "",
                ai_draft=True, provider="custom", base_url=url, model="mock",
                api_key="EMPTY", temperature=0.3, thinking="off", timeout=30, ai_prompt="",
                **kw,
            )

        ret = run_pose(pose_in=POSE_A, pose_in_2=POSE_B, pose_mode="自动")
        assert "mps_draft" in ret["ui"], "姿势路径不影响拆解写回"
        assert POSE_A in _last_system_prompt() and POSE_B in _last_system_prompt()

        run_pose(pose_in=POSE_A, pose_in_2=POSE_B, pose_mode="关闭")
        assert POSE_A not in _last_system_prompt(), "关闭档不得把姿势送进 LLM"
    finally:
        srv.shutdown()


def test_editor_is_changed_tracks_pose():
    base = dict(source="散文", canvas_width=1024, canvas_height=1024, edits="",
                ai_draft=True)
    h0 = MOSSEditor.IS_CHANGED(**base)
    assert MOSSEditor.IS_CHANGED(**base, pose_in=POSE_A) != h0, "换姿势必须让缓存失效"
    assert MOSSEditor.IS_CHANGED(**base, pose_in_2=POSE_B) != h0
    assert MOSSEditor.IS_CHANGED(**base, pose_mode="关闭") != h0
    # 旧工作流姿势参数缺省（kwargs 兜底）也不得抛错
    assert MOSSEditor.IS_CHANGED("散文", 1024, 1024, "", True)


# ---------- 姿势本地注入（📥姿势 按钮，不调 LLM） ----------

POSE_PAIR_DESC = "双人缠绵 / 立位贴身 / 面对面・抱起单膝 / 正面"


def _mk_pose_draft():
    """item0=左侧小角色(x中心128, 面积131072)；item1=右侧大角色(x中心768, 面积524288)。"""
    return {
        "width": 1024, "height": 1024,
        "bbox_coord_space": "abs", "bbox_format": "xyxy",
        "summary": {"meta": ["特摄"], "canvas": "single", "caption": "场景描述。"},
        "items": [
            {"label": "character", "caption": "左侧较小的人物", "bbox": [0, 512, 256, 1024]},
            {"label": "character", "caption": "右侧较大的人物", "bbox": [512, 0, 1024, 1024]},
            {"label": "object", "caption": "背景招牌", "bbox": [300, 300, 400, 400]},
        ],
    }


def _captions(draft):
    return [it["caption"] for it in draft["items"]]


def test_pose_phrase_parsing():
    from py.translate import _pose_phrase
    # 图库四段格式：姿势名称 + 视角标注；体系段判定双人
    assert _pose_phrase(POSE_A) == ("四肢着地・单腿后高抬（俯视）", False)
    assert _pose_phrase(POSE_PAIR_DESC) == ("面对面・抱起单膝（正面）", True)
    # 只有三段（缺视角）：不加括号
    assert _pose_phrase("全身单人・标准系 / 跪立 / 四肢着地") == ("四肢着地", False)
    # 手写文本（无 " / " 分段）：整句使用
    assert _pose_phrase("手持长剑向前突进") == ("手持长剑向前突进", False)
    assert _pose_phrase("") == ("", False)


def test_apply_pose_single_character_replace_not_append():
    from py.translate import POSE_MARKER, apply_pose_to_draft
    draft = _mk_pose_draft()
    draft["items"] = draft["items"][:1] + [draft["items"][2]]  # 只留 1 角色 + 1 物体
    out = apply_pose_to_draft(draft, POSE_A, "")
    assert out["items"][0]["caption"].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")
    assert POSE_MARKER not in out["items"][1]["caption"], "非角色条目不得被注入"
    # 重复注入换姿势：替换而非叠加（幂等）
    out = apply_pose_to_draft(out, POSE_B, "")
    assert out["items"][0]["caption"].endswith(POSE_MARKER + "S形站姿・胯部侧顶（正面）")
    assert out["items"][0]["caption"].count(POSE_MARKER) == 1
    # 已有姿势段时保留段前原文
    assert out["items"][0]["caption"].startswith("左侧较小的人物；姿势：")


def test_apply_pose_dual_left_right_and_prominence():
    from py.translate import POSE_MARKER, apply_pose_to_draft
    # 双参考：按 bbox x 中心分配（左→A、右→B），与面积大小无关
    d = apply_pose_to_draft(_mk_pose_draft(), POSE_A, POSE_B)
    caps = _captions(d)
    assert caps[0].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）"), "左角色应得 pose_in"
    assert caps[1].endswith(POSE_MARKER + "S形站姿・胯部侧顶（正面）"), "右角色应得 pose_in_2"
    # 单参考：给面积最大（最突出）的角色，另一人保留原文
    d = apply_pose_to_draft(_mk_pose_draft(), POSE_A, "")
    caps = _captions(d)
    assert caps[1].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")
    assert POSE_MARKER not in caps[0]


def test_apply_pose_pair_pose_hits_both():
    from py.translate import POSE_MARKER, apply_pose_to_draft
    d = apply_pose_to_draft(_mk_pose_draft(), POSE_PAIR_DESC, "")
    caps = _captions(d)
    assert caps[0].endswith(POSE_MARKER + "面对面・抱起单膝（正面）")
    assert caps[1].endswith(POSE_MARKER + "面对面・抱起单膝（正面）"), "双人姿势应两人各写一段互动描述"
    assert POSE_MARKER not in caps[2]
    # 只接 pose_in_2 的双人姿势同样生效
    d2 = apply_pose_to_draft(_mk_pose_draft(), "", POSE_PAIR_DESC)
    assert _captions(d2)[0].endswith(POSE_MARKER + "面对面・抱起单膝（正面）")
    # pose_in 单人 + pose_in_2 双人：双人参考整体套用给两人，忽略单人参考
    d3 = apply_pose_to_draft(_mk_pose_draft(), POSE_A, POSE_PAIR_DESC)
    caps = _captions(d3)
    assert caps[0].endswith(POSE_MARKER + "面对面・抱起单膝（正面）")
    assert caps[1].endswith(POSE_MARKER + "面对面・抱起单膝（正面）"), \
        "pose_in_2 是双人姿势时必须写给两个角色"
    assert "四肢着地" not in caps[0] and "四肢着地" not in caps[1], "单人参考应被忽略"
    # pose_in 双人 + pose_in_2 单人：仍两人整体使用双人参考
    d4 = apply_pose_to_draft(_mk_pose_draft(), POSE_PAIR_DESC, POSE_B)
    caps = _captions(d4)
    assert caps[0].endswith(POSE_MARKER + "面对面・抱起单膝（正面）")
    assert caps[1].endswith(POSE_MARKER + "面对面・抱起单膝（正面）")
    assert "S形站姿" not in caps[0] and "S形站姿" not in caps[1]


def test_apply_pose_closeup_face_not_selected():
    """真机反推踩坑回归（四联人像）：面部特写框 [579,0,1024,1024]（445x1024，
    高宽比 2.3）面积 455k 全场最大，但三个全身框高宽比 5.5+ 更细长——
    单参考必须注入全身框而非占满画布的特写脸。"""
    from py.translate import POSE_MARKER, apply_pose_to_draft
    d = _mk_pose_draft()
    d["items"] = [
        {"label": "character", "caption": "正面全身站姿，红衣女子", "bbox": [31, 46, 195, 957]},   # 164x911 比5.6 档2
        {"label": "character", "caption": "面部大特写", "bbox": [579, 0, 1024, 1024]},             # 445x1024 比2.3 档1
    ]
    out = apply_pose_to_draft(d, POSE_A, "")
    caps = _captions(out)
    assert POSE_MARKER in caps[0], "细长全身框应被优先选中"
    assert POSE_MARKER not in caps[1], "面部特写不应获得站姿"


def test_apply_pose_all_closeup_falls_back_to_area():
    """全是特写（无更细长框）时同档退回纯面积最大，不失效。"""
    from py.translate import POSE_MARKER, apply_pose_to_draft
    d = _mk_pose_draft()
    d["items"] = [
        {"label": "character", "caption": "小特写", "bbox": [0, 0, 300, 600]},    # 300x600 比2.0
        {"label": "character", "caption": "大特写", "bbox": [400, 100, 900, 900]}, # 500x800 比1.6
    ]
    out = apply_pose_to_draft(d, POSE_A, "")
    # 两者都 <2.5 同为档1；小的反而更细长(档更高)？300x600 比2.0 仍档1，500x800 比1.6 档1
    # 同档比面积 → 大特写 400k 胜小特写 180k
    assert POSE_MARKER in _captions(out)[1]


def test_apply_pose_target_index_selection():
    from py.translate import POSE_MARKER, apply_pose_to_draft
    d = apply_pose_to_draft(_mk_pose_draft(), POSE_A, POSE_B, target_index=1)
    caps = _captions(d)
    assert caps[1].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")
    assert POSE_MARKER not in caps[0], "指定目标时其他角色不动"
    for bad in (2, 99):  # 物体下标 / 越界：明确报错
        try:
            apply_pose_to_draft(_mk_pose_draft(), POSE_A, "", target_index=bad)
            assert False, f"target_index={bad} 应报错"
        except ValueError:
            pass


def test_apply_pose_error_matrix():
    from py.translate import apply_pose_to_draft
    cases = [
        ("关闭档", dict(draft=_mk_pose_draft(), pose_a=POSE_A, pose_b="", mode="关闭")),
        ("无姿势输入", dict(draft=_mk_pose_draft(), pose_a="", pose_b="")),
        ("无角色条目", dict(draft={"items": [{"label": "object", "caption": "x", "bbox": [0, 0, 9, 9]}]},
                     pose_a=POSE_A, pose_b="")),
        ("非布局形态", dict(draft={"width": 1024}, pose_a=POSE_A, pose_b="")),
    ]
    for name, kw in cases:
        try:
            apply_pose_to_draft(**kw)
            assert False, f"{name} 应报错"
        except ValueError:
            pass
    # 独立校验闸门：注入前草稿本身不合法（bbox 缺失）→ 注入后 validate 拒绝
    bad = _mk_pose_draft()
    del bad["items"][0]["bbox"]
    try:
        apply_pose_to_draft(bad, POSE_A, "")
        assert False, "非法草稿应被注入路径的 validate_draft 拦下"
    except Exception:
        pass


def _run_editor_pose(edits_text, source="", sig="", **pose_kw):
    return MOSSEditor().run(
        source, 1024, 1024, edits_text, ai_draft=False,
        edits_source_sig=sig, **pose_kw,
    )


def test_editor_run_apply_pose_writeback_and_errors():
    from py.translate import POSE_MARKER
    draft = _mk_pose_draft()
    # ① 画布 edits 持有布局（拆解写回 / 反推 canvas_zh 的形态）：自动分配注入 → ui.mps_draft 写回
    ret = _run_editor_pose(json.dumps(draft, ensure_ascii=False), apply_pose=-2, pose_in=POSE_A)
    caps = _captions(json.loads(ret["ui"]["mps_draft"]))
    assert caps[1].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")
    assert json.loads(ret["result"][0])["items"][1]["caption"] == caps[1], "输出与写回一致"
    # ② 指定目标下标：只注入选中角色
    ret = _run_editor_pose(json.dumps(_mk_pose_draft(), ensure_ascii=False),
                           apply_pose=0, pose_in=POSE_A)
    caps = _captions(json.loads(ret["ui"]["mps_draft"]))
    assert caps[0].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")
    assert POSE_MARKER not in caps[1]
    # ③ apply_pose 缺省（-1）：普通 Queue 不注入、不产生 ui —— 三节点原行为零变化
    ret = _run_editor_pose(json.dumps(draft, ensure_ascii=False), pose_in=POSE_A)
    assert isinstance(ret, tuple) and len(ret) == 3, "无注入时保持普通三元组返回"
    # ④ 输入是散文（如反推 prose_zh 刚写入 source、edits 旧签名失效）：注入须明确报错
    try:
        _run_editor_pose(json.dumps(draft, ensure_ascii=False), source="一段散文。",
                         sig=source_signature("一段散文。") + "x",  # 签名不匹配 → 以 source 为准
                         apply_pose=-2, pose_in=POSE_A)
        assert False, "caption 形态注入应报错引导用户先拆解"
    except ValueError as e:
        assert "拆解" in str(e)
    # ⑤ IS_CHANGED：apply_pose != -1 绕过缓存（保证写回必达），缺省走哈希路径
    assert MOSSEditor.IS_CHANGED("散文", 8, 8, "", False, apply_pose=-2) != \
        MOSSEditor.IS_CHANGED("散文", 8, 8, "", False)
    import math
    assert math.isnan(MOSSEditor.IS_CHANGED("散文", 8, 8, "", False, apply_pose=0))


def test_editor_apply_pose_after_reverse_flows():
    """与反推节点关联：反推两种产物形态（draft_zh→source / canvas_zh→edits）都可注入。"""
    from py.translate import POSE_MARKER
    draft = _mk_pose_draft()
    draft_json = json.dumps(draft, ensure_ascii=False)
    # 反推 draft_zh：layout 写入 source（ai_draft 已被前端关掉）→ 直接注入
    ret = MOSSEditor().run(draft_json, 1024, 1024, "", ai_draft=False,
                           apply_pose=-2, pose_in=POSE_A)
    caps = _captions(json.loads(ret["ui"]["mps_draft"]))
    assert caps[1].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")
    # 反推 canvas_zh：layout 写入 edits，source 仍是原散文且签名匹配 → 注入 edits 画布
    sig = source_signature("一段散文。")
    ret = MOSSEditor().run("一段散文。", 1024, 1024, draft_json, ai_draft=False,
                           edits_source_sig=sig, apply_pose=-2, pose_in=POSE_A)
    caps = _captions(json.loads(ret["ui"]["mps_draft"]))
    assert caps[1].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")


def test_editor_run_ui_carries_source_sig():
    """ui.mps_src_sig 回传本次执行实际使用的 source 签名（apply_pose + AI 拆解）。"""
    draft = _mk_pose_draft()
    # ① apply_pose 分支：source 为空、布局在 edits
    ret = _run_editor_pose(json.dumps(draft, ensure_ascii=False),
                           apply_pose=-2, pose_in=POSE_A)
    assert "mps_draft" in ret["ui"]
    assert ret["ui"]["mps_src_sig"] == source_signature("")

    # ② apply_pose 分支：source 散文 + edits 签名匹配
    sig = source_signature("一段散文。")
    ret = MOSSEditor().run("一段散文。", 1024, 1024,
                           json.dumps(draft, ensure_ascii=False), ai_draft=False,
                           edits_source_sig=sig, apply_pose=-2, pose_in=POSE_A)
    assert ret["ui"]["mps_src_sig"] == sig

    # ③ 普通 layout 透传（无 mps_draft）保持原三元组返回
    ret = MOSSEditor().run(json.dumps(draft, ensure_ascii=False), 1024, 1024, "",
                           ai_draft=False)
    assert isinstance(ret, tuple) and len(ret) == 3

    # ③b source 首尾空白必须与前端 currentSourceSig 一样按 trim 后签名，避免误判 stale
    ret = MOSSEditor().run("  一段散文。\n", 1024, 1024,
                           json.dumps(draft, ensure_ascii=False), ai_draft=False,
                           edits_source_sig=sig, apply_pose=-2, pose_in=POSE_A)
    assert ret["ui"]["mps_src_sig"] == sig

    # ④ AI 拆解分支同样回传签名
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_ZH, ensure_ascii=False)]
        ret = _run_editor_ai(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        assert "mps_draft" in ret["ui"]
        assert ret["ui"]["mps_src_sig"] == source_signature("一段特摄战斗的纯文本提示词。")
    finally:
        srv.shutdown()


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
            "model": "mock", "api_key": "test-key", "thinking": "off", "timeout": 10,
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


@contextlib.contextmanager
def _config_env(cfg):
    """异步测试用的 config.json 环境变量上下文（_with_config 只支持同步回调）。"""
    old = os.environ.get("COMFY_MOSS_STUDIO_CONFIG")
    path = _temp_config(cfg)
    os.environ["COMFY_MOSS_STUDIO_CONFIG"] = path
    try:
        yield
    finally:
        os.environ.pop("COMFY_MOSS_STUDIO_CONFIG", None)
        if old is not None:
            os.environ["COMFY_MOSS_STUDIO_CONFIG"] = old
        os.unlink(path)


class RedirectHandler(http.server.BaseHTTPRequestHandler):
    """按 server.redirect_location 返回 302；为空时直接返回 200 JSON。"""

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        loc = getattr(self.server, "redirect_location", "")
        if loc:
            self.send_response(302)
            self.send_header("Location", loc)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json()

    def do_GET(self):
        self._json()

    def _json(self):
        body = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _start_redirect(location):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    srv.redirect_location = location
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv



def test_sanitize_secret_redacts_key_and_urls():
    key = "test-key-live"
    text = (f"Incorrect API key provided: {key} | Authorization: Bearer {key} | "
            f"https://user:{key}@host.example/v1?api_key={key}")
    out = sanitize_secret(text, key)
    assert key not in out
    assert "user:" not in out
    assert "api_key=" not in out
    # 即使调用方没传 key，也要按 Bearer/URL 规则兜底
    out2 = sanitize_secret("Authorization: Bearer dummy-token https://u:p@x/y?token=z")
    assert "dummy-token" not in out2 and "u:p@" not in out2
    assert "?***" in sanitize_secret("see https://x.example/v1?key=secret", "")


def test_chat_error_echoing_key_is_redacted():
    key = "test-key-echo"
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
    key = "test-key-retry"
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


def test_chat_cross_host_redirect_does_not_leak_key():
    """主 LLM 通道：跨主机 302 不得把 Authorization 带到目标主机；同主机 302 仍可用。"""
    key = "test-key-redirect"
    target = _start_recording([(200, {
        "model": "mock", "choices": [{"message": {"content": "OK"}}],
    })])
    target_url = f"http://127.0.0.1:{target.server_address[1]}/v1/chat/completions"
    # 第二个端口模拟攻击者主机：A 收到带 key 的请求后 302 到 B
    a = _start_redirect(target_url)
    a_url = f"http://127.0.0.1:{a.server_address[1]}/v1"
    try:
        try:
            chat(a_url, key, {"model": "m", "stream": False}, 5)
            raise AssertionError("跨主机 302 应抛 TranslationError")
        except TranslationError as e:
            assert "302" in str(e), f"错误信息应含状态码: {e}"
        assert RecordingHandler.hits == 0, "重定向目标主机不得收到任何请求"
        assert not RecordingHandler.headers_seen, "Authorization 不得被转发到新主机"

        # 探测通道共用同一实现：_post_once 同样不跟随跨主机重定向
        d = _post_once(target_url.replace(
            f"127.0.0.1:{target.server_address[1]}", f"127.0.0.1:{a.server_address[1]}"),
            key, {"model": "m"}, 5)
        assert d["ok"] is False and d["status"] == 302
        assert RecordingHandler.hits == 0
    finally:
        a.shutdown()
        target.shutdown()

    # 同 scheme/host/port 的重定向保持默认行为（不影响正常网关跳转）
    same = _start_redirect("/ok")
    try:
        content = chat(f"http://127.0.0.1:{same.server_address[1]}/v1", key,
                       {"model": "m", "stream": False}, 5)
        assert content == "OK", "同主机重定向必须仍然可用"
    finally:
        same.shutdown()


def test_chat_response_body_is_capped():
    """主 LLM 通道只读 _MAX_RESPONSE：超大响应体截断后按坏 JSON 报错，不会无限读入。"""
    from py.translate import _MAX_RESPONSE
    assert _MAX_RESPONSE == 1_000_000
    srv = _start_mock()
    try:
        huge = json.dumps({"choices": [{"message": {"content": "x" * 2_000_000}}]})
        assert len(huge) > _MAX_RESPONSE
        MockHandler.queue = [huge]
        try:
            chat(f"http://127.0.0.1:{srv.server_address[1]}/v1", "EMPTY",
                 {"model": "m", "stream": False}, 10)
            raise AssertionError("超过上限的响应体被截断后必须按坏响应报错")
        except TranslationError as e:
            assert "JSON" in str(e), f"截断后应按坏 JSON 报错: {e}"
        assert MockHandler.hits == 1, "不得因超大 body 重试读取"
    finally:
        srv.shutdown()


def test_probe_minimax_200_business_error():
    """HTTP 200 + base_resp 业务错误：不得因 _sanitize 三参调用抛 TypeError。"""
    srv = _start_recording([(200, {
        "base_resp": {"status_code": 1004, "status_msg": "invalid api key"},
    })])
    try:
        d = run_probe({
            "provider": "custom", "base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1",
            "model": "mock", "api_key": "EMPTY", "thinking": "off", "timeout": 10,
        })
        assert d["ok"] is False, "业务错误必须判失败，不能退化成 internal_error"
        assert d["error_type"] == "bad_request"
        assert "1004" in d["message"] and "invalid api key" in d["message"]
        assert "invalid api key" in d["detail"]
        assert RecordingHandler.hits == 1, "200+业务错误不应再补文本 ping"
    finally:
        srv.shutdown()


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


def test_pose_reinject_preserves_caption_tail():
    """重复 📥姿势 只保留已知遮挡后缀，其余旧姿势段（含手填逗号描述）整段替换。"""
    from py.translate import (OCCL_BACK, OCCL_FRONT, POSE_MARKER, _pose_set_caption,
                              apply_pose_to_draft)
    draft = _mk_pose_draft()
    draft["items"] = draft["items"][:1] + [draft["items"][2]]  # 只留 1 角色 + 1 物体
    out = apply_pose_to_draft(draft, POSE_A, "")
    out["items"][0]["caption"] += OCCL_FRONT
    out = apply_pose_to_draft(out, POSE_B, "")
    cap = out["items"][0]["caption"]
    assert cap.endswith(OCCL_FRONT), "遮挡措辞不得被重复注入吞掉"
    assert "S形站姿・胯部侧顶（正面）" in cap and "四肢着地" not in cap, "姿势段必须已更新"
    assert cap.count(POSE_MARKER) == 1

    # OCCL_BACK 同样保留
    item = {"label": "character", "caption": "某人；姿势：跪立" + OCCL_BACK}
    _pose_set_caption(item, POSE_A)
    assert item["caption"] == "某人；姿势：四肢着地・单腿后高抬（俯视）" + OCCL_BACK

    # 手填描述自带中文逗号：旧片段（双手插兜）必须整段丢弃，不得残留
    item = {"label": "character", "caption": "某人"}
    _pose_set_caption(item, "站立，双手插兜")
    _pose_set_caption(item, POSE_A)
    assert "双手插兜" not in item["caption"], "旧手填描述的逗号尾段不得被当成遮挡措辞"
    assert item["caption"] == "某人；姿势：四肢着地・单腿后高抬（俯视）"
    assert item["caption"].count(POSE_MARKER) == 1

    # 旧段含逗号 + 已知遮挡后缀：保留后缀、丢弃旧动作片段
    item = {"label": "character", "caption": "某人；姿势：站立，双手插兜" + OCCL_FRONT}
    _pose_set_caption(item, POSE_B)
    assert item["caption"] == "某人；姿势：S形站姿・胯部侧顶（正面）" + OCCL_FRONT
    assert "双手插兜" not in item["caption"]

    # 无标记时行为不变
    item = {"label": "character", "caption": "某人"}
    _pose_set_caption(item, POSE_A)
    assert item["caption"] == "某人；姿势：四肢着地・单腿后高抬（俯视）"


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
    key = "test-key-config"
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


def test_host_is_private_fails_closed_on_dns_error():
    """P2-13：DNS 解析失败必须按私网阻断；多 IP 中任一私网也判私网。"""
    import socket as _socket

    from py.vision_probe import _host_is_private

    orig = _socket.getaddrinfo
    try:
        def raise_gaierror(*a, **kw):
            raise _socket.gaierror("name resolution failed")

        _socket.getaddrinfo = raise_gaierror
        assert _host_is_private("http://attacker.invalid/v1") is True, \
            "DNS 失败必须 fail-closed，不能放行"
        d = run_probe({
            "provider": "custom", "base_url": "http://attacker.invalid/v1",
            "model": "mock", "api_key": "EMPTY", "thinking": "off", "timeout": 10,
        }, allow_config_key=False, allow_private_targets=False)
        assert d["ok"] is False and d["error_type"] == "forbidden_target"

        # 多 A 记录：任一条是私网 → 整体按私网处理
        _socket.getaddrinfo = lambda *a, **kw: [
            (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0)),
            (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("10.0.0.7", 0)),
        ]
        assert _host_is_private("http://mixed.example/v1") is True

        # 全部公网 → 放行
        _socket.getaddrinfo = lambda *a, **kw: [
            (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0)),
        ]
        assert _host_is_private("http://public.example/v1") is False
    finally:
        _socket.getaddrinfo = orig


def test_host_is_private_blocks_non_global_ranges():
    """P2：100.64/10 等共享地址段 is_private=False 但 is_global=False，必须阻断。

    旧实现对每个 IP 只检查 is_private/is_loopback/is_link_local/is_reserved/...，
    会放行 100.64.0.1 这类运营商级 NAT 地址；改为 not is_global 后统一阻断。
    """
    import socket as _socket

    from py.vision_probe import _host_is_private

    orig = _socket.getaddrinfo

    def _resolves(ip):
        return lambda *a, **kw: [
            (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", (ip, 0)),
        ]

    try:
        # 非公网地址（共享地址段）→ 阻断
        for ip in ("100.64.0.1", "100.100.100.200", "0.0.0.0", "198.18.0.1"):
            _socket.getaddrinfo = _resolves(ip)
            assert _host_is_private("http://shared.example/v1") is True, ip
        # 公网地址 → 放行
        _socket.getaddrinfo = _resolves("8.8.8.8")
        assert _host_is_private("http://public.example/v1") is False
        # 多 IP：任一非公网即阻断
        _socket.getaddrinfo = lambda *a, **kw: [
            (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("100.64.0.1", 0)),
        ]
        assert _host_is_private("http://mixed.example/v1") is True
        # DNS 失败仍 fail-closed
        def _raise_gaierror(*a, **kw):
            raise _socket.gaierror("name resolution failed")

        _socket.getaddrinfo = _raise_gaierror
        assert _host_is_private("http://attacker.invalid/v1") is True
        # run_probe 在禁止私网时同样拒绝（不发起网络请求）
        d = run_probe({
            "provider": "custom", "base_url": "http://shared.example/v1",
            "model": "mock", "api_key": "EMPTY", "thinking": "off", "timeout": 10,
        }, allow_config_key=False, allow_private_targets=False)
        assert d["ok"] is False and d["error_type"] == "forbidden_target"
    finally:
        _socket.getaddrinfo = orig


def test_probe_http_error_body_read_is_capped():
    """P2：探测的 HTTPError 分支只能读取 _MAX_RESPONSE，不能无限读错误体。"""
    import email.message
    import urllib.error

    import py.vision_probe as vp

    class RecordingBody(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.read_sizes = []

        def read(self, size=-1):
            self.read_sizes.append(size)
            return super().read(size)

    huge = b"x" * (2 * vp._MAX_RESPONSE)
    body = RecordingBody(huge)
    err = urllib.error.HTTPError(
        "http://example.invalid/v1/chat/completions", 500, "Server Error",
        email.message.Message(), body,
    )

    class FakeOpener:
        def open(self, req, timeout=None):
            raise err

    orig = vp._opener
    vp._opener = lambda: FakeOpener()
    try:
        d = _post_once("http://example.invalid/v1/chat/completions", "test-key",
                       {"model": "m"}, 5)
    finally:
        vp._opener = orig

    assert body.read_sizes == [vp._MAX_RESPONSE], \
        f"错误体必须按上限读取，实际请求 {body.read_sizes}"
    assert d["ok"] is False and d["status"] == 500
    assert d["error_type"] == "upstream_error"
    # 超长非 JSON 错误体 → 稳定的截断信息（300 字消息 + 501 字 detail）
    assert d["message"] == "x" * 300
    assert len(d["detail"]) == 501


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
                # 同源 ≠ 可信：默认配置（probe_allow_private=false）禁止私网目标
                assert r.status == 200
                assert data["ok"] is False and data["error_type"] == "forbidden_target"
                assert "content_excerpt" not in data
                assert RecordingHandler.hits == 0, "未开 probe_allow_private 时同源也不得探测内网"

                # 无 Origin/Sec-Fetch-Site（脚本调用）：允许请求，但不得访问本地/内网，
                # 也不回退配置 key；响应同样不返回上游 detail/content_excerpt。
                r = await client.post("/moss_studio/test_connection", json=body)
                data = await r.json()
                assert r.status == 200
                assert data["ok"] is False and data["error_type"] == "forbidden_target"
                assert "content_excerpt" not in data and "detail" not in data
                assert RecordingHandler.hits == 0, "无法判断来源的请求不应触达内网上游"

                # 显式开启 probe_allow_private 后同源才放行（本地 Ollama/LM Studio 场景）
                with _config_env({"probe_allow_private": True}):
                    r = await client.post(
                        "/moss_studio/test_connection", json=body,
                        headers={"Origin": f"http://127.0.0.1:{port}"},
                    )
                    data = await r.json()
                    assert r.status == 200 and data["ok"] is True
                    assert data.get("content_excerpt") == "Red"
                    assert RecordingHandler.hits == 1

                    # 开关只对可信（同源）来源生效：脚本调用仍禁止私网
                    r = await client.post("/moss_studio/test_connection", json=body)
                    data = await r.json()
                    assert data["ok"] is False and data["error_type"] == "forbidden_target"
                    assert RecordingHandler.hits == 1, "非可信来源不得借开关探测内网"
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


def test_pose_catalog_route_cache():
    """pose_catalog 路由：进程内 (path, mtime_ns, size) 缓存 + 索引更新后自动失效。"""
    import asyncio
    import builtins
    import types as pytypes

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import server_routes

    with tempfile.TemporaryDirectory() as td:
        lib = os.path.join(td, "data")
        os.makedirs(lib)
        path = os.path.join(lib, "pose-zh.json")

        def write_rows(n):
            rows = [{
                "id": f"p{i}", "体系": "单人", "分类": "站姿",
                "姿势名称": f"姿势{i}", "角度": "正面", "检索": f"p{i}",
            } for i in range(n)]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)

        write_rows(1)
        old_env = os.environ.get("COMFYUI_POSE_GALLERY_LIBRARY")
        os.environ["COMFYUI_POSE_GALLERY_LIBRARY"] = td

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
        server_routes._POSE_CACHE.clear()

        # 统计路由对索引文件的实际 open 次数（命中缓存时不得再读文件）
        real_open = builtins.open
        reads = []

        def counting_open(file, *a, **kw):
            if os.path.abspath(str(file)) == os.path.abspath(path):
                reads.append(str(file))
            return real_open(file, *a, **kw)

        async def run():
            app = web.Application()
            app.add_routes(routes)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                r = await client.get("/moss_studio/pose_catalog")
                data = await r.json()
                assert r.status == 200 and data["ok"] is True
                assert data["count"] == 1 and data["items"][0]["name"] == "姿势0"
                assert r.headers.get("Cache-Control") == "no-store"
                assert len(reads) == 1

                r = await client.get("/moss_studio/pose_catalog")
                data = await r.json()
                assert data["count"] == 1
                assert len(reads) == 1, "第二次请求必须命中缓存，不得重读文件"

                # 索引更新（size + mtime 变化）→ 缓存失效并返回新数据
                write_rows(2)
                st = os.stat(path)
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
                r = await client.get("/moss_studio/pose_catalog")
                data = await r.json()
                assert data["count"] == 2
                assert len(reads) == 2, "mtime/size 变化后必须重新读取"
            finally:
                await client.close()

        server_routes.open = counting_open
        try:
            server_routes.register_routes()
            asyncio.run(run())
        finally:
            server_routes.__dict__.pop("open", None)
            server_routes._POSE_CACHE.clear()
            server_routes._REGISTERED = old_registered
            if old_mod is not None:
                sys.modules["server"] = old_mod
            else:
                sys.modules.pop("server", None)
            if old_env is None:
                os.environ.pop("COMFYUI_POSE_GALLERY_LIBRARY", None)
            else:
                os.environ["COMFYUI_POSE_GALLERY_LIBRARY"] = old_env


# ---------- 服装注入（Costume Gallery 可选接入，v1.8） ----------

COSTUME_A = "春装 / 象牙白、荷叶边、针织、牛仔布、迷你、半身裙、大衣、短袜、白色、运动鞋"
COSTUME_B = "夏装 / 黑色、吊带、棉、短裤、凉鞋"
COSTUME_SENSITIVE = "内衣 / 黑色、蕾丝、文胸"
COSTUME_A_PHRASE = "身穿象牙白、荷叶边、针织、牛仔布、迷你、半身裙、大衣、短袜、白色、运动鞋"
COSTUME_B_PHRASE = "身穿黑色、吊带、棉、短裤、凉鞋"


def test_costume_directive_block_modes():
    from py.translate import (COSTUME_MARKER, costume_category,
                              costume_directive_block, costume_is_sensitive)
    # 无输入 / 关闭 → 空串；不接入时 system prompt 与历史版本逐字节一致
    assert costume_directive_block("", "", "自动") == ""
    assert costume_directive_block("  ", None, "自动") == ""
    assert costume_directive_block(COSTUME_A, COSTUME_B, "关闭") == ""
    both = costume_directive_block(COSTUME_A, COSTUME_B, "自动")
    assert COSTUME_A in both and COSTUME_B in both and COSTUME_MARKER in both
    assert "further LEFT" in both, "双参考须含左右分配规则"
    assert "REFERENCE WINS" in both and "never blend" in both, "冲突时参考服装须完胜"
    only_a = costume_directive_block(COSTUME_A, "", "自动")
    assert COSTUME_A in only_a and COSTUME_B not in only_a and "more prominent" in only_a
    assert "matching set" in only_a, "单参考不得默认给两人穿同一套"
    only_b = costume_directive_block("", COSTUME_B, "自动")
    assert COSTUME_B in only_b and "Costume B" in only_b, "只接 costume_in_2 时按单一参考处理"
    sensitive = costume_directive_block(COSTUME_SENSITIVE, "", "自动")
    assert "neutral terms" in sensitive and "内衣" in sensitive
    assert costume_category(COSTUME_A) == "春装"
    assert costume_category("手填红色长裙") == ""
    assert costume_is_sensitive(COSTUME_SENSITIVE)
    assert costume_is_sensitive("泳装 / 白色、比基尼")
    assert not costume_is_sensitive(COSTUME_A)
    assert not costume_is_sensitive("手填红色长裙")


def test_costume_phrase_parsing():
    from py.translate import _costume_phrase
    assert _costume_phrase(COSTUME_A) == COSTUME_A_PHRASE
    assert "春装" not in _costume_phrase(COSTUME_A), "季节分类不是服装，不拼进短语"
    assert _costume_phrase(COSTUME_B) == COSTUME_B_PHRASE
    assert _costume_phrase("泳装 / 白色、比基尼、系带") == "身穿泳装（白色、比基尼、系带）"
    assert _costume_phrase("内衣 / 黑色、蕾丝") == "身穿内衣风格服装（黑色、蕾丝）"
    assert _costume_phrase("汉服 / 红色、长裙") == "身穿汉服（红色、长裙）", "未知分类保留"
    assert _costume_phrase("红色长裙") == "红色长裙", "手填整句原样使用"
    assert _costume_phrase("春装 / ") == ""
    assert _costume_phrase("") == ""
    assert _costume_phrase("泳装 / ") == "身穿泳装"


def test_split_caption_segments_generic():
    from py.translate import (COSTUME_MARKER, OCCL_BACK, POSE_MARKER,
                              _join_caption_segments, _split_caption_segments,
                              _set_caption_segment)
    cap = "角色甲" + POSE_MARKER + "站立" + COSTUME_MARKER + "红色长裙" + OCCL_BACK
    head, segs, tail = _split_caption_segments(cap)
    assert head == "角色甲"
    assert segs == {"pose": "站立", "costume": "红色长裙"}
    assert tail == OCCL_BACK
    # 拼回顺序固定：姿势 → 服装 → 遮挡后缀
    assert _join_caption_segments(head, segs, tail) == cap
    item = {"caption": cap}
    _set_caption_segment(item, "pose", "跪立")
    assert item["caption"] == "角色甲" + POSE_MARKER + "跪立" + COSTUME_MARKER + "红色长裙" + OCCL_BACK
    _set_caption_segment(item, "costume", "蓝色外套")
    assert item["caption"] == "角色甲" + POSE_MARKER + "跪立" + COSTUME_MARKER + "蓝色外套" + OCCL_BACK
    # 无标记时 head 原样保留
    head2, segs2, tail2 = _split_caption_segments("无标记角色")
    assert (head2, segs2, tail2) == ("无标记角色", {}, "")


def test_apply_costume_single_and_dual():
    from py.translate import COSTUME_MARKER, apply_costume_to_draft
    # 单参考 → 最突出角色（item1，右侧大面积）
    d = apply_costume_to_draft(_mk_pose_draft(), COSTUME_A, "")
    caps = _captions(d)
    assert caps[1].endswith(COSTUME_MARKER + COSTUME_A_PHRASE)
    assert COSTUME_MARKER not in caps[0]
    assert COSTUME_MARKER not in caps[2], "非角色条目不得被注入"
    # 双参考 → 左 costume_in、右 costume_in_2
    d = apply_costume_to_draft(_mk_pose_draft(), COSTUME_A, COSTUME_B)
    caps = _captions(d)
    assert caps[0].endswith(COSTUME_MARKER + COSTUME_A_PHRASE), "左角色应得 costume_in"
    assert caps[1].endswith(COSTUME_MARKER + COSTUME_B_PHRASE), "右角色应得 costume_in_2"
    # 重复注入幂等：替换不叠加
    d = apply_costume_to_draft(d, COSTUME_B, COSTUME_A)
    caps = _captions(d)
    assert caps[0].count(COSTUME_MARKER) == 1 and caps[1].count(COSTUME_MARKER) == 1
    assert caps[0].endswith(COSTUME_MARKER + COSTUME_B_PHRASE)
    assert caps[1].endswith(COSTUME_MARKER + COSTUME_A_PHRASE)


def test_apply_costume_target_and_errors():
    from py.translate import COSTUME_MARKER, apply_costume_to_draft
    # 指定目标下标：只注入选中角色
    d = apply_costume_to_draft(_mk_pose_draft(), COSTUME_A, "", target_index=0)
    caps = _captions(d)
    assert caps[0].endswith(COSTUME_MARKER + COSTUME_A_PHRASE)
    assert COSTUME_MARKER not in caps[1]
    # 只接 costume_in_2 也生效
    d = apply_costume_to_draft(_mk_pose_draft(), "", COSTUME_B, target_index=1)
    assert _captions(d)[1].endswith(COSTUME_MARKER + COSTUME_B_PHRASE)
    # 单角色 + 双参考：只套用 costume_in，不得越界
    solo = {"width": 1024, "height": 1024,
            "bbox_coord_space": "abs", "bbox_format": "xyxy",
            "summary": {"meta": ["特摄"], "canvas": "single", "caption": "场景。"},
            "items": [{"label": "character", "caption": "唯一角色", "bbox": [0, 0, 512, 1024]}]}
    d = apply_costume_to_draft(solo, COSTUME_A, COSTUME_B)
    assert _captions(d)[0].endswith(COSTUME_MARKER + COSTUME_A_PHRASE)
    assert _captions(d)[0].count(COSTUME_MARKER) == 1
    cases = [
        ("关闭档", dict(draft=_mk_pose_draft(), costume_a=COSTUME_A, costume_b="", mode="关闭")),
        ("无服装输入", dict(draft=_mk_pose_draft(), costume_a="", costume_b="")),
        ("只有季节分类", dict(draft=_mk_pose_draft(), costume_a="春装 / ", costume_b="")),
        ("非布局形态", dict(draft={"width": 1024}, costume_a=COSTUME_A, costume_b="")),
        ("无角色条目", dict(draft={"width": 1024, "height": 1024, "items": [
            {"label": "object", "caption": "招牌", "bbox": [0, 0, 10, 10]}]},
            costume_a=COSTUME_A, costume_b="")),
        ("目标越界", dict(draft=_mk_pose_draft(), costume_a=COSTUME_A, costume_b="",
                     target_index=99)),
        ("目标非角色", dict(draft=_mk_pose_draft(), costume_a=COSTUME_A, costume_b="",
                     target_index=2)),
    ]
    for name, kw in cases:
        try:
            apply_costume_to_draft(**kw)
            assert False, f"{name} 应抛 ValueError"
        except ValueError as e:
            assert str(e), name


def test_pose_costume_coexist_and_tail_preserved():
    from py.translate import (COSTUME_MARKER, OCCL_FRONT, POSE_MARKER,
                              _costume_phrase, apply_costume_to_draft,
                              apply_pose_to_draft)
    d = _mk_pose_draft()
    apply_pose_to_draft(d, POSE_A, "")
    apply_costume_to_draft(d, COSTUME_A, "")
    cap = _captions(d)[1]
    assert cap.count(POSE_MARKER) == 1 and cap.count(COSTUME_MARKER) == 1
    assert cap.index(POSE_MARKER) < cap.index(COSTUME_MARKER), "顺序固定为 姿势 → 服装"
    assert cap.endswith(COSTUME_MARKER + COSTUME_A_PHRASE)
    # 再注入姿势：服装段必须保留
    apply_pose_to_draft(d, POSE_B, "")
    cap = _captions(d)[1]
    assert cap.count(POSE_MARKER) == 1 and cap.count(COSTUME_MARKER) == 1
    assert POSE_MARKER + "S形站姿・胯部侧顶（正面）" in cap
    assert cap.endswith(COSTUME_MARKER + COSTUME_A_PHRASE), "姿势注入不得删掉服装段"
    # 再注入服装：姿势段必须保留
    apply_costume_to_draft(d, COSTUME_B, "")
    cap = _captions(d)[1]
    assert cap.count(POSE_MARKER) == 1 and cap.count(COSTUME_MARKER) == 1
    assert POSE_MARKER + "S形站姿・胯部侧顶（正面）" in cap, "服装注入不得删掉姿势段"
    assert cap.endswith(COSTUME_MARKER + COSTUME_B_PHRASE)
    # 已知遮挡后缀必须留在最后
    d["items"][1]["caption"] += OCCL_FRONT
    apply_costume_to_draft(d, COSTUME_A, "")
    cap = _captions(d)[1]
    assert cap.endswith(OCCL_FRONT)
    assert cap.index(POSE_MARKER) < cap.index(COSTUME_MARKER)
    assert _costume_phrase(COSTUME_A) in cap


def test_apply_costume_removes_overlapping_old_costume_objects():
    """反推草稿把旧服装拆成 object 时，本地服装注入必须移除它，否则旧衣服会画回去。"""
    from py.translate import apply_costume_to_draft
    d = _mk_pose_draft()
    d["items"] += [
        # 与右侧大角色重叠的旧连衣裙：应移除
        {"label": "object", "caption": "浅粉色无袖连衣裙，领口有三角形装饰",
         "bbox": [600, 200, 1000, 900]},
        # 背景/配饰：不含服装名词，保留
        {"label": "object", "caption": "背景为灰白色斑驳水泥墙面",
         "bbox": [0, 0, 1024, 1024]},
        {"label": "object", "caption": "右手佩戴金色戒指", "bbox": [700, 800, 800, 900]},
    ]
    before = len(d["items"])
    out = apply_costume_to_draft(d, COSTUME_A, "")
    caps = _captions(out)
    assert not any("连衣裙" in c for c in caps), "与角色重叠的旧服装 object 应被移除"
    assert any("水泥墙" in c for c in caps), "背景 object 必须保留"
    assert any("戒指" in c for c in caps), "配饰 object 必须保留"
    assert len(out["items"]) == before - 1
    # 不重叠的场景服装（如橱窗里的裙子）保留
    d2 = _mk_pose_draft()
    d2["items"].append({"label": "object", "caption": "橱窗里的红色连衣裙",
                        "bbox": [0, 0, 100, 100]})
    out2 = apply_costume_to_draft(d2, COSTUME_A, "")
    assert any("橱窗里的红色连衣裙" in c for c in _captions(out2)), "不重叠的场景服装不得误删"
    # 非法 bbox 的重叠比例返回 0（避免清理逻辑自己抛错；草稿仍由 validate_draft 把关）
    from py.translate import _bbox_overlap_ratio
    assert _bbox_overlap_ratio("bad", [0, 0, 10, 10]) == 0
    assert _bbox_overlap_ratio([0, 0, 10, 10], "bad") == 0


def test_editor_costume_inputs_definition():
    opt = MOSSEditor.INPUT_TYPES()["optional"]
    assert opt["costume_in"][1].get("multiline") and opt["costume_in"][1].get("default") == ""
    assert opt["costume_in_2"][1].get("multiline")
    modes, meta = opt["costume_mode"]
    assert list(modes) == ["自动", "关闭"] and meta["default"] == "自动"
    apply_meta = opt["apply_costume"][1]
    assert apply_meta["default"] == -1 and apply_meta["hidden"] and apply_meta["socketless"]
    # 新控件必须全部追加在姿势控件之后，旧工作流 widgets_values 不错位
    names = list(opt)
    assert names.index("costume_mode") > names.index("pose_in_2")
    assert names.index("costume_in") > names.index("apply_costume") > names.index("costume_mode")


def test_editor_is_changed_tracks_costume():
    base = dict(source="散文", canvas_width=1024, canvas_height=1024, edits="",
                ai_draft=True)
    h0 = MOSSEditor.IS_CHANGED(**base)
    assert MOSSEditor.IS_CHANGED(**base, costume_in=COSTUME_A) != h0, "换服装必须让缓存失效"
    assert MOSSEditor.IS_CHANGED(**base, costume_in_2=COSTUME_B) != h0
    assert MOSSEditor.IS_CHANGED(**base, costume_mode="关闭") != h0
    import math
    assert math.isnan(MOSSEditor.IS_CHANGED(**base, apply_costume=0))
    assert math.isnan(MOSSEditor.IS_CHANGED(**base, apply_costume=-2))
    # 旧工作流参数缺省（kwargs 兜底）也不得抛错
    assert MOSSEditor.IS_CHANGED("散文", 1024, 1024, "", True)


def test_prose_to_layout_costume_prompt_assembly():
    """无服装 → system prompt 与历史逐字节一致；有服装 → 末尾追加指令段。"""
    from py.translate import (append_agent_md, costume_directive_block,
                              pose_directive_block)
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_ZH, ensure_ascii=False)] * 4

        def call(**kw):
            prose_to_layout("一段散文。", width=1024, height=1024,
                            base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                            api_key="EMPTY", model="m", temperature=0.3,
                            thinking="off", provider="custom", timeout=30, **kw)

        call()
        baseline = append_agent_md(DEFAULT_AI_DRAFT_PROMPT, "")
        assert _last_system_prompt() == baseline, "无服装时 system prompt 不得有任何变化"

        call(costume_a=COSTUME_A, costume_b=COSTUME_B)
        assert _last_system_prompt() == baseline + "\n\n" + \
            costume_directive_block(COSTUME_A, COSTUME_B)

        call(costume_a=COSTUME_A, costume_mode="关闭")
        assert _last_system_prompt() == baseline, "关闭档必须等价于未接入"

        call(pose_a=POSE_A, costume_a=COSTUME_A)
        assert _last_system_prompt() == baseline + "\n\n" + \
            pose_directive_block(POSE_A, "") + "\n\n" + \
            costume_directive_block(COSTUME_A, ""), "姿势段在前、服装段在后"
    finally:
        srv.shutdown()


def test_editor_run_passes_costume_to_llm():
    """Editor 全链路：costume_in/costume_in_2/costume_mode 正确送达拆解 LLM。"""
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_ZH, ensure_ascii=False)] * 3
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1"

        def run_costume(**kw):
            return MOSSEditor().run(
                "一段特摄战斗的纯文本提示词。", 1024, 1024, "",
                ai_draft=True, provider="custom", base_url=url, model="mock",
                api_key="EMPTY", temperature=0.3, thinking="off", timeout=30, ai_prompt="",
                **kw,
            )

        ret = run_costume(costume_in=COSTUME_A, costume_in_2=COSTUME_B, costume_mode="自动")
        assert "mps_draft" in ret["ui"], "服装路径不影响拆解写回"
        assert COSTUME_A in _last_system_prompt() and COSTUME_B in _last_system_prompt()

        run_costume(costume_in=COSTUME_A, costume_mode="关闭")
        assert COSTUME_A not in _last_system_prompt(), "关闭档不得把服装送进 LLM"

        run_costume(pose_in=POSE_A, costume_in=COSTUME_A)
        sys_prompt = _last_system_prompt()
        assert sys_prompt.index("Pose references") < sys_prompt.index("Costume references")
    finally:
        srv.shutdown()


def test_editor_run_apply_costume_writeback_and_errors():
    from py.translate import COSTUME_MARKER, POSE_MARKER
    draft = _mk_pose_draft()
    # ① 自动分配 → ui.mps_draft 写回，输出与写回一致
    ret = _run_editor_pose(json.dumps(draft, ensure_ascii=False),
                           apply_costume=-2, costume_in=COSTUME_A)
    caps = _captions(json.loads(ret["ui"]["mps_draft"]))
    assert caps[1].endswith(COSTUME_MARKER + COSTUME_A_PHRASE)
    assert json.loads(ret["result"][0])["items"][1]["caption"] == caps[1]
    # ② 指定目标下标：只注入选中角色
    ret = _run_editor_pose(json.dumps(_mk_pose_draft(), ensure_ascii=False),
                           apply_costume=0, costume_in=COSTUME_A)
    caps = _captions(json.loads(ret["ui"]["mps_draft"]))
    assert caps[0].endswith(COSTUME_MARKER + COSTUME_A_PHRASE)
    assert COSTUME_MARKER not in caps[1]
    # ③ apply_costume 缺省（-1）：普通 Queue 不注入、不产生 ui
    ret = _run_editor_pose(json.dumps(draft, ensure_ascii=False), costume_in=COSTUME_A)
    assert isinstance(ret, tuple) and len(ret) == 3, "无注入时保持普通三元组返回"
    # ④ 姿势 + 服装同一提交：两个分段都写入且顺序正确
    ret = _run_editor_pose(json.dumps(_mk_pose_draft(), ensure_ascii=False),
                           apply_pose=-2, apply_costume=-2,
                           pose_in=POSE_A, costume_in=COSTUME_A)
    cap = _captions(json.loads(ret["ui"]["mps_draft"]))[1]
    assert POSE_MARKER in cap and COSTUME_MARKER in cap
    assert cap.index(POSE_MARKER) < cap.index(COSTUME_MARKER)
    # ⑤ 输入是散文：注入须明确报错
    try:
        _run_editor_pose(json.dumps(draft, ensure_ascii=False), source="一段散文。",
                         sig=source_signature("一段散文。") + "x",
                         apply_costume=-2, costume_in=COSTUME_A)
        assert False, "caption 形态注入应报错引导用户先拆解"
    except ValueError as e:
        assert "拆解" in str(e)
    # ⑥ costume_mode=关闭：拒绝注入
    try:
        _run_editor_pose(json.dumps(draft, ensure_ascii=False),
                         apply_costume=-2, costume_in=COSTUME_A, costume_mode="关闭")
        assert False, "关闭档应拒绝注入"
    except ValueError as e:
        assert "costume_mode" in str(e)
    # ⑦ IS_CHANGED：apply_costume != -1 绕过缓存
    import math
    assert math.isnan(MOSSEditor.IS_CHANGED("散文", 8, 8, "", False, apply_costume=-2))


TESTS = [
    test_compile_official_examples,
    test_validate_rejects,
    test_compiled_contract_matches_draft,
    test_compile_bbox_official_rounding,
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
    test_agent_md_validate_inputs_bypasses_combo_layer,
    test_resolve_endpoint_and_body,
    test_http_first_try_success,
    test_translator_reuses_cached_translation_when_enabled,
    test_translator_cache_invalidates_on_draft_change,
    test_translator_reuse_off_forces_fresh_and_refreshes_cache,
    test_translator_is_changed_tracks_reuse_switch,
    test_translator_reuse_widget_is_appended,
    test_translator_compiled_non_1024_canvas,
    test_translator_canvas_dimensions_affect_cache_and_is_changed,
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
    test_chat_cross_host_redirect_does_not_leak_key,
    test_chat_response_body_is_capped,
    test_origin_evaluation,
    test_probe_never_sends_config_key_to_foreign_base_url,
    test_route_same_origin_integration,
    test_pose_catalog_route_cache,
    test_probe_untrusted_private_target_blocked,
    test_host_is_private_fails_closed_on_dns_error,
    test_host_is_private_blocks_non_global_ranges,
    test_probe_http_error_body_read_is_capped,
    test_probe_error_classification,
    test_probe_minimax_200_business_error,
    test_probe_image_detail_and_vision_warning,
    test_editor_source_signature_priority,
    test_editor_stale_edits_ai_draft_uses_new_source,
    test_reverse_legacy_widget_order_and_optional,
    test_editor_legacy_widget_order,
    test_sniff_compiled_errors_are_friendly,
    test_token_stream_semantics,
    # 姿势注入（拆解 LLM 路径 + 📥姿势 本地注入路径 + 反推关联）
    test_pose_directive_block_modes,
    test_editor_pose_inputs_definition,
    test_locate_pose_catalog,
    test_prose_to_layout_pose_prompt_assembly,
    test_editor_run_passes_pose_to_llm,
    test_editor_is_changed_tracks_pose,
    test_editor_is_changed_tracks_llm_config,
    test_pose_phrase_parsing,
    test_apply_pose_single_character_replace_not_append,
    test_pose_reinject_preserves_caption_tail,
    test_apply_pose_dual_left_right_and_prominence,
    test_apply_pose_pair_pose_hits_both,
    test_apply_pose_target_index_selection,
    test_apply_pose_closeup_face_not_selected,
    test_apply_pose_all_closeup_falls_back_to_area,
    test_apply_pose_error_matrix,
    test_editor_run_apply_pose_writeback_and_errors,
    test_editor_run_ui_carries_source_sig,
    test_editor_apply_pose_after_reverse_flows,
    # 服装注入（Costume Gallery 可选接入；通用 caption segment + 本地注入路径）
    test_costume_directive_block_modes,
    test_costume_phrase_parsing,
    test_split_caption_segments_generic,
    test_apply_costume_single_and_dual,
    test_apply_costume_target_and_errors,
    test_apply_costume_removes_overlapping_old_costume_objects,
    test_pose_costume_coexist_and_tail_preserved,
    test_editor_costume_inputs_definition,
    test_editor_is_changed_tracks_costume,
    test_prose_to_layout_costume_prompt_assembly,
    test_editor_run_passes_costume_to_llm,
    test_editor_run_apply_costume_writeback_and_errors,
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
