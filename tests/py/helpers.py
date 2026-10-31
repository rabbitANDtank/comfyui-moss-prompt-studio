"""tests/py 共享测试设施（自原单文件 tests/run_tests.py 纯移动抽出）。

包含: 官方示例数据 fixture / 断言助手 _expect / mock HTTP 基础设施 /
      配置注入 / 测试运行器助手。仅被单一功能簇使用的设施留在对应的
      test_*.py 文件内，不在此堆放。
"""
import contextlib
import http.server
import json
import os
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)  # 测试模块 `from py... import ...` 依赖仓库根在 sys.path

from nodes.editor import MOSSEditor  # noqa: E402
from nodes.translator import MOSSTranslator  # noqa: E402


# ---------- 官方示例数据与共享 fixture ----------


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


LAYOUT_ZH = {
    "width": 1024, "height": 1024,
    "bbox_coord_space": "abs", "bbox_format": "xyxy",
    "summary": {"meta": ["特摄"], "canvas": "single", "caption": "废弃工厂内的战斗，绿色逆光。"},
    "items": [
        {"label": "character", "caption": "成年女性战队战士向前突进，挥动能量剑。", "bbox": [256, 384, 704, 896]},
        {"label": "character", "caption": "酸性绿色黏液怪人从右上压迫。", "bbox": [576, 128, 960, 512]},
    ],
}


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


POSE_A = "全身单人・标准系 / 跪立 / 四肢着地・单腿后高抬 / 俯视"
POSE_B = "全身单人・标准系 / 站姿 / S形站姿・胯部侧顶 / 正面"


def _expect(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


# ---------- mock HTTP 基础设施 ----------


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


# ---------- 配置注入 ----------


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


# ---------- 测试运行器助手 ----------


def _run_translator(base_url, reuse_translation=False):
    return MOSSTranslator().run(
        json.dumps(DRAFT_ZH, ensure_ascii=False), "custom", base_url, "mock",
        "EMPTY", 0.3, "off", 30, "", "", None,
        reuse_translation=reuse_translation,
    )


def _run_editor_ai(base_url, draft_widget="", ai=True):
    return MOSSEditor().run(
        "一段特摄战斗的纯文本提示词。", 1024, 1024, draft_widget,
        ai_draft=ai, provider="custom", base_url=base_url, model="mock",
        api_key="EMPTY", temperature=0.3, thinking="off", timeout=30, ai_prompt="",
    )


def _last_system_prompt():
    msgs = MockHandler.requests[-1]["messages"]
    assert msgs[0]["role"] == "system"
    return msgs[0]["content"]


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


def _run_editor_pose(edits_text, source="", sig="", **pose_kw):
    return MOSSEditor().run(
        source, 1024, 1024, edits_text, ai_draft=False,
        edits_source_sig=sig, **pose_kw,
    )
