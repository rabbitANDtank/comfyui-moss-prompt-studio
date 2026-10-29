"""ComfyUI 自定义路由：图像反推节点的“测试模型连接”按钮。

路由注册在 PromptServer.instance.routes 上，会同时暴露 /moss_studio/test_connection
和 /api/moss_studio/test_connection（前端 api.fetchApi 自动加 /api 前缀）。
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web

try:
    from .py.vision_probe import evaluate_origin, run_probe
except ImportError:  # 兼容非包方式导入
    from py.vision_probe import evaluate_origin, run_probe

_REGISTERED = False
_ATTR = "_moss_studio_probe_registered"
_SEM = None


def _sem():
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(2)  # 最多两个并发探测，避免本地模型被反复加载
    return _SEM


def _route_error(error_type: str, message: str) -> dict:
    return {
        "ok": False, "error_type": error_type, "message": message,
        "status": None, "latency_ms": 0,
    }


def register_routes() -> None:
    global _REGISTERED
    if _REGISTERED:
        return

    try:
        from server import PromptServer
    except Exception as e:
        print(f"[MOSS Studio] PromptServer 不可用，跳过连接测试路由: {e}")
        return

    srv = getattr(PromptServer, "instance", None)
    if srv is None:
        return
    if getattr(srv, _ATTR, False):
        return
    if getattr(getattr(srv, "app", None), "router", None) and srv.app.router.frozen:
        logging.warning("[MOSS Studio] 路由表已冻结，连接测试路由未注册；请重启 ComfyUI")
        return

    @srv.routes.post("/moss_studio/test_connection")
    async def moss_test_connection(request: web.Request):
        if request.content_type != "application/json":
            return web.json_response(
                _route_error("invalid_content_type", "Content-Type 必须是 application/json"),
                status=415,
            )
        if request.content_length and request.content_length > 64 * 1024:
            return web.json_response(
                _route_error("body_too_large", "请求体过大"), status=413
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                _route_error("invalid_json", "JSON 解析失败"), status=400
            )
        if not isinstance(body, dict):
            return web.json_response(
                _route_error("invalid_json", "请求体必须是 JSON 对象"), status=400
            )

        # 同源校验（防 CSRF / 任意网页借 ComfyUI 的 key 探测内网）：
        # cross-origin 直接拒绝；无法判断来源（脚本/curl）允许请求，但不得回退
        # config key，也不得访问本地/内网地址。
        origin_state = evaluate_origin(request.headers, request.host)
        if origin_state == "cross-origin":
            return web.json_response(
                _route_error("origin_rejected", "跨源请求被拒绝：连接测试只允许 ComfyUI 页面同源调用"),
                status=403,
            )
        trusted = origin_state == "same-origin"

        try:
            timeout = int(body.get("timeout") or 60)
        except (TypeError, ValueError):
            timeout = 60
        timeout = max(10, min(timeout, 150))
        # 视觉请求失败时还会补一次文本 ping，总预算要覆盖两条路径
        total_timeout = min(2 * timeout + 20, 330)
        try:
            async with _sem():
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        run_probe, body,
                        allow_config_key=trusted,
                        allow_private_targets=trusted,
                    ),
                    timeout=total_timeout,
                )
        except asyncio.TimeoutError:
            result = _route_error("timeout", f"测试总耗时超过 {total_timeout}s")
        except Exception:
            logging.exception("[MOSS Studio] 连接测试路由内部错误")
            result = _route_error("internal_error", "测试路由内部错误，请查看 ComfyUI 控制台")

        if isinstance(result, dict) and not trusted:
            # 非浏览器同源调用不返回上游响应体（可能含内网服务信息/回显凭证）
            result.pop("detail", None)
            result.pop("content_excerpt", None)

        return web.json_response(
            result, status=200, headers={"Cache-Control": "no-store"}
        )

    setattr(srv, _ATTR, True)
    _REGISTERED = True
    print("[MOSS Studio] 连接测试路由已注册: /moss_studio/test_connection")
