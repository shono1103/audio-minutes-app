"""認証・認可の依存関係。Bearer アクセストークン、owner、reauth、セッション所有者/共有先。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from minutes_api.db import db_dependency
from minutes_api.errors import forbidden, not_found, unauthorized
from minutes_api.models import AccessToken, MeetingSession, ReauthGrant, SessionShare, User
from minutes_api.security import now, token_hash


@dataclass
class Principal:
    user: User
    access_token: AccessToken

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def is_owner(self) -> bool:
        return self.user.role == "owner"


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def current_principal(request: Request, db: Session = Depends(db_dependency)) -> Principal:
    token = _bearer(request)
    if not token:
        raise unauthorized()
    row = db.execute(
        select(AccessToken, User)
        .join(User, User.id == AccessToken.user_id)
        .where(AccessToken.token_hash == token_hash(token))
    ).first()
    if row is None:
        raise unauthorized("トークンが無効です")
    access_token, user = row
    if access_token.revoked_at is not None or access_token.expires_at <= now() or user.disabled_at is not None:
        raise unauthorized("トークンが無効です")
    return Principal(user=user, access_token=access_token)


def require_owner(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.is_owner:
        raise forbidden("owner 権限が必要です")
    return principal


def require_reauth(principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> Principal:
    grant = db.execute(
        select(ReauthGrant)
        .where(ReauthGrant.access_token_id == principal.access_token.id, ReauthGrant.expires_at > now())
        .order_by(ReauthGrant.expires_at.desc())
    ).scalars().first()
    if grant is None:
        raise forbidden("この操作には再認証が必要です (POST /v1/auth/reauth)")
    return principal


def require_owner_reauth(principal: Principal = Depends(require_owner), db: Session = Depends(db_dependency)) -> Principal:
    return require_reauth(principal, db)


@dataclass
class SessionAccess:
    session: MeetingSession
    principal: Principal
    is_owner: bool  # セッション所有者か (アカウントの owner ロールではない)


def load_session(db: Session, session_id: uuid.UUID) -> MeetingSession | None:
    session = db.get(MeetingSession, session_id)
    if session is None or session.deleted_at is not None:
        return None
    return session


def session_access(
    session_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)
) -> SessionAccess:
    """所有者または共有先だけ。該当しない場合は存在を推測されないよう 404。"""
    session = load_session(db, session_id)
    if session is None:
        raise not_found("セッションが見つかりません")
    if session.owner_id == principal.id:
        return SessionAccess(session=session, principal=principal, is_owner=True)
    shared = db.execute(
        select(SessionShare.id).where(SessionShare.session_id == session.id, SessionShare.user_id == principal.id)
    ).first()
    if shared is None:
        raise not_found("セッションが見つかりません")
    return SessionAccess(session=session, principal=principal, is_owner=False)


def session_owner_access(access: SessionAccess = Depends(session_access)) -> SessionAccess:
    if not access.is_owner:
        raise forbidden("セッション所有者だけが実行できます")
    return access
