"""本人アカウント: /v1/me、再認証、リカバリーコード、パスキー一覧。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from minutes_api import accounts, audit
from minutes_api.config import get_settings
from minutes_api.db import db_dependency
from minutes_api.deps import Principal, current_principal
from minutes_api.errors import ApiException, not_found
from minutes_api.models import BrowserReauthRequest, PasswordCredential, ReauthGrant, WebAuthnCredential
from minutes_api.ratelimit import limiter
from minutes_api.security import after, now, verify_password

router = APIRouter(prefix="/v1", tags=["account"])


class ReauthRequest(BaseModel):
    password: str | None = None
    recovery_code: str | None = None


class BrowserReauthStatus(BaseModel):
    request_id: uuid.UUID
    status: Literal["pending", "completed", "expired"]
    expires_at: datetime
    reauth_valid_until: datetime | None = None


class BrowserReauthStarted(BrowserReauthStatus):
    reauth_url: str


class PasskeyBrowserStatus(BaseModel):
    request_id: uuid.UUID
    status: Literal["pending", "ready", "completed", "expired"]
    expires_at: datetime


class PasskeyBrowserStarted(PasskeyBrowserStatus):
    passkey_url: str


def _browser_reauth_view(row: BrowserReauthRequest, db: Session) -> dict:
    timestamp = now()
    status = "completed" if row.completed_at is not None else "expired" if row.expires_at <= timestamp else "pending"
    grant = db.get(ReauthGrant, row.grant_id) if row.grant_id else None
    return {
        "request_id": str(row.id),
        "status": status,
        "expires_at": row.expires_at.isoformat(),
        "reauth_valid_until": grant.expires_at.isoformat() if grant is not None else None,
    }


def _passkey_browser_view(row: BrowserReauthRequest) -> dict:
    timestamp = now()
    if row.action_completed_at is not None:
        status = "completed"
    elif row.expires_at <= timestamp:
        status = "expired"
    elif row.completed_at is not None:
        status = "ready"
    else:
        status = "pending"
    return {
        "request_id": str(row.id),
        "status": status,
        "expires_at": row.expires_at.isoformat(),
    }


@router.get("/me", summary="自分の情報")
def me(principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> dict:
    grant = db.execute(
        select(ReauthGrant).where(ReauthGrant.access_token_id == principal.access_token.id, ReauthGrant.expires_at > now())
    ).scalars().first()
    passkeys = db.execute(select(WebAuthnCredential.id).where(WebAuthnCredential.user_id == principal.id)).all()
    return {
        "user_id": str(principal.id),
        "email": principal.user.email,
        "role": principal.user.role,
        "reauth_valid_until": grant.expires_at.isoformat() if grant else None,
        "passkey_count": len(passkeys),
        "default_format_profile_id": str(principal.user.default_format_profile_id) if principal.user.default_format_profile_id else None,
    }


@router.post(
    "/auth/reauth",
    summary="旧形式の直接再認証",
    deprecated=True,
    description="nativeはpasswordを受け取らず、/v1/auth/reauth/browserを使用すること。",
)
def reauth(body: ReauthRequest, principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> dict:
    limiter.check(f"reauth:{principal.id}", 10, 60)
    ok = False
    if body.password:
        credential = db.get(PasswordCredential, principal.id)
        ok = credential is not None and verify_password(credential.password_hash, body.password)
    elif body.recovery_code:
        ok = accounts.consume_recovery_code(db, principal.user, body.recovery_code)
    if not ok:
        audit.record(db, "reauth_failed", actor_id=principal.id)
        raise ApiException(401, "unauthorized", "再認証に失敗しました")
    grant = ReauthGrant(user_id=principal.id, access_token_id=principal.access_token.id, expires_at=after(get_settings().reauth_seconds))
    db.add(grant)
    audit.record(db, "reauth_succeeded", actor_id=principal.id)
    return {"reauth_valid_until": grant.expires_at.isoformat()}


@router.post(
    "/auth/reauth/browser",
    status_code=201,
    response_model=BrowserReauthStarted,
    summary="ブラウザー再認証の開始",
)
def start_browser_reauth(
    principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)
) -> dict:
    row, token = accounts.issue_browser_reauth(db, principal.user, principal.access_token)
    value = _browser_reauth_view(row, db)
    value["reauth_url"] = f"{get_settings().public_base_url}/auth/reauth/{token}"
    return value


@router.get(
    "/auth/reauth/browser/{request_id}",
    response_model=BrowserReauthStatus,
    summary="ブラウザー再認証の状態",
)
def browser_reauth_status(
    request_id: uuid.UUID,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(db_dependency),
) -> dict:
    row = db.get(BrowserReauthRequest, request_id)
    if (
        row is None
        or row.purpose != "reauth"
        or row.user_id != principal.id
        or row.access_token_id != principal.access_token.id
    ):
        raise not_found("再認証要求が見つかりません")
    return _browser_reauth_view(row, db)


@router.post("/account/recovery-codes", summary="リカバリーコード再発行 (再認証必須)")
def regenerate_recovery_codes(principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> dict:
    from minutes_api.deps import require_reauth

    require_reauth(principal, db)
    codes = accounts.issue_recovery_codes(db, principal.user)
    audit.record(db, "recovery_codes_regenerated", actor_id=principal.id)
    return {"recovery_codes": codes}


@router.get("/account/passkeys", summary="パスキー一覧")
def list_passkeys(principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> dict:
    rows = db.execute(select(WebAuthnCredential).where(WebAuthnCredential.user_id == principal.id)).scalars()
    return {
        "items": [
            {"id": str(row.id), "label": row.label, "created_at": row.created_at.isoformat(), "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None}
            for row in rows
        ]
    }


@router.post(
    "/account/passkeys/browser",
    status_code=201,
    response_model=PasskeyBrowserStarted,
    summary="ブラウザーでのパスキー追加を開始",
    description="server browser内で本人再認証とWebAuthn登録を完結し、nativeへ認証情報を渡さない。",
)
def start_passkey_browser_registration(
    principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)
) -> dict:
    row, token = accounts.issue_browser_reauth(
        db,
        principal.user,
        principal.access_token,
        purpose="passkey_registration",
    )
    value = _passkey_browser_view(row)
    value["passkey_url"] = f"{get_settings().public_base_url}/auth/account/passkeys/{token}"
    return value


@router.get(
    "/account/passkeys/browser/{request_id}",
    response_model=PasskeyBrowserStatus,
    summary="ブラウザーでのパスキー追加状態",
)
def passkey_browser_registration_status(
    request_id: uuid.UUID,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(db_dependency),
) -> dict:
    row = db.get(BrowserReauthRequest, request_id)
    if (
        row is None
        or row.purpose != "passkey_registration"
        or row.user_id != principal.id
        or row.access_token_id != principal.access_token.id
    ):
        raise not_found("パスキー登録要求が見つかりません")
    return _passkey_browser_view(row)


@router.delete("/account/passkeys/{passkey_id}", status_code=204, summary="パスキー削除")
def delete_passkey(passkey_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> None:
    row = db.get(WebAuthnCredential, passkey_id)
    if row is None or row.user_id != principal.id:
        raise not_found("パスキーが見つかりません")
    db.delete(row)
    audit.record(db, "passkey_deleted", actor_id=principal.id, target_type="passkey", target_id=passkey_id)
