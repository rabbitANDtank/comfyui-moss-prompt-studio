"""ComfyUI 自定义路由：图像反推节点的“测试模型连接”按钮 + 姿势目录代理。

路由注册在 PromptServer.instance.routes 上，会同时暴露 /moss_studio/test_connection
和 /api/moss_studio/test_connection（前端 api.fetchApi 自动加 /api 前缀）。

/moss_studio/pose_catalog：运行时代理读取已安装 comfyui-pose-gallery 的中文姿势
索引（library/data/pose-zh.json），供 Editor 的姿势点选面板使用。本插件不复制、
不分发该索引数据（权利归 ギア / Anhao，详见上游 NOTICE），仅在用户已安装
pose-gallery 时就地读取。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from aiohttp import web

try:
    from .py.vision_probe import evaluate_origin, probe_allow_private, run_probe
except ImportError:  # 兼容非包方式导入
    from py.vision_probe import evaluate_origin, probe_allow_private, run_probe

_REGISTERED = False
_ATTR = "_moss_studio_probe_registered"
_SEM = None
# pose_catalog 进程内缓存：{"key": (path, mtime_ns, size), "payload": {...}}
_POSE_CACHE = {}


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


def locate_pose_catalog(custom_nodes_dirs=None):
    """定位已安装 pose-gallery 的 pose-zh.json；未安装返回 None。

    优先认 pose-gallery 自己的 COMFYUI_POSE_GALLERY_LIBRARY 环境变量（其 catalog.py
    同款语义：库根目录下 data/pose-zh.json），否则扫描 custom_nodes 目录。
    """
    env = os.environ.get("COMFYUI_POSE_GALLERY_LIBRARY")
    candidates = []
    if env:
        candidates.append(os.path.join(env, "data", "pose-zh.json"))
    if custom_nodes_dirs is None:
        try:
            import folder_paths
            custom_nodes_dirs = folder_paths.get_folder_paths("custom_nodes")
        except Exception:
            custom_nodes_dirs = []
    for d in custom_nodes_dirs or []:
        candidates.append(os.path.join(d, "comfyui-pose-gallery", "library", "data", "pose-zh.json"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _pose_catalog_payload(path: str) -> dict:
    """读取并精简姿势索引，按 (path, mtime_ns, size) 做进程内缓存。

    第三方索引是 500 KB 级文件，每次打开面板都全量 json.load 会阻塞事件循环
    （路由经 asyncio.to_thread 调用本函数）。命中原文件信息时直接返回上次结果；
    索引重装/升级后 mtime 或 size 变化即自动失效。
    """
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    if _POSE_CACHE.get("key") == key and "payload" in _POSE_CACHE:
        return _POSE_CACHE["payload"]
    with open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    items = [{
        "id": r.get("id", ""),
        "system": r.get("体系", ""),
        "category": r.get("分类", ""),
        "name": r.get("姿势名称", ""),
        "view": r.get("角度", ""),
        "search": r.get("检索", ""),
    } for r in rows if isinstance(r, dict) and r.get("姿势名称")]
    # 缩略图是否存在（存在则前端点选面板顺带显示线稿小图，走 pose-gallery 自己的资源路由）
    has_thumbs = os.path.isdir(os.path.join(os.path.dirname(os.path.dirname(path)), "thumbs"))
    payload = {"ok": True, "count": len(items), "has_thumbs": has_thumbs, "items": items}
    _POSE_CACHE["key"] = key
    _POSE_CACHE["payload"] = payload
    return payload


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
        # 同源 ≠ 可信：ComfyUI 页面会加载几十个第三方自定义节点的前端脚本，
        # 任意同源脚本都能调到这里。私网目标必须由用户在 config.json 显式打开
        # probe_allow_private（默认 false）才放行；config key 回退仍仅限同源。
        allow_private = trusted and probe_allow_private()

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
                        allow_private_targets=allow_private,
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

    @srv.routes.get("/moss_studio/pose_catalog")
    async def moss_pose_catalog(request: web.Request):
        """已安装 pose-gallery 时返回其姿势索引（精简字段），否则 404 带安装提示。"""
        try:
            path = await asyncio.to_thread(locate_pose_catalog)
        except Exception:
            logging.exception("[MOSS Studio] 姿势目录定位失败")
            path = None
        if not path:
            return web.json_response({
                "ok": False,
                "message": "未找到 comfyui-pose-gallery 的姿势索引。请在 ComfyUI/custom_nodes 安装 "
                           "https://github.com/wangweihong/comfyui-pose-gallery 后重启（索引随仓库自带，"
                           "本插件不复制其数据）。也可继续在 pose_in 手填/连线任意动作描述文本。",
            }, status=404, headers={"Cache-Control": "no-store"})
        try:
            # 读盘 + json.load 500 KB 级索引放到线程池，避免阻塞事件循环
            payload = await asyncio.to_thread(_pose_catalog_payload, path)
        except Exception:
            logging.exception("[MOSS Studio] 姿势索引读取失败: %s", path)
            return web.json_response({"ok": False, "message": "姿势索引读取失败，查看 ComfyUI 控制台"},
                                     status=500, headers={"Cache-Control": "no-store"})
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    setattr(srv, _ATTR, True)
    _REGISTERED = True
    print("[MOSS Studio] 路由已注册: /moss_studio/test_connection, /moss_studio/pose_catalog")
