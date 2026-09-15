"""再実行の冪等性 (R07) と一覧 cursor の境界 (R08)。

jobs テーブルは PostgreSQL 専用なので、`jobs.enqueue` の UPSERT 意味論だけを
辞書で再現して差し替える。冪等キーの作り方とセッション状態の扱いを検証する。
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from conftest import auth_headers, issue_access_token, make_user
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minutes_api import jobs, models, sessions_service
from minutes_api.security import now


class FakeJobTable:
    """idempotency_key の UPSERT を辞書で再現する。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.keys: list[str] = []

    def enqueue(self, db, *, kind, session_id, owner_id, input, settings, idempotency_key, timeout_seconds, max_attempts=3):  # noqa: A002
        self.keys.append(idempotency_key)
        existing = self.rows.get(idempotency_key)
        if existing is not None:
            return jobs.EnqueuedJob(job_id=existing["job_id"], status=existing["status"], created=False)
        row = {"job_id": uuid.uuid4(), "status": "queued", "settings": settings, "kind": kind}
        self.rows[idempotency_key] = row
        return jobs.EnqueuedJob(job_id=row["job_id"], status="queued", created=True)

    def finish(self, key: str, status: str) -> None:
        self.rows[key]["status"] = status


@pytest.fixture
def fake_jobs(monkeypatch: pytest.MonkeyPatch) -> FakeJobTable:
    table = FakeJobTable()
    monkeypatch.setattr(jobs, "enqueue", table.enqueue)
    return table


def _finalized_session(db: Session, owner: models.User) -> models.MeetingSession:
    session = models.MeetingSession(
        id=uuid.uuid4(),
        owner_id=owner.id,
        title="試験セッション",
        input_kind="imported_mixed",
        language_mode="auto",
        package={"tracks": [{"track_id": "imported-audio", "start_offset_ms": 0}]},
        status="failed",
        started_at=datetime.datetime.now(datetime.UTC),
        finalized_at=now(),
        audio_retained=True,
    )
    db.add(session)
    db.flush()
    db.add(
        models.Artifact(
            id="art_01J7Q0AAAAAAAAAAAAAAAAAAA1", session_id=session.id, kind="audio_track",
            track_id="imported-audio", role="mixed", content_type="audio/wav", byte_size=10, sha256="0" * 64,
        )
    )
    db.commit()
    return session


def test_retry_after_failure_creates_a_new_job(client: TestClient, session_factory, fake_jobs: FakeJobTable) -> None:
    """失敗した文字起こしの再実行が、終了済み job の使い回しにならない。"""
    with session_factory() as db:
        user = make_user(db)
        db.commit()
        token = issue_access_token(db, user)
        session = _finalized_session(db, user)

    first = client.post(f"/v1/sessions/{session.id}/retry", json={"stage": "transcription"}, headers=auth_headers(token))
    assert first.status_code == 202
    first_job = first.json()["job_id"]
    fake_jobs.finish(fake_jobs.keys[0], "failed")

    with session_factory() as db:
        db.get(models.MeetingSession, session.id).status = "failed"
        db.commit()

    second = client.post(f"/v1/sessions/{session.id}/retry", json={"stage": "transcription"}, headers=auth_headers(token))
    assert second.status_code == 202
    assert second.json()["job_id"] != first_job, "終了済み job を返して queued 扱いにしている"
    assert fake_jobs.keys[0] != fake_jobs.keys[1], "同じ冪等キーで再投入しようとしている"
    assert second.json()["session"]["status"] == "queued"


def test_same_http_request_resent_does_not_duplicate(client: TestClient, session_factory, fake_jobs: FakeJobTable) -> None:
    with session_factory() as db:
        user = make_user(db)
        db.commit()
        token = issue_access_token(db, user)
        session = _finalized_session(db, user)

    headers = {**auth_headers(token), "Idempotency-Key": "client-retry-1"}
    first = client.post(f"/v1/sessions/{session.id}/retry", json={"stage": "transcription"}, headers=headers)
    with session_factory() as db:
        db.get(models.MeetingSession, session.id).status = "failed"
        db.commit()
    second = client.post(f"/v1/sessions/{session.id}/retry", json={"stage": "transcription"}, headers=headers)

    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert len(fake_jobs.rows) == 1, "同じ HTTP 要求の再送で job が増えている"


def test_language_change_is_reflected_in_the_new_job(client: TestClient, session_factory, fake_jobs: FakeJobTable) -> None:
    with session_factory() as db:
        user = make_user(db)
        db.commit()
        token = issue_access_token(db, user)
        session = _finalized_session(db, user)

    client.post(f"/v1/sessions/{session.id}/retry", json={"stage": "transcription"}, headers=auth_headers(token))
    with session_factory() as db:
        db.get(models.MeetingSession, session.id).status = "failed"
        db.commit()
    response = client.post(
        f"/v1/sessions/{session.id}/retry", json={"stage": "transcription", "language_mode": "ja"},
        headers=auth_headers(token),
    )
    assert response.status_code == 202
    latest = fake_jobs.rows[fake_jobs.keys[-1]]
    assert latest["settings"]["language_mode"] == "ja", "新しい言語設定が job に反映されていない"


def test_session_owner_can_request_job_cancel(client: TestClient, session_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    with session_factory() as db:
        user = make_user(db)
        db.commit()
        token = issue_access_token(db, user)
        session = _finalized_session(db, user)
    job_id = uuid.uuid4()
    observed: list[tuple[uuid.UUID, uuid.UUID]] = []

    def fake_cancel(db, session_id, requested_job_id):  # noqa: ANN001
        observed.append((session_id, requested_job_id))
        return {
            "job_id": job_id,
            "kind": "transcription",
            "session_id": session_id,
            "status": "running",
            "attempt": 1,
            "max_attempts": 3,
            "cancel_requested": True,
            "failure": None,
            "created_at": now(),
            "started_at": now(),
            "finished_at": None,
        }

    monkeypatch.setattr(jobs, "request_cancel", fake_cancel)
    response = client.post(
        f"/v1/sessions/{session.id}/jobs/{job_id}/cancel",
        headers=auth_headers(token),
    )
    assert response.status_code == 202
    assert response.json()["job_id"] == str(job_id)
    assert response.json()["cancel_requested"] is True
    assert observed == [(session.id, job_id)]


def test_shared_user_cannot_cancel_job(client: TestClient, session_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    with session_factory() as db:
        owner = make_user(db)
        shared = make_user(db, "shared@example.test", role="member")
        session = _finalized_session(db, owner)
        db.add(models.SessionShare(session_id=session.id, user_id=shared.id))
        db.commit()
        token = issue_access_token(db, shared)
    called = False

    def fake_cancel(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal called
        called = True

    monkeypatch.setattr(jobs, "request_cancel", fake_cancel)
    response = client.post(
        f"/v1/sessions/{session.id}/jobs/{uuid.uuid4()}/cancel",
        headers=auth_headers(token),
    )
    assert response.status_code == 403
    assert called is False


def test_cancel_unknown_job_is_not_found(client: TestClient, session_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    with session_factory() as db:
        user = make_user(db)
        db.commit()
        token = issue_access_token(db, user)
        session = _finalized_session(db, user)
    monkeypatch.setattr(jobs, "request_cancel", lambda *args, **kwargs: None)
    response = client.post(
        f"/v1/sessions/{session.id}/jobs/{uuid.uuid4()}/cancel",
        headers=auth_headers(token),
    )
    assert response.status_code == 404


def test_transcript_revision_follows_the_last_success_not_the_attempt(session_factory, fake_jobs: FakeJobTable) -> None:
    """成功済み revision・要求世代・attempt の役割が分かれている。"""
    with session_factory() as db:
        user = make_user(db)
        db.commit()
        session = _finalized_session(db, user)
        db.add(
            models.Transcript(session_id=session.id, revision=1, json_artifact_id="art_01J7Q0AAAAAAAAAAAAAAAAAAA2")
        )
        db.commit()

        sessions_service.enqueue_transcription(db, session)
        sessions_service.enqueue_transcription(db, session)
        db.commit()

    settings = [fake_jobs.rows[key]["settings"] for key in fake_jobs.keys]
    assert [item["transcript_revision"] for item in settings] == [2, 2], "成功 revision は変わらない"
    assert fake_jobs.keys[0] != fake_jobs.keys[1], "要求世代が進んでいない"
    assert fake_jobs.keys[0].endswith(":gen:1") and fake_jobs.keys[1].endswith(":gen:2")


def _seed_sessions(db: Session, user: models.User, count: int, *, same_time: bool) -> None:
    base = datetime.datetime(2026, 9, 12, 10, 0, tzinfo=datetime.UTC)
    for index in range(count):
        db.add(
            models.MeetingSession(
                id=uuid.uuid4(), owner_id=user.id, title=f"s{index}", input_kind="imported_mixed",
                language_mode="auto", package={"tracks": []}, status="uploading", started_at=base,
                created_at=base if same_time else base + datetime.timedelta(minutes=index),
            )
        )
    db.commit()


def _walk(client: TestClient, path: str, token: str, limit: int, key: str) -> list:
    seen: list = []
    cursor = None
    for _ in range(50):
        query = {"limit": limit} | ({"cursor": cursor} if cursor is not None else {})
        page = client.get(path, params=query, headers=auth_headers(token)).json()
        seen.extend(item[key] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    return seen


@pytest.mark.parametrize("limit", [1, 2, 5])
@pytest.mark.parametrize("same_time", [False, True])
def test_session_list_cursor_does_not_skip_a_row(
    client: TestClient, session_factory, limit: int, same_time: bool
) -> None:
    """cursor 境界で 1 件も欠落・重複しない (R08)。

    ちょうど limit 件 (limit=5 で 5 件) と、created_at が同じ行の並びも通す。
    """
    with session_factory() as db:
        user = make_user(db)
        db.commit()
        token = issue_access_token(db, user)
        _seed_sessions(db, user, 5, same_time=same_time)

    seen = _walk(client, "/v1/sessions", token, limit, "session_id")
    assert len(seen) == 5, f"limit={limit} same_time={same_time}: 走査できたのは {len(seen)} 件"
    assert len(set(seen)) == 5, f"limit={limit} same_time={same_time}: 重複している"


@pytest.mark.parametrize("limit", [1, 2, 5])
def test_audit_cursor_does_not_skip_a_row(client: TestClient, session_factory, limit: int) -> None:
    from minutes_api import audit

    with session_factory() as db:
        user = make_user(db)
        db.commit()
        token = issue_access_token(db, user)
        before = len(client.get("/v1/admin/audit", params={"limit": 500}, headers=auth_headers(token)).json()["items"])
        for index in range(5):
            audit.record(db, f"test_action_{index}", actor_id=user.id)
        db.commit()

    seen = _walk(client, "/v1/admin/audit", token, limit, "id")
    assert len(seen) == len(set(seen)), f"limit={limit}: 監査一覧が重複している"
    assert len(seen) >= before + 5, f"limit={limit}: 監査一覧に欠落がある ({len(seen)} 件)"
