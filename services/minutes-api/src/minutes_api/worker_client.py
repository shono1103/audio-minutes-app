"""minutes-worker の内部 Claude 制御 API クライアント (contracts/internal/claude-control.md)。
応答の URL はそのまま呼び出し元へ返し、ログ・DB へ書かない。"""

from __future__ import annotations

from typing import Any

import httpx

from minutes_api.config import get_settings
from minutes_api.errors import ApiException


class WorkerControlClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 10.0) -> None:
        settings = get_settings()
        self._base = (base_url or settings.minutes_worker_url).rstrip("/")
        self._token = token if token is not None else settings.internal_token
        self._timeout = timeout

    def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        request_options: dict[str, Any] = {
            "headers": {"X-Internal-Token": self._token},
            "timeout": self._timeout,
        }
        if json_body is not None:
            request_options["json"] = json_body
        try:
            response = httpx.request(
                method,
                f"{self._base}{path}",
                **request_options,
            )
        except httpx.HTTPError as exc:
            raise ApiException(
                503, "internal", "minutes-worker に接続できません", stage="minutes", retryable=True
            ) from exc
        if response.status_code == 409:
            raise ApiException(409, "conflict", "別のログイン処理が進行中です", stage="minutes")
        if response.status_code == 404:
            raise ApiException(404, "not_found", "ログイン処理が見つかりません", stage="minutes")
        if response.status_code >= 400:
            raise ApiException(502, "internal", "minutes-worker がエラーを返しました", stage="minutes", retryable=True)
        try:
            return response.json()
        except ValueError as exc:
            raise ApiException(502, "internal", "minutes-worker の応答を解釈できません", stage="minutes") from exc

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/internal/v1/claude/status")

    def start_login(self) -> dict[str, Any]:
        return self._request("POST", "/internal/v1/claude/login")

    def login_state(self, auth_session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/internal/v1/claude/login/{auth_session_id}")

    def cancel_login(self, auth_session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/internal/v1/claude/login/{auth_session_id}/cancel")

    def submit_login_code(self, auth_session_id: str, code: str) -> dict[str, Any]:
        # codeはHTTP bodyで一度だけ中継し、このclientも呼出し側も保持・出力しない。
        return self._request("POST", f"/internal/v1/claude/login/{auth_session_id}/code", {"code": code})

    def logout(self) -> dict[str, Any]:
        return self._request("POST", "/internal/v1/claude/logout")
