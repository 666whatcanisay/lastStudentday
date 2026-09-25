# -*- coding: utf-8 -*-
"""
本机安全配置（方案 B 4.1）与临时 Funnel 演示域名校验。

系统仅监听 127.0.0.1，但"只监听本机"不等于跨站请求防护：
浏览器里任何恶意页面都能向 127.0.0.1:8000 发请求（CSRF/DNS rebinding 思路）。
因此加两层朴素的硬校验：

1. Host 白名单：默认只接收本机；演示模式再加入准确的 ts.net 域名。
2. Origin 同源：变更类方法（非 GET/HEAD/OPTIONS）若带 Origin 头，
   必须是本机或准确配置的演示域名来源，否则 403。
   不带 Origin 的本机工具（curl/脚本/同源导航）不受影响。

与 CORS 白名单互补：CORS 管"浏览器能不能读响应"，这里管"请求能不能进来"。
"""
import logging
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse

from app.services.demo_auth import DEMO_AUTH

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
if DEMO_AUTH.settings.enabled:
    ALLOWED_HOSTS.add(urlparse(DEMO_AUTH.settings.public_origin).hostname)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _host_of(value: str) -> str:
    v = (value or "").strip().lower()
    if v.startswith("["):  # [::1]:8000 形式
        return v[1:].split("]")[0]
    return v.split(":")[0]


async def local_security_middleware(request: Request, call_next):
    host = _host_of(request.headers.get("host", ""))
    if host not in ALLOWED_HOSTS:
        logger.warning(f"拒绝非本机 Host 请求: {host} {request.url.path}")
        return JSONResponse(
            status_code=403,
            content={"code": 403, "msg": "仅允许本机访问", "data": None},
        )

    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin:
            same_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin != same_origin and origin not in LOCAL_CORS_ORIGINS:
                logger.warning(
                    f"拒绝跨站变更请求: Origin={origin} {request.method} {request.url.path}")
                return JSONResponse(
                    status_code=403,
                    content={"code": 403, "msg": "拒绝跨站变更请求", "data": None},
                )

    return await call_next(request)


# 本机 CORS 白名单（开发期 Vite dev server 5173 + 生产同源 8000）
LOCAL_CORS_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
]
if DEMO_AUTH.settings.enabled:
    LOCAL_CORS_ORIGINS.append(DEMO_AUTH.settings.public_origin)
