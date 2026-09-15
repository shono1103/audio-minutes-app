"""A-1-1: 公開 HTTP 境界だけでは最初の owner を作成できない。"""

from __future__ import annotations

from sqlalchemy import select

from minutes_api import models
from minutes_api.security import after, token_hash


def _assert_no_users(db) -> None:  # noqa: ANN001
    db.expire_all()
    assert db.execute(select(models.User.id)).all() == []


def test_public_http_boundaries_cannot_create_first_owner(client, db) -> None:
    _assert_no_users(db)

    missing_body = client.post("/auth/bootstrap/any-token")
    assert missing_body.status_code == 400
    _assert_no_users(db)

    token = "valid-bootstrap-without-browser-session"
    db.add(models.BootstrapToken(token_hash=token_hash(token), expires_at=after(600)))
    db.commit()
    missing_session = client.post(
        f"/auth/bootstrap/{token}",
        data={
            "email": "owner@example.test",
            "password": "long-enough-password",
            "csrf_token": "synthetic-csrf",
        },
    )
    assert missing_session.status_code == 403
    _assert_no_users(db)

    undefined_create = client.post("/v1/admin/users")
    assert undefined_create.status_code == 405
    _assert_no_users(db)

    unauthenticated_list = client.get("/v1/admin/users")
    assert unauthenticated_list.status_code == 401
    _assert_no_users(db)
