"""error.v1 形式の例外とハンドラー。秘密・本文・ローカルパスを含めない。"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

from audio_minutes_contracts.ids import new_request_id
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

logger = logging.getLogger("minutes_api")
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


class ApiException(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        stage: str | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
        retained_artifacts: list[str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.stage = stage
        self.retryable = retryable
        self.details = details
        self.retained_artifacts = retained_artifacts or []
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "request_id": request_id_var.get() or new_request_id(),
            "retained_artifacts": self.retained_artifacts,
        }
        if self.stage is not None:
            error["stage"] = self.stage
        if self.retryable is not None:
            error["retryable"] = self.retryable
        if self.details:
            error["details"] = self.details
        return {"error": error}


def not_found(message: str = "見つかりません") -> ApiException:
    return ApiException(404, "not_found", message)


def forbidden(message: str = "この操作を行う権限がありません") -> ApiException:
    return ApiException(403, "forbidden", message)


def unauthorized(message: str = "認証が必要です") -> ApiException:
    return ApiException(401, "unauthorized", message, headers={"WWW-Authenticate": "Bearer"})


def invalid(message: str, details: dict[str, Any] | None = None) -> ApiException:
    return ApiException(400, "invalid_request", message, details=details)


def conflict(message: str, details: dict[str, Any] | None = None) -> ApiException:
    return ApiException(409, "conflict", message, details=details)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiException)
    async def _api_exception(request: Request, exc: ApiException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body(), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 入力エラーの位置だけを返し、送られた値は返さない
        locations = [
            "/".join(str(part) for part in error.get("loc", ())) + ": " + str(error.get("msg", ""))
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=400,
            content=ApiException(400, "invalid_request", "リクエストの形式が不正です", details={"errors": locations}).body(),
        )

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> JSONResponse:
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict", 429: "rate_limited"}.get(
            exc.status_code, "invalid_request" if exc.status_code < 500 else "internal"
        )
        message = exc.detail if isinstance(exc.detail, str) else "エラーが発生しました"
        return JSONResponse(
            status_code=exc.status_code, content=ApiException(exc.status_code, code, message).body(), headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error request_id=%s", request_id_var.get())
        return JSONResponse(
            status_code=500,
            content=ApiException(500, "internal", "内部エラーが発生しました", retryable=True).body(),
        )
