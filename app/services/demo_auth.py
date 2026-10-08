"""Temporary, single-process access gate for a public Tailscale Funnel demo.

The default local deployment has no login. Demo mode fails closed at startup,
uses opaque in-memory sessions, and expires every session on process restart.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse


COOKIE_NAME = "doc_ocr_demo_session"
SESSION_SECONDS = 8 * 60 * 60
PASSWORD_ITERATIONS = 600_000
_DOC_ID_PATH = re.compile(r"^/api/document/(-?\d+)(?:/(revisions(?:/\d+)?|restore|permanent))?$")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def password_hash(password: str, *, min_length: int = 12) -> str:
    # 下限从 16 降到 12：单口令模式沿用用户既有口令（14 字符），
    # 该口令由用户指定而非脚本随机生成，硬性 16 位会直接拒绝配置。
    # 12 与 scripts/setup_demo_auth.py 的 --guest-password 校验保持一致。
    if len(password) < min_length:
        raise ValueError(f"演示口令至少需要 {min_length} 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def valid_password_hash(encoded: str) -> bool:
    try:
        algorithm, rounds, salt, digest = encoded.split("$")
        return (algorithm == "pbkdf2_sha256"
                and int(rounds) == PASSWORD_ITERATIONS
                and len(_unb64(salt)) == 16
                and len(_unb64(digest)) == 32)
    except (ValueError, TypeError):
        return False


def verify_password(password: str, encoded: str) -> bool:
    if not valid_password_hash(encoded):
        return False
    _, rounds, salt, expected = encoded.split("$")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 _unb64(salt), int(rounds))
    return hmac.compare_digest(actual, _unb64(expected))


def validated_public_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".ts.net")
            or parsed.port is not None or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or "*" in value):
        raise ValueError("演示公网地址必须是准确的 https://设备名.tailnet.ts.net")
    return f"https://{parsed.hostname.lower()}"


def allowed_browser_origin(request: Request, public_origin: str) -> bool:
    origin = request.headers.get("origin")
    if origin == public_origin:
        return True
    host = request.headers.get("host", "").lower()
    hostname = host.split(":", 1)[0]
    if hostname in {"127.0.0.1", "localhost"}:
        return origin == f"{request.url.scheme}://{host}"
    return False


@dataclass(frozen=True)
class DemoSettings:
    enabled: bool = False
    public_origin: str = ""
    guest_hash: str = ""
    admin_hash: str = ""

    @property
    def single_password(self) -> bool:
        """单口令模式：未配置访客口令时，唯一口令登录后即为完整管理员。

        为什么保留 guest_hash 字段而不删掉：双角色（只读访客 + 管理员）的
        能力本身是有用的，删掉会伤到既有演示流程；这里只是让"未配置访客口令"
        自动降级为单口令，不引入账号系统，也不需要第二个口令。
        """
        return self.enabled and not self.guest_hash

    @classmethod
    def from_environment(cls) -> "DemoSettings":
        enabled = os.getenv("DOC_OCR_DEMO_MODE", "").lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return cls()
        origin = validated_public_origin(os.getenv("DOC_OCR_DEMO_PUBLIC_ORIGIN", ""))
        guest_hash = os.getenv("DOC_OCR_DEMO_GUEST_HASH", "")
        admin_hash = os.getenv("DOC_OCR_DEMO_ADMIN_HASH", "")
        if not valid_password_hash(admin_hash):
            raise ValueError("演示模式缺少有效的管理员口令哈希；先运行配置脚本")
        # 单口令模式：不配访客口令即视为"唯一口令"，登录后获得完整管理员权限。
        if guest_hash:
            if not valid_password_hash(guest_hash):
                raise ValueError("访客口令哈希无效；修正它或删除该项以启用单口令模式")
            if guest_hash == admin_hash:
                raise ValueError("访客与管理员口令必须不同")
        return cls(True, origin, guest_hash, admin_hash)


class DemoAuth:
    def __init__(self, settings: DemoSettings):
        self.settings = settings
        self._lock = threading.RLock()
        self._sessions: dict[str, tuple[str, float]] = {}
        self._failures: deque[float] = deque()
        self._active: dict[tuple[str, str], int] = {}
        self._global_active: dict[str, int] = {}
        self._qa_calls: dict[str, deque[float]] = {}

    def _clean(self, now: float) -> None:
        for key, (_, expiry) in list(self._sessions.items()):
            if expiry <= now:
                self._sessions.pop(key, None)
                self._qa_calls.pop(key, None)
        while self._failures and now - self._failures[0] >= 60:
            self._failures.popleft()

    def login(self, password: str) -> tuple[str, str] | None:
        if not self.settings.enabled:
            return None
        now = time.monotonic()
        with self._lock:
            self._clean(now)
            if len(self._failures) >= 20:
                raise RuntimeError("登录尝试过多，请一分钟后重试")
        # Both hashes are evaluated to keep the role-specific timing uniform.
        admin = verify_password(password, self.settings.admin_hash)
        if self.settings.single_password:
            # 单口令模式只有一个口令：校验通过即为完整管理员，不存在访客分支。
            role = "admin" if admin else None
        else:
            guest = verify_password(password, self.settings.guest_hash)
            role = "admin" if admin else "guest" if guest else None
        with self._lock:
            if role is None:
                self._failures.append(now)
                return None
            token = secrets.token_urlsafe(32)
            self._sessions[hashlib.sha256(token.encode()).hexdigest()] = (
                role, now + SESSION_SECONDS)
            return role, token

    def role_for(self, token: str | None) -> str | None:
        if not token:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        with self._lock:
            self._clean(now)
            item = self._sessions.get(key)
            return item[0] if item else None

    def logout(self, token: str | None) -> None:
        if not token:
            return
        key = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            self._sessions.pop(key, None)
            self._qa_calls.pop(key, None)

    def _owner_only(self, request: Request) -> bool:
        path, method = request.url.path, request.method
        if path in {"/docs", "/redoc", "/openapi.json"}:
            return True
        if path.startswith("/api/maintenance/"):
            return True
        if path == "/api/document/list":
            return request.query_params.get("in_recycle_bin", "").lower() in {"1", "true", "yes", "on"}
        if path in {"/api/rag/index", "/api/rag/index/retry"} and method == "POST":
            return True
        match = _DOC_ID_PATH.fullmatch(path)
        if match:
            suffix = match.group(2)
            return (method == "DELETE"
                    or (method == "POST" and suffix == "restore"))
        return False

    def _work_type(self, request: Request) -> str | None:
        if request.method != "POST":
            return None
        return {
            "/api/file/upload": "upload",
            "/api/ocr/recognize": "ocr",
            "/api/rag/ask": "qa",
        }.get(request.url.path)

    def begin_work(self, token: str, kind: str) -> bool:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        global_limits = {"upload": 2, "ocr": 2, "qa": 4}
        with self._lock:
            if self._active.get((key, kind), 0) >= 1:
                return False
            if self._global_active.get(kind, 0) >= global_limits[kind]:
                return False
            if kind == "qa":
                calls = self._qa_calls.setdefault(key, deque())
                while calls and now - calls[0] >= 60:
                    calls.popleft()
                if len(calls) >= 6:
                    return False
                calls.append(now)
            self._active[(key, kind)] = 1
            self._global_active[kind] = self._global_active.get(kind, 0) + 1
            return True

    def end_work(self, token: str, kind: str) -> None:
        key = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            self._active.pop((key, kind), None)
            self._global_active[kind] = max(0, self._global_active.get(kind, 0) - 1)

    async def middleware(self, request: Request, call_next):
        if not self.settings.enabled:
            request.state.demo_role = "admin"
            return await call_next(request)

        path = request.url.path
        if request.method == "OPTIONS" or path in {
                "/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/me"}:
            return await call_next(request)

        # Built frontend contains no document data; API routes and schema stay gated.
        if not path.startswith("/api/") and path not in {"/docs", "/redoc", "/openapi.json"}:
            return await call_next(request)

        token = request.cookies.get(COOKIE_NAME)
        role = self.role_for(token)
        if role is None:
            return JSONResponse(status_code=401, content={
                "code": 401, "msg": "请先登录后再访问", "data": None})
        request.state.demo_role = role
        if role != "admin" and self._owner_only(request):
            return JSONResponse(status_code=403, content={
                "code": 403, "msg": "此操作仅管理员可执行", "data": None})
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not allowed_browser_origin(request, self.settings.public_origin):
                return JSONResponse(status_code=403, content={
                    "code": 403, "msg": "请求来源未获允许", "data": None})

        kind = self._work_type(request)
        if kind and not self.begin_work(token, kind):
            return JSONResponse(status_code=429, content={
                "code": 429, "msg": "服务繁忙，请稍后再试", "data": None})
        try:
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        finally:
            if kind:
                self.end_work(token, kind)


DEMO_AUTH = DemoAuth(DemoSettings.from_environment())
