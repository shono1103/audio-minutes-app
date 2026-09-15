"""FastAPI アプリケーションの組み立てと起動入口。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from audio_minutes_contracts.ids import new_request_id
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from minutes_api import __version__
from minutes_api.background import create_tasks
from minutes_api.config import get_settings
from minutes_api.errors import install_error_handlers, request_id_var
from minutes_api.routers import (
    account,
    admin,
    artifacts,
    auth_web,
    formats,
    health,
    minutes,
    oauth,
    sessions,
    uploads,
)


class _RejectUvicornWebSocketRequestLog(logging.Filter):
    """path/query を含む Uvicorn の WebSocket handshake record だけを捨てる。"""

    audio_minutes_websocket_request_filter = True
    _request_messages = {
        '%s - "WebSocket %s" [accepted]',
        '%s - "WebSocket %s" 403',
        '%s - "WebSocket %s" %d',
    }

    def filter(self, record: logging.LogRecord) -> bool:
        return not isinstance(record.msg, str) or record.msg not in self._request_messages


def _disable_uvicorn_access_log() -> None:
    """request target に含まれる一回限り token を生 access log へ出さない。"""
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.disabled = True
    access_logger.propagate = False
    access_logger.handlers.clear()

    # Uvicorn は WebSocket handshake だけを uvicorn.error の INFO へ出す。
    # 一般的な起動・停止・例外ログは維持し、request target を持つ既知recordだけを除く。
    error_logger = logging.getLogger("uvicorn.error")
    if not any(
        getattr(item, "audio_minutes_websocket_request_filter", False)
        for item in error_logger.filters
    ):
        error_logger.addFilter(_RejectUvicornWebSocketRequestLog())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """必要なローカル保存先だけを作る。DB schema は migration が所有する。"""
    _disable_uvicorn_access_log()
    settings = get_settings()
    for path in (settings.artifacts_dir, settings.uploads_dir, settings.log_dir):
        path.mkdir(parents=True, exist_ok=True)
    tasks = create_tasks(settings) if settings.background_tasks else []
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def create_app() -> FastAPI:
    # `python -m uvicorn minutes_api.app:app` は Config 構築後にこの module を読む。
    # CLI flag が無い直接起動でも fail closed にし、再生成された app にも同じ方針を適用する。
    _disable_uvicorn_access_log()
    settings = get_settings()
    app = FastAPI(
        title="audio-minutes API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Tus-Resumable", "Upload-Length", "Upload-Offset", "Upload-Metadata", "X-CSRF-Token"],
        expose_headers=["Location", "Tus-Resumable", "Upload-Expires", "Upload-Length", "Upload-Offset", "X-Request-ID"],
    )

    @app.middleware("http")
    async def update_maintenance(request: Request, call_next):
        # update.sh の検証中は health だけを公開する。marker は共有 volume 上にあり、
        # readiness 成功後の削除が write services 全体の atomic な commit point になる。
        if settings.update_maintenance_file.exists() and request.url.path != "/v1/health":
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "update_in_progress", "message": "更新中です。しばらく待って再試行してください"}},
                headers={"Retry-After": "10", "Cache-Control": "no-store"},
            )
        return await call_next(request)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or new_request_id()
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            response.headers.setdefault("Cache-Control", "no-store")
            return response
        finally:
            request_id_var.reset(token)

    @app.middleware("http")
    async def keep_access_log_disabled(request: Request, call_next):
        # 最後に登録した middleware は最外周で動く。Uvicorn の access record は response 後に
        # 生成されるため、外部 logging 設定で logger が作り直されても記録前に再度抑止する。
        _disable_uvicorn_access_log()
        return await call_next(request)

    install_error_handlers(app)
    for router in (
        health.router,
        auth_web.router,
        oauth.router,
        account.router,
        admin.router,
        sessions.router,
        uploads.router,
        artifacts.router,
        minutes.router,
        formats.router,
    ):
        app.include_router(router)
    return app


app = create_app()


def main() -> None:
    uvicorn.run(
        "minutes_api.app:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        access_log=False,
        ws="none",
    )
