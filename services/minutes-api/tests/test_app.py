from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minutes_api.app import create_app
from minutes_api.config import Settings, reset_settings
from minutes_api.db import db_dependency
from minutes_api.routers.account import (
    BrowserReauthStarted,
    BrowserReauthStatus,
    PasskeyBrowserStarted,
    PasskeyBrowserStatus,
)

FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "fixtures" / "api"


class BrokenDatabase:
    def execute(self, statement):
        raise RuntimeError("DB unavailable")

    def rollback(self) -> None:
        pass


def broken_database():
    yield BrokenDatabase()


@pytest.fixture(autouse=True)
def local_storage(tmp_path):
    reset_settings(
        Settings(
            artifacts_dir=tmp_path / "artifacts",
            uploads_dir=tmp_path / "uploads",
            log_dir=tmp_path / "logs",
            background_tasks=False,
        )
    )
    yield
    reset_settings()


def test_health_is_available_and_does_not_expose_database_error() -> None:
    app = create_app()
    app.dependency_overrides[db_dependency] = broken_database
    with TestClient(app) as client:
        response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "version": "0.1.0"}
    assert response.headers["x-request-id"].startswith("req_")
    assert response.headers["cache-control"] == "no-store"


def test_update_maintenance_allows_health_and_rejects_other_routes(tmp_path: Path) -> None:
    settings = reset_settings(
        Settings(
            artifacts_dir=tmp_path / "artifacts",
            uploads_dir=tmp_path / "uploads",
            log_dir=tmp_path / "logs",
            update_maintenance_file=tmp_path / "logs/.update-maintenance",
            background_tasks=False,
        )
    )
    settings.update_maintenance_file.parent.mkdir(parents=True)
    settings.update_maintenance_file.touch()
    app = create_app()
    app.dependency_overrides[db_dependency] = broken_database
    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 200
        blocked = client.get("/docs")
        assert blocked.status_code == 503
        assert blocked.json()["error"]["code"] == "update_in_progress"
        assert blocked.headers["retry-after"] == "10"


def test_openapi_contains_core_routes() -> None:
    paths = create_app().openapi()["paths"]
    for expected in (
        "/v1/health",
        "/oauth/token",
        "/v1/auth/reauth/browser",
        "/v1/auth/reauth/browser/{request_id}",
        "/v1/account/passkeys/browser",
        "/v1/account/passkeys/browser/{request_id}",
        "/v1/admin/claude/login/{auth_session_id}/code",
        "/v1/sessions",
        "/v1/uploads/{upload_id}",
    ):
        assert expected in paths


def test_http_errors_use_versioned_error_shape() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/not-found")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


def test_browser_reauth_fixtures_match_response_models() -> None:
    started = json.loads((FIXTURES / "browser-reauth-started.json").read_text(encoding="utf-8"))
    completed = json.loads((FIXTURES / "browser-reauth-completed.json").read_text(encoding="utf-8"))
    assert BrowserReauthStarted.model_validate(started).status == "pending"
    assert BrowserReauthStatus.model_validate(completed).status == "completed"


def test_passkey_browser_fixtures_match_response_models() -> None:
    started = json.loads((FIXTURES / "passkey-browser-started.json").read_text(encoding="utf-8"))
    completed = json.loads((FIXTURES / "passkey-browser-completed.json").read_text(encoding="utf-8"))
    assert PasskeyBrowserStarted.model_validate(started).status == "pending"
    assert PasskeyBrowserStatus.model_validate(completed).status == "completed"
