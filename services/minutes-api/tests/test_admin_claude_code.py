from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy import select

from minutes_api import accounts, models
from minutes_api.routers import admin
from minutes_api.security import now, token_hash
from minutes_api.worker_client import WorkerControlClient


class FakeWorkerControl:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, str]] = []
        self.state_result = {"state": "url_ready", "expires_at": now().isoformat()}

    def submit_login_code(self, auth_session_id: str, code: str) -> dict:
        self.submitted.append((auth_session_id, code))
        return {"auth_session_id": auth_session_id, "state": "url_ready"}

    def login_state(self, auth_session_id: str) -> dict:
        return {"auth_session_id": auth_session_id, **self.state_result}


def test_worker_control_client_sends_code_only_in_json_body(monkeypatch) -> None:
    captured = {}

    def request(method, url, **options):
        captured.update(method=method, url=url, options=options)
        return httpx.Response(200, json={"auth_session_id": "auth-1", "state": "url_ready"})

    monkeypatch.setattr(httpx, "request", request)
    result = WorkerControlClient(base_url="http://worker", token="internal", timeout=2).submit_login_code(
        "auth-1", "one-time-code"
    )
    assert result["state"] == "url_ready"
    assert captured["url"] == "http://worker/internal/v1/claude/login/auth-1/code"
    assert captured["options"]["json"] == {"code": "one-time-code"}
    assert "one-time-code" not in captured["url"]


def _owner_with_reauth(db):
    from conftest import issue_access_token

    owner, _ = accounts.create_user(
        db,
        email="claude-code-owner@example.test",
        role="owner",
        password="correct horse battery staple 2026",
    )
    db.commit()
    raw = issue_access_token(db, owner)
    access = db.execute(
        select(models.AccessToken).where(models.AccessToken.token_hash == token_hash(raw))
    ).scalars().one()
    db.add(
        models.ReauthGrant(
            user_id=owner.id,
            access_token_id=access.id,
            expires_at=now() + timedelta(minutes=5),
        )
    )
    db.commit()
    return owner, raw


def test_claude_login_code_requires_request_owner_and_reauth_without_persisting_code(
    client, db, monkeypatch
) -> None:
    from conftest import auth_headers

    owner, access = _owner_with_reauth(db)
    row = models.ClaudeAuthSession(
        id="auth-session-1",
        requested_by=owner.id,
        state="url_ready",
        expires_at=now() + timedelta(minutes=5),
    )
    db.add(row)
    db.commit()
    worker = FakeWorkerControl()
    monkeypatch.setattr(admin, "WorkerControlClient", lambda: worker)
    secret_code = "authorization-code-must-not-persist"

    response = client.post(
        f"/v1/admin/claude/login/{row.id}/code",
        headers=auth_headers(access),
        json={"code": secret_code},
    )
    assert response.status_code == 200
    assert response.json() == {"auth_session_id": row.id, "state": "url_ready"}
    assert worker.submitted == [(row.id, secret_code)]

    db.expire_all()
    stored = db.get(models.ClaudeAuthSession, row.id)
    assert secret_code not in repr(stored.__dict__)
    audits = list(
        db.execute(select(models.AuditLog).where(models.AuditLog.action == "claude_login_code_submitted")).scalars()
    )
    assert len(audits) == 1 and audits[0].metadata_ is None
    assert secret_code not in repr(audits[0].__dict__)


def test_claude_login_code_rejects_other_owner(client, db, monkeypatch) -> None:
    from conftest import auth_headers, issue_access_token, make_user

    requester = make_user(db, "requester@example.test")
    other = make_user(db, "other-owner@example.test")
    db.commit()
    other_access = issue_access_token(db, other)
    token_row = db.execute(
        select(models.AccessToken).where(models.AccessToken.token_hash == token_hash(other_access))
    ).scalars().one()
    db.add(
        models.ReauthGrant(
            user_id=other.id,
            access_token_id=token_row.id,
            expires_at=now() + timedelta(minutes=5),
        )
    )
    db.add(
        models.ClaudeAuthSession(
            id="auth-session-other",
            requested_by=requester.id,
            state="url_ready",
            expires_at=now() + timedelta(minutes=5),
        )
    )
    db.commit()
    worker = FakeWorkerControl()
    monkeypatch.setattr(admin, "WorkerControlClient", lambda: worker)

    response = client.post(
        "/v1/admin/claude/login/auth-session-other/code",
        headers=auth_headers(other_access),
        json={"code": "must-not-forward"},
    )
    assert response.status_code == 404 and worker.submitted == []


def test_claude_login_code_requires_recent_reauth(client, db, monkeypatch) -> None:
    from conftest import auth_headers, issue_access_token, make_user

    owner = make_user(db, "without-reauth@example.test")
    db.commit()
    access = issue_access_token(db, owner)
    db.add(
        models.ClaudeAuthSession(
            id="auth-session-no-reauth",
            requested_by=owner.id,
            state="url_ready",
            expires_at=now() + timedelta(minutes=5),
        )
    )
    db.commit()
    worker = FakeWorkerControl()
    monkeypatch.setattr(admin, "WorkerControlClient", lambda: worker)

    response = client.post(
        "/v1/admin/claude/login/auth-session-no-reauth/code",
        headers=auth_headers(access),
        json={"code": "must-not-forward"},
    )
    assert response.status_code == 403 and worker.submitted == []


def test_claude_login_state_forwards_failure_code_without_persisting_it(client, db, monkeypatch) -> None:
    from conftest import auth_headers

    owner, access = _owner_with_reauth(db)
    row = models.ClaudeAuthSession(
        id="auth-session-failed",
        requested_by=owner.id,
        state="pending",
        expires_at=now() + timedelta(minutes=5),
    )
    db.add(row)
    db.commit()
    worker = FakeWorkerControl()
    worker.state_result = {
        "state": "failed",
        "expires_at": row.expires_at.isoformat(),
        "failure_code": "claude_not_authenticated",
    }
    monkeypatch.setattr(admin, "WorkerControlClient", lambda: worker)

    response = client.get(f"/v1/admin/claude/login/{row.id}", headers=auth_headers(access))
    assert response.status_code == 200
    assert response.json()["failure_code"] == "claude_not_authenticated"
    db.expire_all()
    stored = db.get(models.ClaudeAuthSession, row.id)
    assert stored.state == "failed" and "failure_code" not in stored.__dict__
