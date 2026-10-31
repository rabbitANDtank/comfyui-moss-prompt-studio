"""server_routes 集成：同源探测路由与 pose_catalog 路由缓存（真实 aiohttp TestServer）。"""
from helpers import RecordingHandler, _config_env, _start_recording  # noqa: F401

import json
import os
import sys
import tempfile


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
