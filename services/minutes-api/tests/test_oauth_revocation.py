"""リフレッシュ再使用の検知で、失効と監査が拒否応答と一緒に巻き戻らないこと (R03)。"""

from __future__ import annotations

from conftest import make_refresh_pair, make_user
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from minutes_api import models
from minutes_api.security import token_hash


def _token_form(refresh_token: str) -> dict[str, str]:
    return {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": "audio-minutes-native"}


def test_refresh_reuse_revokes_family_and_survives_the_rejection(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as db:
        user = make_user(db)
        db.commit()
        first_refresh, family = make_refresh_pair(db, user)

    rotated = client.post("/oauth/token", data=_token_form(first_refresh))
    assert rotated.status_code == 200
    new_refresh = rotated.json()["refresh_token"]

    reused = client.post("/oauth/token", data=_token_form(first_refresh))
    assert reused.status_code == 400
    assert reused.json()["error"]["details"]["oauth_error"] == "invalid_grant"

    # 別リクエスト (別 transaction) から見て、系列全体が失効している必要がある
    with session_factory() as db:
        refresh_rows = list(db.execute(select(models.RefreshToken).where(models.RefreshToken.family_id == family)).scalars())
        assert refresh_rows, "系列が見つからない"
        assert all(row.revoked_at is not None for row in refresh_rows), "新しい refresh が失効していない"
        access_rows = list(db.execute(select(models.AccessToken).where(models.AccessToken.family_id == family)).scalars())
        assert all(row.revoked_at is not None for row in access_rows), "既存 access が失効していない"
        actions = list(db.execute(select(models.AuditLog.action)).scalars())
        assert "refresh_reuse_detected" in actions, "監査が残っていない"

    # 失効後は回転で得た refresh も使えない
    assert client.post("/oauth/token", data=_token_form(new_refresh)).status_code == 400


def test_unrelated_family_is_not_affected(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    with session_factory() as db:
        user = make_user(db)
        db.commit()
        reused_refresh, _ = make_refresh_pair(db, user)
        other_refresh, other_family = make_refresh_pair(db, user)

    client.post("/oauth/token", data=_token_form(reused_refresh))
    assert client.post("/oauth/token", data=_token_form(reused_refresh)).status_code == 400

    with session_factory() as db:
        rows = list(db.execute(select(models.RefreshToken).where(models.RefreshToken.family_id == other_family)).scalars())
        assert rows and all(row.revoked_at is None for row in rows), "無関係な系列まで失効している"
    assert client.post("/oauth/token", data=_token_form(other_refresh)).status_code == 200


def test_authorization_code_is_consumed_even_when_pkce_fails(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """PKCE 検証に失敗しても code の消費は確定させ、同じ code を試し直せないようにする。"""
    from minutes_api.security import after, new_token

    code = new_token()
    with session_factory() as db:
        user = make_user(db)
        db.add(
            models.AuthorizationCode(
                code_hash=token_hash(code), user_id=user.id, client_id="audio-minutes-native",
                redirect_uri="http://127.0.0.1:8899/callback", code_challenge="x" * 43, scope="",
                expires_at=after(60),
            )
        )
        db.commit()

    form = {
        "grant_type": "authorization_code", "code": code, "code_verifier": "y" * 64,
        "redirect_uri": "http://127.0.0.1:8899/callback", "client_id": "audio-minutes-native",
    }
    assert client.post("/oauth/token", data=form).status_code == 400
    with session_factory() as db:
        row = db.execute(select(models.AuthorizationCode).where(models.AuthorizationCode.code_hash == token_hash(code))).scalars().one()
        assert row.used_at is not None, "検証失敗時に code が未使用のまま残っている"
    assert client.post("/oauth/token", data=form).status_code == 400
