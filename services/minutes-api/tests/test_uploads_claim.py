"""tus 追送の排他 (R04)。

ストリーム受信中に行ロックも transaction も保持しないので、競合する 2 本目は
待たずに 409 を返し、1 本目は最後まで受け切れる。異常終了で残った claim は
期限で引き継ぎ、切断時は offset を進めずに claim だけ解放する。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from conftest import auth_headers, issue_access_token, make_upload, make_user
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from minutes_api import models
from minutes_api.routers.uploads import _http_date
from minutes_api.security import now

TUS = {"Tus-Resumable": "1.0.0", "Content-Type": "application/offset+octet-stream"}


def test_http_date_accepts_postgresql_utc_timezone() -> None:
    value = datetime(2026, 9, 12, 0, 0, tzinfo=ZoneInfo("Etc/UTC"))
    assert _http_date(value) == "Sat, 12 Sep 2026 00:00:00 GMT"


def _setup(session_factory: sessionmaker[Session], length: int = 8) -> tuple[str, models.Upload]:
    with session_factory() as db:
        user = make_user(db)
        db.commit()
        token = issue_access_token(db, user)
        _, upload = make_upload(db, user, length=length)
    return token, upload


def _patch(client: TestClient, upload_id, token: str, offset: int, body: bytes):
    return client.patch(
        f"/v1/uploads/{upload_id}",
        content=body,
        headers={**TUS, "Upload-Offset": str(offset), **auth_headers(token)},
    )


def test_sequential_patches_advance_offset_and_complete(client: TestClient, session_factory) -> None:
    token, upload = _setup(session_factory)
    first = _patch(client, upload.id, token, 0, b"abcd")
    assert first.status_code == 204 and first.headers["Upload-Offset"] == "4"
    second = _patch(client, upload.id, token, 4, b"efgh")
    assert second.status_code == 204 and second.headers["Upload-Offset"] == "8"

    with session_factory() as db:
        row = db.get(models.Upload, upload.id)
        assert row.state == "completed"
        assert row.write_claim_id is None and row.write_claim_expires_at is None


def test_concurrent_patch_is_rejected_without_waiting(client: TestClient, session_factory) -> None:
    """受信中の upload へ 2 本目が来ても待たずに 409 を返す。"""
    token, upload = _setup(session_factory)
    with session_factory() as db:
        row = db.get(models.Upload, upload.id)
        row.write_claim_id = models.uuid.uuid4()
        row.write_claim_expires_at = now() + timedelta(seconds=60)
        row.state = "uploading"
        db.commit()

    response = _patch(client, upload.id, token, 0, b"abcd")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert response.headers["Upload-Offset"] == "0", "受信済み位置を返して再開できるようにする"

    with session_factory() as db:
        assert db.get(models.Upload, upload.id).offset == 0, "2 本目の書き込みが混ざっていない"


def test_expired_claim_is_taken_over(client: TestClient, session_factory) -> None:
    """異常終了で残った claim は期限を過ぎたら引き継ぐ。"""
    token, upload = _setup(session_factory)
    with session_factory() as db:
        row = db.get(models.Upload, upload.id)
        row.write_claim_id = models.uuid.uuid4()
        row.write_claim_expires_at = now() - timedelta(seconds=1)
        row.state = "uploading"
        db.commit()

    assert _patch(client, upload.id, token, 0, b"abcd").status_code == 204
    with session_factory() as db:
        row = db.get(models.Upload, upload.id)
        assert row.offset == 4 and row.write_claim_id is None


def test_offset_mismatch_is_rejected_and_claim_is_not_held(client: TestClient, session_factory) -> None:
    token, upload = _setup(session_factory)
    response = _patch(client, upload.id, token, 3, b"abcd")
    assert response.status_code == 409 and response.headers["Upload-Offset"] == "0"
    with session_factory() as db:
        row = db.get(models.Upload, upload.id)
        assert row.write_claim_id is None, "拒否した要求が claim を掴んだままになっている"

    # 正しい offset ならそのまま続けられる
    assert _patch(client, upload.id, token, 0, b"abcd").status_code == 204


def test_over_length_body_releases_the_claim_and_keeps_offset(client: TestClient, session_factory) -> None:
    token, upload = _setup(session_factory, length=4)
    response = _patch(client, upload.id, token, 0, b"abcdefgh")
    assert response.status_code == 400 and response.json()["error"]["code"] == "limit_exceeded"
    with session_factory() as db:
        row = db.get(models.Upload, upload.id)
        assert row.offset == 0 and row.write_claim_id is None

    # 解放されているので、正しい大きさで再送できる
    assert _patch(client, upload.id, token, 0, b"abcd").status_code == 204


def test_head_reports_offset_while_health_stays_available(client: TestClient, session_factory) -> None:
    token, upload = _setup(session_factory)
    assert _patch(client, upload.id, token, 0, b"abcd").status_code == 204
    head = client.head(f"/v1/uploads/{upload.id}", headers={"Tus-Resumable": "1.0.0", **auth_headers(token)})
    assert head.status_code == 200 and head.headers["Upload-Offset"] == "4"
    assert client.get("/v1/health").status_code == 200
