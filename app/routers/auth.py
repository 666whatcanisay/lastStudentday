"""Login and session endpoints for the temporary public demo mode."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.services.demo_auth import (
    COOKIE_NAME, DEMO_AUTH, SESSION_SECONDS, allowed_browser_origin,
)

router = APIRouter(prefix="/api/auth", tags=["演示访问"])


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


def _response(code: int, msg: str, data=None, status: int = 200):
    return JSONResponse(status_code=status, content={"code": code, "msg": msg, "data": data})


@router.get("/me")
def me(request: Request):
    if not DEMO_AUTH.settings.enabled:
        response = _response(200, "success", {
            "demo_mode": False, "authenticated": True, "role": "admin"})
        response.headers["Cache-Control"] = "no-store"
        return response
    role = DEMO_AUTH.role_for(request.cookies.get(COOKIE_NAME))
    response = _response(200, "success", {
        "demo_mode": True, "authenticated": bool(role), "role": role})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/login")
def login(body: LoginRequest, request: Request):
    if not DEMO_AUTH.settings.enabled:
        return _response(400, "当前未开启演示登录", status=400)
    if not allowed_browser_origin(request, DEMO_AUTH.settings.public_origin):
        return _response(403, "请求来源未获允许", status=403)
    try:
        result = DEMO_AUTH.login(body.password)
    except RuntimeError as exc:
        return _response(429, str(exc), status=429)
    if result is None:
        return _response(401, "口令不正确", status=401)
    role, token = result
    response = _response(200, "登录成功", {
        "demo_mode": True, "authenticated": True, "role": role})
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_SECONDS, path="/",
        httponly=True, secure=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/logout")
def logout(request: Request):
    DEMO_AUTH.logout(request.cookies.get(COOKIE_NAME))
    response = _response(200, "已退出", {
        "demo_mode": DEMO_AUTH.settings.enabled,
        "authenticated": not DEMO_AUTH.settings.enabled,
        "role": None if DEMO_AUTH.settings.enabled else "admin"})
    response.delete_cookie(COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response
