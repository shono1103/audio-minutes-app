"""内部 Claude 制御 API (contracts/internal/claude-control.md)。Compose 内部ネットワークだけに公開する。

* `X-Internal-Token` が一致しない要求は 401。
* 認証 URL は `url_ready` の間だけ応答本文に含め、アクセスログ・アプリログへ出さない。
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from minutes_worker import __version__
from minutes_worker.claude_auth import ClaudeAuthAdapter, LoginInProgress, LoginNotFound
from minutes_worker.redaction import install_redaction

logger = logging.getLogger(__name__)
install_redaction(logger)


class LoginCodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=512)


def create_app(auth: ClaudeAuthAdapter, internal_token: str) -> FastAPI:
    if not internal_token:
        raise ValueError("AM_INTERNAL_TOKEN が設定されていません")

    app = FastAPI(title="minutes-worker internal control", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    def require_token(x_internal_token: str | None = Header(default=None)) -> None:
        if x_internal_token is None or not hmac.compare_digest(x_internal_token, internal_token):
            raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "internal token が一致しません"})

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, error: HTTPException) -> JSONResponse:
        detail = error.detail if isinstance(error.detail, dict) else {"code": "error", "message": str(error.detail)}
        return JSONResponse(status_code=error.status_code, content={"error": detail})

    @app.get("/internal/v1/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "worker": "minutes-worker", "version": __version__}

    @app.get("/internal/v1/claude/status", dependencies=[Depends(require_token)])
    async def status(force: bool = False) -> dict[str, Any]:
        return auth.status(force=force).to_dict()

    @app.post("/internal/v1/claude/login", status_code=201, dependencies=[Depends(require_token)])
    async def login() -> dict[str, Any]:
        try:
            session = auth.login()
        except LoginInProgress:
            raise HTTPException(status_code=409, detail={"code": "conflict", "message": "ログイン処理が既に進行中です"})
        body = session.public()
        body.pop("url", None)  # 作成応答には URL を含めない。GET で取得する
        return body

    @app.get("/internal/v1/claude/login/{auth_session_id}", dependencies=[Depends(require_token)])
    async def login_state(auth_session_id: str) -> dict[str, Any]:
        try:
            return auth.get_login(auth_session_id).public()
        except LoginNotFound:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "認証セッションがありません"})

    @app.post("/internal/v1/claude/login/{auth_session_id}/cancel", dependencies=[Depends(require_token)])
    async def cancel(auth_session_id: str) -> dict[str, Any]:
        try:
            session = auth.cancel_login(auth_session_id)
        except LoginNotFound:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "認証セッションがありません"})
        return {"auth_session_id": session.auth_session_id, "state": session.state}

    @app.post("/internal/v1/claude/login/{auth_session_id}/code", dependencies=[Depends(require_token)])
    async def submit_code(auth_session_id: str, body: LoginCodeBody) -> dict[str, Any]:
        """契約外の拡張: callback がコンテナへ届かない配置で owner が受け取ったコードを CLI へ渡す。"""
        try:
            session = auth.submit_login_code(auth_session_id, body.code)
        except LoginNotFound:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "認証セッションがありません"})
        except LoginInProgress as error:
            raise HTTPException(status_code=409, detail={"code": "conflict", "message": str(error)})
        return {"auth_session_id": session.auth_session_id, "state": session.state}

    @app.post("/internal/v1/claude/logout", dependencies=[Depends(require_token)])
    async def logout() -> dict[str, Any]:
        return auth.logout().to_dict()

    return app


__all__ = ["create_app"]
