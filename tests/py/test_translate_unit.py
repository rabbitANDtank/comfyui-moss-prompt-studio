"""翻译结构完整性校验 / endpoint 与请求体组装（不启动 mock 服务的 translate 直测）。"""
from helpers import DRAFT_ZH, GOOD_EN, _expect  # noqa: F401  (helpers 先导入以初始化 sys.path)

import copy
import json

from py.compile_prompt import compile_draft, validate_draft  # noqa: F401
from py.sniff import sniff  # noqa: F401
from py.translate import (TranslationError, build_body, check_translation,  # noqa: F401
                          extract_json, resolve_endpoint)
from nodes.image_reverse import resolve_reverse_options  # noqa: F401


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


# ---------- MiniMax endpoint / 请求体 ----------


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


# ---------- token / stream 语义 ----------


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
