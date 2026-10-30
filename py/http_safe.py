"""HTTP 客户端安全辅助：带 Authorization 的请求禁止跨主机重定向。

urllib 的默认 HTTPRedirectHandler 只剔除 content-length / content-type，
Authorization: Bearer <key> 会被原样带到重定向目标主机。探测通道
（vision_probe）与主 LLM 通道（translate.chat）共用本模块的
SameHostRedirect：跨主机 3xx 直接抛 HTTPError（不发送请求到新主机），
同 scheme/host/port 的重定向保持默认可用。

本模块只依赖标准库，供各子模块用「try: 相对 import / except: 绝对 import」
的兜底风格引入，互相之间没有循环依赖。
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request


def _origin(url: str):
    """→ (scheme, hostname, port)；端口缺省按 scheme 归一。"""
    p = urllib.parse.urlsplit(url)
    port = p.port
    if port is None:
        port = 443 if (p.scheme or "").lower() == "https" else 80
    return ((p.scheme or "").lower(), (p.hostname or "").lower(), port)


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """只跟随同主机（scheme/host/port 完全一致）的重定向。

    跨主机/跨 scheme/跨端口一律抛 HTTPError，调用方按普通 HTTP 错误处理；
    这样 Authorization 永远不会被 urllib 转发到另一台主机。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(req.full_url) != _origin(newurl):
            # 不跟随：把 3xx 当错误交给上层（HTTPError 会在 open() 处抛出）
            raise urllib.error.HTTPError(
                req.full_url, code, "cross-host redirect blocked", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)
