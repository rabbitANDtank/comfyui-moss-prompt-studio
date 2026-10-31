"""本地 mock 服务下的翻译 HTTP 成功-重试-失败路径 / 重定向 / 密钥脱敏 / 响应体上限。"""
from helpers import (DRAFT_ZH, GOOD_EN, MockHandler, RecordingHandler,  # noqa: F401
                     _expect, _run_translator, _start_mock, _start_recording)

import contextlib
import http.server
import io
import json
import threading

from py.translate import PermanentError, TranslationError, chat, sanitize_secret  # noqa: F401
from py.vision_probe import _post_once  # noqa: F401
from nodes.translator import MOSSTranslator  # noqa: F401


# ---------- 翻译：mock HTTP 成功 / 重试 / 失败 ----------


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


# ---------- MiniMax 业务错误（HTTP 200 + base_resp） ----------


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


# ---------- 本地 mock 处理器：key 回显 / 重定向 ----------


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


# ---------- 密钥脱敏 / 跨主机重定向 / 响应体上限 ----------


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
