"""连接测试核心：用一张 64x64 红图做最小视觉 ping，返回结构化诊断。

不依赖 ComfyUI 运行时；server_routes.py 只负责把 HTTP 请求转进来。
"""
from __future__ import annotations

import base64
import copy
import io
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from .translate import (
        build_body,
        config_key_allowed,
        load_config,
        resolve_api_key,
        resolve_endpoint,
        sanitize_secret,
        sanitize_url,
    )
    from .vision import image_detail_for
except ImportError:  # 兼容非包方式导入
    from translate import (
        build_body,
        config_key_allowed,
        load_config,
        resolve_api_key,
        resolve_endpoint,
        sanitize_secret,
        sanitize_url,
    )
    from vision import image_detail_for

try:
    from .http_safe import SameHostRedirect
except ImportError:  # 兼容非包方式导入
    from http_safe import SameHostRedirect

_VISION_PROMPT = "What is the dominant color of this image? Reply with one English color word."
_PNG_CACHE = None
_MAX_RESPONSE = 1_000_000

# 旧名保留：同主机重定向限制已提到 py/http_safe.py，探测与主 LLM 通道共用
_SameHostRedirect = SameHostRedirect


def _opener():
    handlers = [urllib.request.ProxyHandler({})]
    try:
        if load_config().get("use_proxy"):
            handlers = []
    except Exception:
        pass
    handlers.append(SameHostRedirect())
    return urllib.request.build_opener(*handlers)


def _png_data():
    global _PNG_CACHE
    if _PNG_CACHE is None:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (220, 40, 40)).save(buf, format="PNG", optimize=True)
        raw = buf.getvalue()
        _PNG_CACHE = (base64.b64encode(raw).decode("ascii"), len(raw))
    return _PNG_CACHE


def _chat_url(base_url: str) -> str:
    url = (base_url or "").rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    return url


def _safe_endpoint(url: str) -> str:
    return sanitize_url(url)


def _mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key or key == "EMPTY":
        return ""
    if len(key) <= 8:
        return "***"
    return key[:3] + "..." + key[-4:]


def _sanitize(text: str, key: str, limit: int = 500) -> str:
    return sanitize_secret(text, key, limit=limit)


# ---------- 同源判断（server_routes 与测试共用，不依赖 aiohttp）----------

def _default_port(scheme: str) -> int:
    return 443 if (scheme or "").lower() == "https" else 80


def _origin_matches_host(origin: str, host_header: str) -> bool:
    try:
        o = urllib.parse.urlsplit(origin)
        h = urllib.parse.urlsplit("//" + (host_header or ""))
    except ValueError:
        return False
    if (o.hostname or "").lower() != (h.hostname or "").lower():
        return False
    try:
        oport = o.port or _default_port(o.scheme)
        hport = h.port or _default_port(o.scheme)
    except ValueError:
        return False
    return oport == hport


def evaluate_origin(headers, host_header: str) -> str:
    """返回 'same-origin' | 'cross-origin' | 'unknown'。

    - Sec-Fetch-Site 由浏览器按页面真实来源计算，反向代理下仍可靠。
    - 老浏览器只有 Origin 时按 Host 头比较；代理改写 Host 会得到 cross-origin
      （安全侧失败：仅拒绝配置 key 回退，不阻断显式 key 的探测）。
    - 两者都没有（curl/脚本）视为 unknown：请求仍可执行，但不得使用配置 key，
      也不得访问本地/内网地址。
    """
    def _get(name):
        try:
            return (headers.get(name) or "").strip().lower()
        except Exception:
            return ""

    sec = _get("Sec-Fetch-Site")
    if sec == "same-origin":
        return "same-origin"
    if sec in ("same-site", "cross-site"):
        return "cross-origin"
    origin = _get("Origin")
    if origin:
        return "same-origin" if _origin_matches_host(origin, host_header) else "cross-origin"
    return "unknown"


def _host_is_private(url: str) -> bool:
    """目标是否为本机/内网/链路本地地址（用于 SSRF 防护）。

    解析失败按私网处理（fail-closed）；解析出多个 IP 时任一为私网即判私网。
    注意：本函数与 urllib 实际连接是两次独立 DNS 解析，仍存在 DNS rebinding
    的 TOCTOU 窗口（见 REVIEW-CODE-P0-P2 #13 的残余风险说明）。
    """
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return True
    if not host:
        return True
    if host.lower() in ("localhost", "localhost.localdomain"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        # fail-closed：解析失败无法证明目标是公网地址，按私网/阻断处理，
        # 防止攻击者用 SERVFAIL/NXDOMAIN 让检查放行、实际连接时再解析到内网。
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            # 任一解析结果落在私网即阻断（多 A/AAAA 记录不能只看第一条）
            return True
    return False


def probe_allow_private(config: dict | None = None) -> bool:
    """config.json 顶层 probe_allow_private（默认 false）。

    即使请求来自 ComfyUI 同源页面，也默认禁止连接测试访问本机/内网地址：
    同源只代表"页面里跑的脚本"（任意第三方自定义节点前端都在同源上下文），
    不能作为内网探测代理的信任边界。需要测试本地 Ollama / LM Studio 的用户
    在 config.json 显式写 "probe_allow_private": true 打开。
    """
    try:
        cfg = config if isinstance(config, dict) else load_config()
    except Exception:
        return False  # 配置损坏时按最安全侧失败
    return cfg.get("probe_allow_private") is True


def _business_error(detail: str):
    """从错误响应体里提取 (provider 业务码, message)。"""
    try:
        obj = json.loads(detail)
    except Exception:
        return None, str(detail or "")
    if not isinstance(obj, dict):
        return None, str(detail or "")
    err = obj.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        return code, f"{err.get('message') or ''} {err.get('type') or ''}".strip()
    br = obj.get("base_resp")
    if isinstance(br, dict):
        return br.get("status_code"), str(br.get("status_msg") or "")
    return obj.get("code"), str(obj.get("message") or "")


def _error_type(code: int, detail: str = "") -> str:
    if code == 401:
        return "auth_failed"
    if code == 403:
        return "forbidden"
    if code == 404:
        return "model_or_endpoint_not_found"
    if code == 408:
        return "timeout"
    if code == 429:
        return "rate_limited"
    biz_code, msg = _business_error(detail)
    try:
        biz_code_int = int(biz_code) if biz_code not in (None, "") else None
    except (TypeError, ValueError):
        biz_code_int = None
    # MiniMax 400 + 2013/2022（unknown model / model is not exist）本质是模型名或地址错
    if biz_code_int in (2013, 2022) or re.search(
        r"(?i)unknown\s+model|model.{0,24}(not\s+(exist|found)|does\s+not\s+exist)|invalid\s+model",
        msg,
    ):
        return "model_or_endpoint_not_found"
    if code in (400, 415, 422):
        return "bad_request"
    if code >= 500:
        return "upstream_error"
    return "http_error"


def _error_message(detail: str) -> str:
    try:
        obj = json.loads(detail)
    except Exception:
        return detail[:300]
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or "").strip()
            code = err.get("code")
            if msg:
                return f"{msg} ({code})" if code not in (None, "") else msg
        br = obj.get("base_resp")
        if isinstance(br, dict):
            msg = str(br.get("status_msg") or "").strip()
            code = br.get("status_code")
            if msg or code not in (None, 0):
                return f"{msg} ({code})" if msg else f"业务错误 {code}"
        msg = str(obj.get("message") or "").strip()
        if msg:
            return msg
    return detail[:300]


def _post_once(url: str, key: str, body: dict, timeout: int) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key and key != "EMPTY":
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    t0 = time.perf_counter()
    try:
        with _opener().open(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read(_MAX_RESPONSE)
    except urllib.error.HTTPError as e:
        detail = _sanitize(e.read().decode("utf-8", "replace"), key)
        retry_after = e.headers.get("Retry-After") if e.headers else None
        msg = _error_message(detail)
        if e.code == 429 and retry_after:
            msg += f"（Retry-After: {retry_after}）"
        return {
            "ok": False, "status": e.code, "error_type": _error_type(e.code, detail),
            "message": msg or f"HTTP {e.code}", "detail": detail,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
        }
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLError):
            etype, msg = "tls_error", f"TLS/证书错误: {reason}"
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            etype, msg = "timeout", f"请求超时: {reason}"
        elif isinstance(reason, socket.gaierror):
            etype, msg = "dns_error", f"域名解析失败: {reason}"
        elif isinstance(reason, ConnectionRefusedError) or getattr(reason, "errno", None) == 10061:
            etype, msg = "connection_refused", "连接被拒绝：服务未启动或端口不对"
        else:
            etype, msg = "network_error", f"网络错误: {reason}"
        return {
            "ok": False, "status": None, "error_type": etype, "message": msg,
            "detail": _sanitize(str(reason), key),
            "latency_ms": int((time.perf_counter() - t0) * 1000),
        }
    except (TimeoutError, socket.timeout):
        return {
            "ok": False, "status": None, "error_type": "timeout",
            "message": f"请求超时（>{timeout}s）",
            "detail": "", "latency_ms": int((time.perf_counter() - t0) * 1000),
        }
    except ValueError as e:
        return {
            "ok": False, "status": None, "error_type": "config_error",
            "message": f"URL 非法: {e}", "detail": "",
            "latency_ms": int((time.perf_counter() - t0) * 1000),
        }
    except OSError as e:
        return {
            "ok": False, "status": None, "error_type": "network_error",
            "message": f"网络错误: {e}", "detail": _sanitize(str(e), key),
            "latency_ms": int((time.perf_counter() - t0) * 1000),
        }

    latency = int((time.perf_counter() - t0) * 1000)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        excerpt = _sanitize(raw.decode("utf-8", "replace"), key)[:200]
        return {
            "ok": False, "status": status, "error_type": "non_json_response",
            "message": "响应不是 JSON（base_url 可能指向网页/代理）",
            "detail": excerpt, "latency_ms": latency,
        }
    if isinstance(payload, dict) and isinstance(payload.get("base_resp"), dict):
        br = payload["base_resp"]
        code = br.get("status_code")
        if code not in (0, None):
            br_json = _sanitize(json.dumps(br, ensure_ascii=False), key)
            return {
                "ok": False, "status": status,
                "error_type": _error_type(400, br_json),
                "message": _sanitize(
                    f"MiniMax 业务错误 {code}: {str(br.get('status_msg') or '').strip()}", key, 300
                ),
                "detail": br_json,
                "latency_ms": latency,
            }
    try:
        choice = payload["choices"][0]
        content = (choice["message"].get("content") or "").strip()
        finish = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return {
            "ok": False, "status": status, "error_type": "bad_response",
            "message": "响应缺少 choices[0].message.content",
            "detail": _sanitize(json.dumps(payload, ensure_ascii=False), key)[:300],
            "latency_ms": latency,
        }
    if not content:
        return {
            "ok": False, "status": status, "error_type": "empty_response",
            "message": f"模型返回空 content（finish_reason={finish}）",
            "detail": "", "latency_ms": latency,
        }
    return {
        "ok": True, "status": status, "error_type": None, "message": "",
        "content": content, "response_model": payload.get("model"),
        "finish_reason": finish, "detail": "", "latency_ms": latency,
    }


def _hint(endpoint: str, error_type: str) -> str:
    if "11434" in endpoint:
        return ("Ollama：确认模型目录为 %USERPROFILE%\\.ollama\\models 并重启；"
                "用 ollama list 确认 tag；上下文不足时设 OLLAMA_CONTEXT_LENGTH=32768")
    if "1234" in endpoint:
        return "LM Studio：确认本地 server 已启动、端口 1234、且已加载带视觉能力的模型"
    if error_type == "auth_failed":
        return "检查 api_key，以及 key 的区域与 base_url 是否匹配（如 minimax.cn / minimax.io）"
    if error_type == "model_or_endpoint_not_found":
        return "检查 model 名称和 base_url 是否以 /v1 结尾；Ollama 需填准确 tag"
    if error_type == "empty_response":
        return "推理模型可能把 token 用在思考上：降低思考强度/关闭思考，或调大 timeout"
    return ""


def run_probe(p: dict, *, allow_config_key: bool = True,
              allow_private_targets: bool = True) -> dict:
    """执行连接探测。

    allow_config_key: 调用者是否可回退 config.json 的 key。HTTP 路由只有在
      浏览器同源请求时才传 True；即使 True，也仅当 base_url 等于该 provider
      的 config/默认地址时才回退（防止 key 被发到调用者指定的任意地址）。
    allow_private_targets: 是否允许访问本地/内网地址。浏览器同源请求也只在
      config.json 的 probe_allow_private=true 时才由路由放行；无法判断来源的
      脚本请求一律禁止。
    """
    t0 = time.perf_counter()
    provider = str(p.get("provider") or "").strip()
    base_url = str(p.get("base_url") or "").strip()
    model = str(p.get("model") or "").strip()
    api_key = str(p.get("api_key") or "").strip()
    thinking = str(p.get("thinking") or "off").strip()
    extra_body = str(p.get("extra_body") or "")
    try:
        timeout = max(10, min(int(p.get("timeout") or 60), 150))
    except (TypeError, ValueError):
        timeout = 60

    try:
        cfg = load_config()
        url, mdl = resolve_endpoint(provider, base_url, model, thinking, cfg)
    except Exception as e:
        return {
            "ok": False, "error_type": "config_error",
            "message": sanitize_secret(str(e), api_key, 300),
            "status": None, "latency_ms": 0, "endpoint": "", "provider": provider,
            "model": model, "text_ok": None, "vision_ok": None,
        }
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        return {
            "ok": False, "error_type": "config_error",
            "message": "base_url 只支持 http/https", "status": None,
            "latency_ms": 0, "endpoint": _safe_endpoint(url), "provider": provider,
            "model": mdl, "text_ok": None, "vision_ok": None,
        }
    if not allow_private_targets and _host_is_private(url):
        return {
            "ok": False, "error_type": "forbidden_target",
            "message": "未开启 probe_allow_private（config.json）时不允许访问本地/内网地址",
            "status": None, "latency_ms": 0, "endpoint": _safe_endpoint(url),
            "provider": provider, "model": mdl, "text_ok": None, "vision_ok": None,
        }

    key, auth_source = resolve_api_key(provider, base_url, api_key, cfg)
    if key and auth_source == "config" and not allow_config_key:
        key, auth_source = "", "none"

    endpoint = _chat_url(url)
    try:
        body = build_body(mdl, 0.0, thinking, provider, extra_body)
    except Exception as e:
        return {
            "ok": False, "error_type": "config_error",
            "message": sanitize_secret(str(e), key, 300),
            "status": None, "latency_ms": 0, "endpoint": _safe_endpoint(endpoint),
            "provider": provider, "model": mdl, "text_ok": None, "vision_ok": None,
        }
    body["stream"] = False  # extra_body 若写了 stream=true，探测端无法解析
    if provider == "minimax":
        body["max_completion_tokens"] = 4096
    else:
        body["max_tokens"] = 2048
        if "max_completion_tokens" in body:
            body["max_completion_tokens"] = 2048

    b64, nbytes = _png_data()
    image_url = {"url": "data:image/png;base64," + b64}
    detail = image_detail_for(provider, "low")  # 与真实反推共用 provider 映射
    if detail:
        image_url["detail"] = detail
    body["messages"] = [{
        "role": "user",
        "content": [
            {"type": "text", "text": _VISION_PROMPT},
            {"type": "image_url", "image_url": image_url},
        ],
    }]
    vision = _post_once(endpoint, key, body, timeout)
    total = int((time.perf_counter() - t0) * 1000)
    base = {
        "endpoint": _safe_endpoint(endpoint),
        "provider": provider,
        "model": mdl,
        "api_key_masked": _mask_key(key),
        "auth_source": auth_source,
        "image": {"mime": "image/png", "width": 64, "height": 64, "bytes": nbytes},
        "latency_ms": total,
        "text_ok": None,
        "vision_ok": None,
    }

    if vision.get("ok"):
        content = vision.get("content") or ""
        confirmed = bool(re.search(r"\bred\b", content, re.I))
        warning = "" if confirmed else (
            "模型返回了内容，但没有确认测试图的主色为红色，可能忽略了图片输入；"
            "请用真实图片试跑一次确认视觉能力"
        )
        base.update({
            "ok": True,
            "error_type": None,
            "message": "连接成功，模型可接收图片" if confirmed else "连接成功，但未确认模型读到了图片",
            "status": vision.get("status"),
            "text_ok": True,
            "vision_ok": True,
            "vision_confirmed": confirmed,
            "warning": warning,
            "response_model": vision.get("response_model"),
            "content_excerpt": _sanitize(content, key)[:120],
            "hint": "",
        })
        return base

    # 疑似“图片不被接受”时才补文本 ping：区分“无视觉能力”与“整体不可用”。
    # 模型名/端点错误（如 MiniMax 400+2013）重试文本也不会好，直接保留原分类。
    if (vision.get("status") in (400, 415, 422)
            and vision.get("error_type") != "model_or_endpoint_not_found"):
        text_body = copy.deepcopy(body)
        text_body["messages"] = [{"role": "user", "content": "Reply with OK only."}]
        text = _post_once(endpoint, key, text_body, timeout)
        total = int((time.perf_counter() - t0) * 1000)
        if text.get("ok"):
            base.update({
                "ok": False,
                "error_type": "vision_unsupported",
                "message": "连接/鉴权正常，但当前模型或端点不接受图片输入",
                "status": vision.get("status"),
                "latency_ms": total,
                "text_ok": True,
                "vision_ok": False,
                "detail": vision.get("detail", ""),
                "hint": _hint(base["endpoint"], "vision_unsupported"),
            })
            return base
        vision = text

    base.update({
        "ok": False,
        "error_type": vision.get("error_type") or "bad_request",
        "message": vision.get("message") or "请求失败",
        "status": vision.get("status"),
        "latency_ms": total,
        "text_ok": False,
        "vision_ok": False,
        "detail": vision.get("detail", ""),
        "hint": _hint(base["endpoint"], vision.get("error_type") or ""),
    })
    return base
