"""OAuth 2.0 Authorization Code + PKCE (S256)、リフレッシュのローテーション、失効 (ADR-0001)。"""

from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from minutes_api import accounts, audit
from minutes_api.browser import load_browser_session
from minutes_api.config import get_settings
from minutes_api.db import commit_rejection, db_dependency
from minutes_api.errors import ApiException
from minutes_api.models import AuthorizationCode, RefreshToken, User
from minutes_api.security import after, new_token, now, token_hash

router = APIRouter(tags=["oauth"])

_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


def _valid_redirect(uri: str) -> bool:
    parts = urlsplit(uri)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "[::1]", "::1"):
        return False
    return parts.path == "/callback" and not parts.query and not parts.fragment


def _oauth_error(status: int, oauth_error: str, message: str) -> ApiException:
    code = "unauthorized" if oauth_error in ("invalid_grant", "invalid_client") else "invalid_request"
    return ApiException(status, code, message, details={"oauth_error": oauth_error})


@router.get("/oauth/authorize", include_in_schema=True, summary="認可要求 (ブラウザー)")
def authorize(
    request: Request,
    response_type: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    code_challenge_method: str = "S256",
    scope: str = "",
    db: Session = Depends(db_dependency),
):
    settings = get_settings()
    if response_type != "code" or client_id != settings.oauth_client_id:
        raise _oauth_error(400, "unauthorized_client", "クライアントまたは応答種別が不正です")
    if not _valid_redirect(redirect_uri):
        raise _oauth_error(400, "invalid_request", "redirect_uri が許可されていません")
    if code_challenge_method != "S256" or not _CHALLENGE_RE.fullmatch(code_challenge):
        raise _oauth_error(400, "invalid_request", "PKCE (S256) が必要です")
    if not state or len(state) > 512:
        raise _oauth_error(400, "invalid_request", "state が必要です")
    browser = load_browser_session(request, db)
    if browser is None or browser.user_id is None:
        target = "/oauth/authorize?" + urlencode(dict(request.query_params))
        return RedirectResponse("/auth/login?" + urlencode({"next": target}), status_code=303)
    user = db.get(User, browser.user_id)
    if user is None or user.disabled_at is not None:
        raise _oauth_error(403, "access_denied", "このアカウントは利用できません")
    code = new_token()
    db.add(
        AuthorizationCode(
            code_hash=token_hash(code), user_id=user.id, client_id=client_id, redirect_uri=redirect_uri,
            code_challenge=code_challenge, scope=scope[:200], expires_at=after(settings.authorization_code_seconds),
        )
    )
    audit.record(db, "authorization_code_issued", actor_id=user.id, target_type="client", target_id=client_id)
    parts = urlsplit(redirect_uri)
    query = urlencode(dict(parse_qsl(parts.query)) | {"code": code, "state": state})
    # code をログへ残さないため、redirect 先 URL はアクセスログの対象外 (Location ヘッダーのみ)
    return RedirectResponse(urlunsplit((parts.scheme, parts.netloc, parts.path, query, "")), status_code=302)


@router.post("/oauth/token", summary="トークン交換")
def token(
    request: Request,
    grant_type: str = Form(...),
    code: str = Form(""),
    redirect_uri: str = Form(""),
    client_id: str = Form(""),
    code_verifier: str = Form(""),
    refresh_token: str = Form(""),
    db: Session = Depends(db_dependency),
) -> JSONResponse:
    settings = get_settings()
    if client_id and client_id != settings.oauth_client_id:
        raise _oauth_error(401, "invalid_client", "クライアントが不正です")
    if grant_type == "authorization_code":
        if not (code and code_verifier and redirect_uri):
            raise _oauth_error(400, "invalid_request", "code・code_verifier・redirect_uri が必要です")
        row = db.execute(select(AuthorizationCode).where(AuthorizationCode.code_hash == token_hash(code)).with_for_update()).scalars().first()
        if row is None or row.used_at is not None or row.expires_at <= now():
            raise _oauth_error(400, "invalid_grant", "認可コードが無効です")
        row.used_at = now()
        # 以降の検証に失敗しても認可コードの消費は確定させる (同じ code の再試行を許さない)
        if row.redirect_uri != redirect_uri:
            commit_rejection(db)
            raise _oauth_error(400, "invalid_grant", "redirect_uri が一致しません")
        digest = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        if digest != row.code_challenge:
            commit_rejection(db)
            raise _oauth_error(400, "invalid_grant", "PKCE の検証に失敗しました")
        user = db.get(User, row.user_id)
        if user is None or user.disabled_at is not None:
            commit_rejection(db)
            raise _oauth_error(400, "invalid_grant", "このアカウントは利用できません")
        issued = accounts.issue_tokens(db, user, scope=row.scope)
        audit.record(db, "tokens_issued", actor_id=user.id, metadata={"grant": "authorization_code"})
        return JSONResponse({key: value for key, value in issued.items() if not key.startswith("_")})
    if grant_type == "refresh_token":
        if not refresh_token:
            raise _oauth_error(400, "invalid_request", "refresh_token が必要です")
        row = db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash(refresh_token)).with_for_update()).scalars().first()
        if row is None:
            raise _oauth_error(400, "invalid_grant", "リフレッシュトークンが無効です")
        if row.used_at is not None:
            # 再使用検知: 系列全体を失効する
            accounts.revoke_family(db, row.family_id)
            audit.record(db, "refresh_reuse_detected", actor_id=row.user_id, target_type="token_family", target_id=row.family_id)
            commit_rejection(db)
            raise _oauth_error(400, "invalid_grant", "リフレッシュトークンが再使用されたため失効しました")
        if row.revoked_at is not None or row.expires_at <= now():
            raise _oauth_error(400, "invalid_grant", "リフレッシュトークンが無効です")
        user = db.get(User, row.user_id)
        if user is None or user.disabled_at is not None:
            accounts.revoke_family(db, row.family_id)
            commit_rejection(db)
            raise _oauth_error(400, "invalid_grant", "このアカウントは利用できません")
        row.used_at = now()
        issued = accounts.issue_tokens(db, user, scope="", family_id=row.family_id)
        row.replaced_by = issued["_refresh_id"]
        audit.record(db, "tokens_issued", actor_id=user.id, metadata={"grant": "refresh_token"})
        return JSONResponse({key: value for key, value in issued.items() if not key.startswith("_")})
    raise _oauth_error(400, "unsupported_grant_type", "対応していない grant_type です")


@router.post("/oauth/revoke", summary="トークン失効")
def revoke(token: str = Form(...), db: Session = Depends(db_dependency)) -> JSONResponse:
    row = db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash(token))).scalars().first()
    if row is not None:
        accounts.revoke_family(db, row.family_id)
        audit.record(db, "tokens_revoked", actor_id=row.user_id, target_type="token_family", target_id=row.family_id)
    # RFC 7009: 不明なトークンでも 200
    return JSONResponse({"revoked": True})
