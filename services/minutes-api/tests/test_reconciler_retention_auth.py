from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from audio_minutes_contracts.models import JobKind, WorkerResult, WorkerResultArtifact
from sqlalchemy import func, select

from minutes_api import accounts, jobs, models, reconciler, retention, sessions_service
from minutes_api.errors import ApiException
from minutes_api.security import now, token_hash

FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "fixtures"
TRANSCRIPT_SESSION_ID = uuid.UUID("7c9b2b1e-6b6d-4f7a-9d3e-1a2b3c4d5e6f")


def _session(db, owner, *, session_id: uuid.UUID | None = None, allow_external_send: bool = False):
    row = models.MeetingSession(
        id=session_id or uuid.uuid4(),
        owner_id=owner.id,
        title="試験会議",
        input_kind="recorded_dual_track",
        language_mode="auto",
        allow_external_send=allow_external_send,
        package={
            "tracks": [
                {"track_id": "app-audio", "start_offset_ms": 0},
                {"track_id": "microphone", "start_offset_ms": 12},
            ]
        },
        status="transcribing",
        started_at=datetime(2026, 9, 12, 10, 0, tzinfo=UTC),
        duration_ms=1_800_000,
        finalized_at=now(),
    )
    db.add(row)
    db.flush()
    return row


def _result_artifact(store, data: bytes, kind: str) -> WorkerResultArtifact:
    stored = store.put_bytes(data)
    return WorkerResultArtifact(
        artifact_id=stored.artifact_id,
        kind=kind,
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        content_type="application/json" if kind == "transcript_json" else "text/markdown",
    )


def test_transcription_result_is_reconciled_idempotently(db, settings, session_factory) -> None:
    from conftest import make_user

    owner = make_user(db)
    session = _session(db, owner, session_id=TRANSCRIPT_SESSION_ID)
    job_id = uuid.uuid4()
    session.transcription_job_id = job_id
    store = sessions_service.artifact_store(settings)
    json_result = _result_artifact(store, (FIXTURES / "transcript.dual-track.json").read_bytes(), "transcript_json")
    md_result = _result_artifact(store, b"# transcript\n", "transcript_md")
    job = {
        "job_id": job_id,
        "kind": "transcription",
        "session_id": session.id,
        "status": "succeeded",
        "settings": {"transcript_revision": 1},
        "input": {
            "artifacts": [
                {"artifact_id": "art_01J7Q0AAAAAAAAAAAAAAAAAAA1", "kind": "audio_track"},
                {"artifact_id": "art_01J7Q0AAAAAAAAAAAAAAAAAAA2", "kind": "audio_track"},
            ]
        },
        "result": json.loads(
            WorkerResult(kind=JobKind.TRANSCRIPTION, artifacts=[json_result, md_result]).model_dump_json()
        ),
        "failure": None,
    }

    reconciler.reconcile_job(db, job)
    reconciler.reconcile_job(db, job)
    db.flush()

    assert db.execute(select(func.count()).select_from(models.Transcript)).scalar_one() == 1
    assert db.execute(select(func.count()).select_from(models.Artifact)).scalar_one() == 2
    assert session.status == "transcribed" and session.transcript_revision == 1
    assert session.review_count == 0


def test_old_or_deleted_job_result_is_never_published(db, settings) -> None:
    from conftest import make_user

    owner = make_user(db)
    session = _session(db, owner)
    session.transcription_job_id = uuid.uuid4()
    stale_job = {
        "job_id": uuid.uuid4(), "kind": "transcription", "session_id": session.id, "status": "succeeded",
        "settings": {}, "input": {}, "result": {}, "failure": None,
    }
    reconciler.reconcile_job(db, stale_job)
    session.deleted_at = now()
    session.transcription_job_id = stale_job["job_id"]
    reconciler.reconcile_job(db, stale_job)
    assert db.execute(select(func.count()).select_from(models.Transcript)).scalar_one() == 0
    assert db.execute(select(func.count()).select_from(models.Artifact)).scalar_one() == 0


def test_minutes_unknown_stops_without_automatic_result(db) -> None:
    from conftest import make_user

    owner = make_user(db)
    session = _session(db, owner)
    job_id = uuid.uuid4()
    session.minutes_job_id = job_id
    job = {
        "job_id": job_id, "kind": "minutes_generation", "session_id": session.id, "status": "succeeded",
        "settings": {}, "input": {},
        "result": json.loads(WorkerResult(kind=JobKind.MINUTES_GENERATION, outcome="unknown", artifacts=[]).model_dump_json()),
        "failure": None,
    }
    reconciler.reconcile_job(db, job)
    assert session.status == "failed"
    assert session.failure["code"] == "claude_unknown_outcome" and session.failure["retryable"] is False
    assert db.execute(select(func.count()).select_from(models.MinutesVersion)).scalar_one() == 0


def test_generated_minutes_becomes_candidate_when_manual_version_won(db, settings) -> None:
    from conftest import make_user

    owner = make_user(db)
    session = _session(db, owner)
    store = sessions_service.artifact_store(settings)
    old = _result_artifact(store, b"# manual\n", "minutes_md")
    db.add(models.Artifact(id=old.artifact_id, session_id=session.id, kind="minutes_md", content_type="text/markdown", byte_size=old.byte_size, sha256=old.sha256))
    current = models.MinutesVersion(
        session_id=session.id, version_number=1, kind="manual_edit", created_by=str(owner.id), artifact_id=old.artifact_id
    )
    db.add(current)
    db.flush()
    session.current_minutes_version_id = current.id
    job_id = uuid.uuid4()
    session.minutes_job_id = job_id
    generated = _result_artifact(store, b"# generated\n", "minutes_md")
    job = {
        "job_id": job_id, "kind": "minutes_generation", "session_id": session.id, "status": "succeeded",
        "settings": {"kind": "claude_generated", "expected_current_version_id": None, "transcript_revision": 1},
        "input": {"parent_minutes_version_id": None},
        "result": json.loads(
            WorkerResult(kind=JobKind.MINUTES_GENERATION, artifacts=[generated], title_proposal="提案").model_dump_json()
        ),
        "failure": None,
    }
    reconciler.reconcile_job(db, job)
    version = db.execute(select(models.MinutesVersion).where(models.MinutesVersion.job_id == job_id)).scalar_one()
    assert version.is_candidate is True
    assert session.current_minutes_version_id == current.id
    assert session.title == "試験会議"


def test_retention_expires_upload_and_audio_but_keeps_transcript_and_minutes(db, settings) -> None:
    from conftest import make_user

    owner = make_user(db)
    store = sessions_service.artifact_store(settings)
    session = _session(db, owner)
    session.status = "completed"
    session.audio_expires_at = now() - timedelta(seconds=1)
    audio = _result_artifact(store, b"audio", "minutes_md")
    transcript = _result_artifact(store, b"transcript", "minutes_md")
    minutes = _result_artifact(store, b"minutes", "minutes_md")
    rows = [
        models.Artifact(id=audio.artifact_id, session_id=session.id, kind="audio_track", content_type="audio/wav", byte_size=audio.byte_size, sha256=audio.sha256),
        models.Artifact(id=transcript.artifact_id, session_id=session.id, kind="transcript_json", content_type="application/json", byte_size=transcript.byte_size, sha256=transcript.sha256),
        models.Artifact(id=minutes.artifact_id, session_id=session.id, kind="minutes_md", content_type="text/markdown", byte_size=minutes.byte_size, sha256=minutes.sha256),
    ]
    db.add_all(rows)

    pending = models.MeetingSession(
        id=uuid.uuid4(), owner_id=owner.id, title="pending", input_kind="imported_mixed", language_mode="auto",
        package={"tracks": []}, status="uploading", started_at=now(),
    )
    db.add(pending)
    db.flush()
    upload = models.Upload(
        session_id=pending.id, track_id="imported-audio", role="mixed", expected_length=1, expected_sha256="0" * 64,
        state="uploading", expires_at=now() - timedelta(seconds=1),
    )
    db.add(upload)
    db.flush()
    part = sessions_service.upload_part_path(upload.id, settings)
    part.write_bytes(b"x")

    plan = retention.plan_sweep(db, active_transcription_sessions=set())
    db.commit()
    retention.delete_planned_files(plan, settings)

    assert session.audio_retained is False and rows[0].deleted_at is not None
    assert not store.exists(audio.artifact_id)
    assert store.exists(transcript.artifact_id) and store.exists(minutes.artifact_id)
    assert upload.state == "expired" and pending.failure["code"] == "upload_expired" and not part.exists()


def test_active_upload_claim_and_transcription_protect_files(db, settings) -> None:
    from conftest import make_user

    owner = make_user(db)
    session = _session(db, owner)
    session.audio_expires_at = now() - timedelta(days=1)
    session.status = "transcribing"
    store = sessions_service.artifact_store(settings)
    stored = store.put_bytes(b"audio")
    audio = models.Artifact(
        id=stored.artifact_id, session_id=session.id, kind="audio_track", content_type="audio/wav",
        byte_size=stored.byte_size, sha256=stored.sha256,
    )
    db.add(audio)
    plan = retention.plan_sweep(db, active_transcription_sessions={session.id})
    assert plan.artifact_ids == () and session.audio_retained is True


def test_temp_sweep_removes_only_unreferenced_old_upload_parts(db, settings) -> None:
    from conftest import make_upload, make_user

    owner = make_user(db)
    _, upload = make_upload(db, owner, length=1)
    referenced = sessions_service.upload_part_path(upload.id, settings)
    referenced.write_bytes(b"kept")
    orphan = settings.uploads_dir / f"{uuid.uuid4()}.part"
    orphan.write_bytes(b"removed")
    old = (now() - timedelta(days=2)).timestamp()
    os.utime(referenced, (old, old))
    os.utime(orphan, (old, old))

    removed = retention.sweep_temp_files(db, settings, older_than_seconds=3600)

    assert removed == 1
    assert referenced.exists()
    assert not orphan.exists()


def test_log_sweep_uses_db_policy_and_removes_worker_archive(settings) -> None:
    worker_log = settings.log_dir / "stdout/minutes-worker/old.log"
    worker_log.parent.mkdir(parents=True)
    worker_log.write_text("worker\n", encoding="utf-8")
    marker = settings.log_dir / ".update-maintenance"
    marker.write_text("", encoding="utf-8")
    old = (now() - timedelta(days=31)).timestamp()
    os.utime(worker_log, (old, old))
    os.utime(marker, (old, old))

    removed = retention.sweep_log_files(
        settings,
        retention.RetentionPolicy(upload_hours=24, audio_days=30, log_days=30),
    )

    assert removed == 1
    assert not worker_log.exists()
    assert marker.exists(), "API は archive 以外の制御ファイルを削除しない"


def test_failed_webauthn_registration_consumes_challenge(client, db, monkeypatch) -> None:
    from conftest import make_user

    from minutes_api.routers import auth_web

    owner = make_user(db)
    token = "browser-test-token"
    browser = models.BrowserSession(
        token_hash=token_hash(token), user_id=owner.id, csrf_token="csrf", webauthn_challenge="one-time",
        webauthn_context={"purpose": "register"},
        pending_registration={"purpose": "fresh_registration"},
        expires_at=now() + timedelta(hours=1),
    )
    db.add(browser)
    db.commit()
    monkeypatch.setattr(auth_web, "verify_registration", lambda *args: (_ for _ in ()).throw(ValueError("invalid")))
    client.cookies.set("am_session", token)
    response = client.post("/auth/webauthn/register/verify", headers={"x-csrf-token": "csrf"}, json={"id": "bad"})
    assert response.status_code == 400
    db.expire_all()
    assert db.get(models.BrowserSession, browser.id).webauthn_challenge is None
    second = client.post("/auth/webauthn/register/verify", headers={"x-csrf-token": "csrf"}, json={"id": "bad"})
    assert second.status_code == 401


def test_delete_commits_tombstone_before_cleanup_and_retries_after_io_failure(
    db, session_factory, settings, monkeypatch
) -> None:
    from conftest import make_user

    owner = make_user(db)
    session = _session(db, owner)
    store = sessions_service.artifact_store(settings)
    stored = store.put_bytes(b"audio")
    artifact = models.Artifact(
        id=stored.artifact_id,
        session_id=session.id,
        kind="audio_track",
        content_type="audio/wav",
        byte_size=stored.byte_size,
        sha256=stored.sha256,
    )
    db.add(artifact)
    db.commit()
    monkeypatch.setattr(jobs, "cancel_all_for_session", lambda *_: 0)
    original_cleanup = sessions_service.cleanup_deleted_session
    monkeypatch.setattr(
        sessions_service,
        "cleanup_deleted_session",
        lambda *_: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    with pytest.raises(ApiException) as raised:
        sessions_service.delete_session(db, session, owner)
    assert raised.value.status_code == 503
    assert store.exists(stored.artifact_id), "I/O失敗時もDB tombstoneだけは確定済み"

    with session_factory() as verify:
        deleted = verify.get(models.MeetingSession, session.id)
        assert deleted.deleted_at is not None and deleted.status == "deleting"
        assert verify.get(models.Artifact, stored.artifact_id).deleted_at is not None

    monkeypatch.setattr(sessions_service, "cleanup_deleted_session", original_cleanup)
    with session_factory() as retry_db:
        sessions_service.delete_session(retry_db, retry_db.get(models.MeetingSession, session.id), retry_db.get(models.User, owner.id))
    assert not store.exists(stored.artifact_id)


def test_delete_does_not_touch_files_when_tombstone_commit_fails(db, settings, monkeypatch) -> None:
    from conftest import make_user

    owner = make_user(db)
    session = _session(db, owner)
    store = sessions_service.artifact_store(settings)
    stored = store.put_bytes(b"audio")
    db.add(
        models.Artifact(
            id=stored.artifact_id,
            session_id=session.id,
            kind="audio_track",
            content_type="audio/wav",
            byte_size=stored.byte_size,
            sha256=stored.sha256,
        )
    )
    db.commit()
    monkeypatch.setattr(jobs, "cancel_all_for_session", lambda *_: 0)
    cleaned = []
    monkeypatch.setattr(sessions_service, "cleanup_deleted_session", lambda plan: cleaned.append(plan))
    monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit failed")))

    with pytest.raises(RuntimeError, match="commit failed"):
        sessions_service.delete_session(db, session, owner)
    assert cleaned == [] and store.exists(stored.artifact_id)


def _start_browser_reauth(client, db, password: str):
    from conftest import auth_headers, issue_access_token

    user, _ = accounts.create_user(db, email=f"reauth-{uuid.uuid4().hex[:8]}@example.test", role="owner", password=password)
    db.commit()
    access = issue_access_token(db, user)
    started = client.post("/v1/auth/reauth/browser", headers=auth_headers(access))
    assert started.status_code == 201
    token = started.json()["reauth_url"].rsplit("/", 1)[-1]
    form = client.get(f"/auth/reauth/{token}")
    assert form.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
    return user, access, started.json(), token, csrf


def test_browser_password_reauth_grants_original_access_token(client, db) -> None:
    from conftest import auth_headers

    password = "correct horse battery staple 2026"
    user, access, started, token, csrf = _start_browser_reauth(client, db, password)
    failed = client.post(f"/auth/reauth/{token}", data={"password": "wrong", "csrf_token": csrf})
    assert failed.status_code == 200 and "確認できません" in failed.text
    response = client.post(f"/auth/reauth/{token}", data={"password": password, "csrf_token": csrf})
    assert response.status_code == 200 and "再認証が完了しました" in response.text

    status = client.get(
        f"/v1/auth/reauth/browser/{started['request_id']}", headers=auth_headers(access)
    )
    assert status.status_code == 200 and status.json()["status"] == "completed"
    assert status.json()["reauth_valid_until"] is not None
    grant = db.execute(select(models.ReauthGrant).where(models.ReauthGrant.user_id == user.id)).scalars().one()
    assert grant.access_token_id is not None


def test_browser_passkey_reauth_uses_target_users_credential(client, db, monkeypatch) -> None:
    from conftest import auth_headers

    from minutes_api.routers import auth_web

    user, access, started, token, csrf = _start_browser_reauth(
        client, db, "another correct horse battery 2026"
    )
    credential = models.WebAuthnCredential(
        user_id=user.id,
        credential_id="Y3JlZGVudGlhbA",
        public_key="cHVibGljLWtleQ",
        sign_count=1,
    )
    db.add(credential)
    db.commit()
    monkeypatch.setattr(auth_web, "verify_authentication", lambda body, challenge, stored: 2)

    options = client.post(
        f"/auth/reauth/{token}/webauthn/options", headers={"x-csrf-token": csrf}
    )
    assert options.status_code == 200
    assert options.json()["allowCredentials"][0]["id"] == credential.credential_id
    verified = client.post(
        f"/auth/reauth/{token}/webauthn/verify",
        headers={"x-csrf-token": csrf},
        json={"id": credential.credential_id, "rawId": credential.credential_id, "type": "public-key", "response": {}},
    )
    assert verified.status_code == 200 and verified.json() == {"completed": True}
    status = client.get(
        f"/v1/auth/reauth/browser/{started['request_id']}", headers=auth_headers(access)
    )
    assert status.json()["status"] == "completed"
