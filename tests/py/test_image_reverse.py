"""image_reverse 节点（三路输出/ui 写回）与 reverse_modes 预设 / 风格指令。"""
from helpers import LAYOUT_1000, MockHandler, _start_mock  # noqa: F401

import copy
import json

from py.translate import AGENT_DEFAULT  # noqa: F401
from py.vision import build_style_instructions, normalize_vision_draft  # noqa: F401
from nodes.image_reverse import MOSSImageReverse, resolve_reverse_options  # noqa: F401


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
