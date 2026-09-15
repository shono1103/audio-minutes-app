"""FR-030: passkey-first登録と、既存ユーザーの安全な後日追加導線。"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import func, select

from minutes_api import accounts, models
from minutes_api.ratelimit import limiter
from minutes_api.security import after, token_hash


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _mock_webauthn(monkeypatch, *, credential_id: str = "bmV3LWNyZWRlbnRpYWw") -> None:
    from minutes_api.routers import auth_web

    monkeypatch.setattr(
        auth_web,
        "registration_options",
        lambda user, existing: (
            {
                "challenge": "Y2hhbGxlbmdl",
                "user": {"id": "dXNlcg", "name": user.email, "displayName": user.email},
                "excludeCredentials": [],
            },
            "Y2hhbGxlbmdl",
        ),
    )
    monkeypatch.setattr(
        auth_web,
        "verify_registration",
        lambda credential, challenge: (credential_id, "cHVibGljLWtleQ", 0, ["internal"]),
    )


def test_bootstrap_supports_passkey_first_without_password(client, db, monkeypatch) -> None:
    token = "bootstrap-passkey-first"
    bootstrap = models.BootstrapToken(token_hash=token_hash(token), expires_at=after(600))
    db.add(bootstrap)
    db.commit()
    _mock_webauthn(monkeypatch)

    form = client.get(f"/auth/bootstrap/{token}")
    assert form.status_code == 200
    assert "パスキーで登録 (推奨)" in form.text
    assert 'name="password" type="password" minlength="15"' in form.text
    assert 'name="password" type="password" required' not in form.text
    csrf = _csrf(form.text)

    options = client.post(
        f"/auth/bootstrap/{token}/webauthn/options",
        headers={"x-csrf-token": csrf},
        json={"email": "First.Owner@example.test"},
    )
    assert options.status_code == 200
    verified = client.post(
        f"/auth/bootstrap/{token}/webauthn/verify",
        headers={"x-csrf-token": csrf},
        json={"id": "new", "label": "Mac"},
    )
    assert verified.status_code == 200
    assert verified.json()["registered"] is True
    assert len(verified.json()["recovery_codes"]) == 8

    db.expire_all()
    user = db.execute(select(models.User).where(models.User.email == "first.owner@example.test")).scalar_one()
    assert user.role == "owner"
    assert db.get(models.PasswordCredential, user.id) is None
    credential = db.execute(
        select(models.WebAuthnCredential).where(models.WebAuthnCredential.user_id == user.id)
    ).scalar_one()
    assert credential.label == "Mac"
    assert db.get(models.BootstrapToken, bootstrap.id).used_at is not None
    assert client.post(
        f"/auth/bootstrap/{token}/webauthn/verify",
        headers={"x-csrf-token": csrf},
        json={"id": "replayed"},
    ).status_code == 403


def test_initial_passkey_options_keeps_csrf_precedence_over_body_validation(client, db) -> None:
    token = "bootstrap-options-csrf"
    db.add(models.BootstrapToken(token_hash=token_hash(token), expires_at=after(600)))
    db.commit()
    form = client.get(f"/auth/bootstrap/{token}")
    assert form.status_code == 200

    response = client.post(
        f"/auth/bootstrap/{token}/webauthn/options",
        headers={"x-csrf-token": "wrong"},
        json={"email": 123},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_invitation_passkey_registration_preserves_invited_owner_and_consumes_challenge(
    client, db, monkeypatch
) -> None:
    owner = models.User(email="owner@example.test", role="owner")
    db.add(owner)
    db.flush()
    token = "member-invitation"
    invitation = models.Invitation(
        token_hash=token_hash(token),
        email="member@example.test",
        role="member",
        created_by=owner.id,
        expires_at=after(3600),
    )
    db.add(invitation)
    db.commit()
    _mock_webauthn(monkeypatch, credential_id="aW52aXRlLWNyZWRlbnRpYWw")

    form = client.get(f"/auth/invite/{token}")
    csrf = _csrf(form.text)
    wrong_email = client.post(
        f"/auth/invite/{token}/webauthn/options",
        headers={"x-csrf-token": csrf},
        json={"email": "attacker@example.test"},
    )
    assert wrong_email.status_code == 400

    options = client.post(
        f"/auth/invite/{token}/webauthn/options",
        headers={"x-csrf-token": csrf},
        json={"email": "member@example.test"},
    )
    assert options.status_code == 200

    from minutes_api.routers import auth_web

    monkeypatch.setattr(
        auth_web,
        "verify_registration",
        lambda *args: (_ for _ in ()).throw(ValueError("invalid assertion")),
    )
    failed = client.post(
        f"/auth/invite/{token}/webauthn/verify",
        headers={"x-csrf-token": csrf},
        json={"id": "invalid"},
    )
    assert failed.status_code == 400
    second = client.post(
        f"/auth/invite/{token}/webauthn/verify",
        headers={"x-csrf-token": csrf},
        json={"id": "replayed"},
    )
    assert second.status_code == 403
    db.expire_all()
    assert db.get(models.Invitation, invitation.id).used_at is None
    assert db.execute(
        select(func.count()).select_from(models.User).where(models.User.email == "member@example.test")
    ).scalar_one() == 0


def test_existing_user_can_reauth_and_add_passkey_in_server_browser(client, db, monkeypatch) -> None:
    from conftest import auth_headers, issue_access_token

    password = "correct horse battery staple 2026"
    user, _ = accounts.create_user(
        db,
        email="later-passkey@example.test",
        role="member",
        password=password,
    )
    db.commit()
    access = issue_access_token(db, user)
    _mock_webauthn(monkeypatch, credential_id="bGF0ZXItY3JlZGVudGlhbA")

    started = client.post("/v1/account/passkeys/browser", headers=auth_headers(access))
    assert started.status_code == 201
    assert started.json()["status"] == "pending"
    token = started.json()["passkey_url"].rsplit("/", 1)[-1]
    form = client.get(f"/auth/account/passkeys/{token}")
    assert form.status_code == 200 and "パスキー追加の本人確認" in form.text
    csrf = _csrf(form.text)

    reauthenticated = client.post(
        f"/auth/account/passkeys/{token}",
        data={"password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert reauthenticated.status_code == 303
    add_form = client.get(reauthenticated.headers["location"])
    assert "新しいパスキーを登録" in add_form.text
    add_csrf = _csrf(add_form.text)
    ready = client.get(
        f"/v1/account/passkeys/browser/{started.json()['request_id']}",
        headers=auth_headers(access),
    )
    assert ready.status_code == 200 and ready.json()["status"] == "ready"

    options = client.post(
        f"/auth/account/passkeys/{token}/webauthn/register/options",
        headers={"x-csrf-token": add_csrf},
    )
    assert options.status_code == 200
    verified = client.post(
        f"/auth/account/passkeys/{token}/webauthn/register/verify",
        headers={"x-csrf-token": add_csrf},
        json={"id": "new", "label": "iPhone"},
    )
    assert verified.status_code == 200 and verified.json() == {"registered": True}
    completed = client.get(
        f"/v1/account/passkeys/browser/{started.json()['request_id']}",
        headers=auth_headers(access),
    )
    assert completed.status_code == 200 and completed.json()["status"] == "completed"
    assert client.post(
        f"/auth/account/passkeys/{token}/webauthn/register/options",
        headers={"x-csrf-token": add_csrf},
    ).status_code == 403

    db.expire_all()
    credential = db.execute(
        select(models.WebAuthnCredential).where(models.WebAuthnCredential.user_id == user.id)
    ).scalar_one()
    assert credential.label == "iPhone"


def test_passkey_browser_status_is_bound_to_access_token_owner(client, db) -> None:
    from conftest import auth_headers, issue_access_token

    first = models.User(email="first@example.test", role="member")
    second = models.User(email="second@example.test", role="member")
    db.add_all([first, second])
    db.commit()
    first_access = issue_access_token(db, first)
    second_access = issue_access_token(db, second)
    started = client.post("/v1/account/passkeys/browser", headers=auth_headers(first_access)).json()

    denied = client.get(
        f"/v1/account/passkeys/browser/{started['request_id']}",
        headers=auth_headers(second_access),
    )
    assert denied.status_code == 404
