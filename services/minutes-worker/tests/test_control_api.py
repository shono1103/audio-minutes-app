from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from minutes_worker.claude_auth import ClaudeAuthAdapter
from minutes_worker.control_api import create_app
from tests.conftest import MOCK_BIN

TOKEN = "internal-secret"


@pytest.fixture
def client(claude_home, clean_environ):
    adapter = ClaudeAuthAdapter(
        MOCK_BIN, claude_home, environ=clean_environ, extra_env={"AM_MOCK_CLAUDE_SCENARIO": "login_url"}, status_cache_seconds=0
    )
    with TestClient(create_app(adapter, TOKEN)) as test_client:
        yield test_client
    adapter.close()


def headers() -> dict[str, str]:
    return {"X-Internal-Token": TOKEN}


def test_health_is_open_but_others_require_token(client):
    assert client.get("/internal/v1/health").json()["worker"] == "minutes-worker"
    assert client.get("/internal/v1/claude/status").status_code == 401
    assert client.get("/internal/v1/claude/status", headers={"X-Internal-Token": "wrong"}).status_code == 401
    assert client.post("/internal/v1/claude/login").status_code == 401


def test_login_flow(client):
    status = client.get("/internal/v1/claude/status", headers=headers()).json()
    assert status["state"] == "logged_out"
    created = client.post("/internal/v1/claude/login", headers=headers())
    assert created.status_code == 201
    body = created.json()
    assert "url" not in body and body["state"] == "pending"
    auth_id = body["auth_session_id"]
    assert client.post("/internal/v1/claude/login", headers=headers()).status_code == 409
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = client.get(f"/internal/v1/claude/login/{auth_id}", headers=headers()).json()
        if state["state"] == "url_ready":
            break
        time.sleep(0.05)
    assert state["state"] == "url_ready" and state["url"].startswith("https://claude.ai/")
    assert client.get("/internal/v1/claude/status", headers=headers()).json()["state"] == "login_pending"
    submitted = client.post(f"/internal/v1/claude/login/{auth_id}/code", headers=headers(), json={"code": "good-code"})
    assert submitted.status_code == 200
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = client.get(f"/internal/v1/claude/login/{auth_id}", headers=headers()).json()
        if state["state"] == "completed":
            break
        time.sleep(0.05)
    assert state["state"] == "completed" and "url" not in state
    assert client.get("/internal/v1/claude/status", headers=headers()).json()["state"] == "logged_in"
    assert client.post("/internal/v1/claude/logout", headers=headers()).json()["state"] == "logged_out"


def test_cancel_and_not_found(client):
    auth_id = client.post("/internal/v1/claude/login", headers=headers()).json()["auth_session_id"]
    assert client.post(f"/internal/v1/claude/login/{auth_id}/cancel", headers=headers()).json()["state"] == "cancelled"
    assert client.get("/internal/v1/claude/login/does-not-exist", headers=headers()).status_code == 404
    error = client.post("/internal/v1/claude/login/does-not-exist/cancel", headers=headers()).json()
    assert error["error"]["code"] == "not_found"
