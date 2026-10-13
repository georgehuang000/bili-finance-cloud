"""Server-side owner boundary for the public cloud app."""
import re

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import config
import db
import cloud_runtime as cloud

_PUBLIC = {"/api/uppers", "/api/summaries", "/api/research", "/api/status", "/api/health"}
_AUTHOR = re.compile(r"^/api/uppers/(\d{3,})/(videos|dynamics)$")


class OwnerLogin(BaseModel):
    password: str = Field(min_length=1, max_length=512)


def configure(app):
    @app.get("/api/health")
    def health():
        return {"ok": not config.CLOUD_MODE or cloud.ready(), "cloud": config.CLOUD_MODE, "test_mode": config.CLOUD_TEST_MODE if config.CLOUD_MODE else False}

    @app.get("/api/auth/session")
    def session(request: Request):
        return {"cloud": config.CLOUD_MODE, "can_manage": not config.CLOUD_MODE or cloud.is_owner(request), "test_mode": config.CLOUD_TEST_MODE if config.CLOUD_MODE else False}

    @app.post("/api/auth/login")
    def login(payload: OwnerLogin):
        if not config.CLOUD_MODE or not config.ADMIN_PASSWORD_HASH:
            return JSONResponse({"detail": "云端管理登录尚未配置"}, status_code=503)
        token, error = cloud.login(payload.password)
        if not token:
            return JSONResponse({"detail": error}, status_code=401)
        response = JSONResponse({"ok": True, "can_manage": True})
        response.set_cookie(cloud.COOKIE, token, max_age=8 * 3600, httponly=True, secure=True, samesite="strict", path="/")
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request):
        if config.CLOUD_MODE:
            cloud.logout(request)
        response = JSONResponse({"ok": True})
        response.delete_cookie(cloud.COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @app.get("/api/jobs")
    def jobs():
        return cloud.jobs() if config.CLOUD_MODE else []

    @app.middleware("http")
    async def boundary(request, call_next):
        if not config.CLOUD_MODE or not request.url.path.startswith("/api/"):
            return await call_next(request)
        path = request.url.path
        if path == "/api/health" or (path == "/api/auth/session" and not cloud.ready()):
            return await call_next(request)
        if not cloud.ready():
            return JSONResponse({"detail": "云端数据库尚未连接，请先完成 Neon 配置"}, status_code=503)
        if request.method not in ("GET", "HEAD", "OPTIONS") and not cloud.same_origin(request):
            return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
        owner = cloud.is_owner(request)
        request.state.owner = owner
        author = _AUTHOR.fullmatch(path)
        public_read = request.method == "GET" and (path in _PUBLIC or author)
        if not public_read and path not in ("/api/auth/login", "/api/auth/session") and not owner:
            return JSONResponse({"detail": "只有管理员可以执行此操作"}, status_code=401)
        if author and author.group(1) not in db.subscribed_uids():
            return JSONResponse({"detail": "该UP主未订阅"}, status_code=404)
        if path in ("/api/llm/test", "/api/llm/models", "/api/llm/settings", "/api/login/qrcode", "/api/login/qrcode/poll", "/api/login/status"):
            cloud.refresh_settings()
            if request.method == "POST" and path.startswith("/api/llm/"):
                try:
                    payload = await request.json()
                except ValueError:
                    return JSONResponse({"detail": "请求内容必须是JSON"}, status_code=400)
                if not isinstance(payload, dict):
                    return JSONResponse({"detail": "请求内容必须是对象"}, status_code=400)
                if payload.get("provider", "deepseek") != "deepseek" or (payload.get("base_url") and payload["base_url"].rstrip("/") not in ("https://api.deepseek.com", "https://api.deepseek.com/v1")):
                    return JSONResponse({"detail": "云端测试仅使用 DeepSeek 官方 API"}, status_code=400)
        response = await call_next(request)
        response.headers["Cache-Control"] = "private, no-store" if owner or path.startswith("/api/auth/") else "no-store"
        return response
