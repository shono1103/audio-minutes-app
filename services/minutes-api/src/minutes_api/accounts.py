"""アカウント作成・トークン発行など、複数 router から使う認証ドメインの操作。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from minutes_api import audit
from minutes_api.config import get_settings
from minutes_api.errors import ApiException
from minutes_api.models import (
    AccessToken,
    BootstrapToken,
    BrowserReauthRequest,
    Invitation,
    PasswordCredential,
    ReauthGrant,
    RecoveryCode,
    RefreshToken,
    User,
)
from minutes_api.security import (
    after,
    hash_password,
    new_recovery_codes,
    new_token,
    now,
    password_problems,
    token_hash,
    verify_password,
)


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or len(email) > 320:
        raise ApiException(400, "invalid_request", "メールアドレスの形式が不正です")
    return email


def create_user(
    db: Session,
    *,
    email: str,
    role: str,
    password: str | None,
    user_id: uuid.UUID | None = None,
) -> tuple[User, list[str]]:
    email = normalize_email(email)
    if password is not None:
        problems = password_problems(password, email)
        if problems:
            raise ApiException(400, "invalid_request", "、".join(problems))
    if db.execute(select(User.id).where(User.email == email)).first() is not None:
        # 存在を推測されないよう表現を一般化する
        raise ApiException(409, "conflict", "この登録は完了できません")
    user = User(id=user_id or uuid.uuid4(), email=email, role=role)
    db.add(user)
    db.flush()
    if password is not None:
        db.add(PasswordCredential(user_id=user.id, password_hash=hash_password(password)))
    codes = issue_recovery_codes(db, user)
    return user, codes


def issue_recovery_codes(db: Session, user: User) -> list[str]:
    for row in db.execute(select(RecoveryCode).where(RecoveryCode.user_id == user.id)).scalars():
        db.delete(row)
    codes = new_recovery_codes()
    for code in codes:
        db.add(RecoveryCode(user_id=user.id, code_hash=token_hash(code.replace("-", ""))))
    return codes


def consume_recovery_code(db: Session, user: User, code: str) -> bool:
    digest = token_hash(code.strip().replace("-", "").lower())
    # 読み出してから更新すると、独立 transaction が同じ未使用行を同時に読み、双方が
    # 成功し得る。used_at IS NULL を更新条件に含め、RETURNING の有無を消費結果とする。
    consumed = db.execute(
        update(RecoveryCode)
        .where(
            RecoveryCode.user_id == user.id,
            RecoveryCode.code_hash == digest,
            RecoveryCode.used_at.is_(None),
        )
        .values(used_at=now())
        .returning(RecoveryCode.id)
    ).scalar_one_or_none()
    return consumed is not None


def authenticate_password(db: Session, email: str, password: str) -> User | None:
    try:
        email = normalize_email(email)
    except ApiException:
        return None
    row = db.execute(
        select(User, PasswordCredential).join(PasswordCredential, PasswordCredential.user_id == User.id).where(User.email == email)
    ).first()
    if row is None:
        # 存在しないユーザーでも計算時間を揃える
        verify_password(hash_password("dummy-password-to-equalize-timing"), password)
        return None
    user, credential = row
    if user.disabled_at is not None or not verify_password(credential.password_hash, password):
        return None
    return user


def verify_user_password(db: Session, user: User, password: str) -> bool:
    credential = db.get(PasswordCredential, user.id)
    return credential is not None and user.disabled_at is None and verify_password(credential.password_hash, password)


def issue_browser_reauth(
    db: Session,
    user: User,
    access_token: AccessToken,
    *,
    purpose: str = "reauth",
) -> tuple[BrowserReauthRequest, str]:
    token = new_token()
    row = BrowserReauthRequest(
        token_hash=token_hash(token),
        user_id=user.id,
        access_token_id=access_token.id,
        purpose=purpose,
        expires_at=after(get_settings().browser_reauth_seconds),
    )
    db.add(row)
    db.flush()
    audit.record(db, "browser_reauth_issued", actor_id=user.id, target_type="reauth", target_id=row.id)
    return row, token


def find_browser_reauth(db: Session, token: str, *, lock: bool = False) -> BrowserReauthRequest | None:
    statement = select(BrowserReauthRequest).where(BrowserReauthRequest.token_hash == token_hash(token))
    if lock:
        statement = statement.with_for_update()
    return db.execute(statement).scalars().first()


def complete_browser_reauth(db: Session, request: BrowserReauthRequest, user: User) -> ReauthGrant:
    """元のaccess tokenへgrantを一度だけ発行する。browser cookieへ権限は載せない。"""
    if request.user_id != user.id or user.disabled_at is not None:
        raise ApiException(401, "unauthorized", "再認証に失敗しました")
    if request.completed_at is not None:
        grant = db.get(ReauthGrant, request.grant_id) if request.grant_id else None
        if grant is None:
            raise ApiException(409, "conflict", "再認証要求は既に使用されています")
        return grant
    if request.expires_at <= now():
        raise ApiException(410, "invalid_request", "再認証要求の有効期限が切れています")
    access = db.get(AccessToken, request.access_token_id)
    if (
        access is None
        or access.user_id != user.id
        or access.revoked_at is not None
        or access.expires_at <= now()
    ):
        raise ApiException(410, "invalid_request", "再認証を開始したログインが無効です")
    grant = ReauthGrant(
        user_id=user.id,
        access_token_id=access.id,
        expires_at=after(get_settings().reauth_seconds),
    )
    db.add(grant)
    db.flush()
    request.grant_id = grant.id
    request.completed_at = now()
    audit.record(db, "reauth_succeeded", actor_id=user.id, target_type="reauth", target_id=request.id)
    return grant


def issue_bootstrap_token(db: Session) -> tuple[str, datetime]:
    if db.execute(select(User.id)).first() is not None:
        raise ApiException(409, "conflict", "既にアカウントが存在するため bootstrap は実行できません")
    token = new_token()
    expires_at = after(get_settings().bootstrap_seconds)
    db.add(BootstrapToken(token_hash=token_hash(token), expires_at=expires_at))
    audit.record(db, "bootstrap_issued", actor_id=None)
    return token, expires_at


def find_valid_bootstrap(db: Session, token: str, *, lock: bool = False) -> BootstrapToken | None:
    statement = select(BootstrapToken).where(BootstrapToken.token_hash == token_hash(token))
    if lock:
        statement = statement.with_for_update()
    row = db.execute(statement).scalars().first()
    if row is None or row.used_at is not None or row.expires_at <= now():
        return None
    return row


def find_valid_invitation(db: Session, token: str, *, lock: bool = False) -> Invitation | None:
    statement = select(Invitation).where(Invitation.token_hash == token_hash(token))
    if lock:
        statement = statement.with_for_update()
    row = db.execute(statement).scalars().first()
    if row is None or row.used_at is not None or row.revoked_at is not None or row.expires_at <= now():
        return None
    return row


def issue_tokens(db: Session, user: User, scope: str = "", family_id: uuid.UUID | None = None) -> dict[str, object]:
    settings = get_settings()
    family = family_id or uuid.uuid4()
    access = new_token()
    refresh = new_token()
    db.add(
        AccessToken(
            token_hash=token_hash(access),
            user_id=user.id,
            family_id=family,
            scope=scope,
            expires_at=after(settings.access_token_seconds),
        )
    )
    refresh_row = RefreshToken(
        token_hash=token_hash(refresh), family_id=family, user_id=user.id, expires_at=after(settings.refresh_token_seconds)
    )
    db.add(refresh_row)
    db.flush()
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": settings.access_token_seconds,
        "refresh_token": refresh,
        "scope": scope,
        "_refresh_id": refresh_row.id,
        "_family_id": family,
    }


def revoke_family(db: Session, family_id: uuid.UUID) -> None:
    timestamp = now()
    db.execute(
        update(RefreshToken).where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None)).values(revoked_at=timestamp)
    )
    db.execute(
        update(AccessToken).where(AccessToken.family_id == family_id, AccessToken.revoked_at.is_(None)).values(revoked_at=timestamp)
    )


def revoke_all_for_user(db: Session, user_id: uuid.UUID) -> None:
    timestamp = now()
    db.execute(update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)).values(revoked_at=timestamp))
    db.execute(update(AccessToken).where(AccessToken.user_id == user_id, AccessToken.revoked_at.is_(None)).values(revoked_at=timestamp))
