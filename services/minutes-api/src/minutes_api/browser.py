"""認証画面用のブラウザーセッション (cookie)。cookie にはランダムトークンだけを置く。"""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from minutes_api.config import get_settings
from minutes_api.errors import ApiException
from minutes_api.models import BrowserSession, User
from minutes_api.security import new_token, now, token_hash

COOKIE_NAME = "am_session"
SESSION_LIFETIME = timedelta(hours=12)


def _secure_cookie() -> bool:
    return get_settings().public_base_url.startswith("https://")


def load_browser_session(request: Request, db: Session, *, lock: bool = False) -> BrowserSession | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    statement = select(BrowserSession).where(BrowserSession.token_hash == token_hash(token))
    if lock:
        statement = statement.with_for_update()
    row = db.execute(statement).scalars().first()
    if row is None or row.expires_at <= now():
        return None
    return row


def ensure_browser_session(request: Request, response: Response, db: Session) -> BrowserSession:
    existing = load_browser_session(request, db)
    if existing is not None:
        return existing
    token = new_token()
    row = BrowserSession(token_hash=token_hash(token), csrf_token=secrets.token_hex(32), expires_at=now() + SESSION_LIFETIME)
    db.add(row)
    db.flush()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(),
        path="/",
    )
    return row


def rotate_browser_session(response: Response, db: Session, old: BrowserSession | None, user: User) -> BrowserSession:
    """ログイン成功時にセッション固定化を防ぐため新しいトークンへ切り替える。"""
    if old is not None:
        db.delete(old)
    token = new_token()
    row = BrowserSession(
        token_hash=token_hash(token), user_id=user.id, csrf_token=secrets.token_hex(32), expires_at=now() + SESSION_LIFETIME
    )
    db.add(row)
    db.flush()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(),
        path="/",
    )
    return row


def clear_browser_session(response: Response, db: Session, row: BrowserSession | None) -> None:
    if row is not None:
        db.delete(row)
    response.delete_cookie(COOKIE_NAME, path="/")


def check_csrf(session: BrowserSession, token: str | None) -> None:
    if not token or not secrets.compare_digest(session.csrf_token, token):
        raise ApiException(403, "forbidden", "CSRF トークンが一致しません")


def safe_next(value: str | None) -> str:
    """相対パスだけ許可し、外部 URL への open redirect を防ぐ。"""
    if not value:
        return "/auth/login"
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/auth/login"
