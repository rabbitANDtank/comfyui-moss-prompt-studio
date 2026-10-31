"""vision 反推规范化 / 连接探测 run_probe / 私网与来源安全（fail-closed）。"""
from helpers import (LAYOUT_1000, MockHandler, RecordingHandler, _start_mock,  # noqa: F401
                     _start_recording, _with_config)

import copy
import http.server
import io
import json
import threading

from py.translate import build_body, config_key_allowed  # noqa: F401
from py.vision import (image_detail_for, normalize_vision_draft,  # noqa: F401
                       reverse_image_to_prompt)
from py.vision_probe import _post_once, evaluate_origin, run_probe  # noqa: F401


# ---------- vision 反推规范化 / mock 往返 ----------


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


# ---------- 连接探测（SequenceHandler：视觉失败→文本成功等时序） ----------


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


# ---------- 来源判定 / 配置 key 安全 ----------


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


# ---------- 私网目标阻断（DNS fail-closed / 非公网段） ----------


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


# ---------- 错误分类（SlowHandler：超时） ----------


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
