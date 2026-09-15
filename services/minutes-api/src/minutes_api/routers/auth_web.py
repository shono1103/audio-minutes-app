"""ブラウザー向け認証画面: bootstrap 登録、招待消費、ログイン、ログアウト、WebAuthn。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from minutes_api import __version__, accounts, audit
from minutes_api.browser import (
    check_csrf,
    clear_browser_session,
    ensure_browser_session,
    load_browser_session,
    rotate_browser_session,
    safe_next,
)
from minutes_api.db import commit_rejection, db_dependency
from minutes_api.errors import ApiException
from minutes_api.models import BrowserReauthRequest, PasswordCredential, User, WebAuthnCredential
from minutes_api.ratelimit import client_key, limiter
from minutes_api.security import now, token_hash
from minutes_api.webauthn_support import (
    authentication_options,
    registration_options,
    verify_authentication,
    verify_registration,
)

router = APIRouter(tags=["auth-web"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _render(request: Request, name: str, response: Response | None = None, **context: Any) -> HTMLResponse:
    html = templates.TemplateResponse(request, name, {"version": __version__, **context})
    if response is not None:
        for key, value in response.headers.items():
            if key.lower() == "set-cookie":
                html.headers.append("set-cookie", value)
    return html


def _host(request: Request) -> str | None:
    return request.client.host if request.client else None


# --- bootstrap -----------------------------------------------------------------


@router.get("/auth/bootstrap/{token}")
def bootstrap_form(token: str, request: Request, db: Session = Depends(db_dependency)) -> Response:
    limiter.check(client_key(_host(request), "bootstrap"), 10, 60)
    if accounts.find_valid_bootstrap(db, token) is None:
        return _render(request, "message.html", title="初回登録", message="この登録リンクは無効か期限切れです。サーバー CLI で再発行してください。")
    response = Response()
    browser = ensure_browser_session(request, response, db)
    return _render(
        request,
        "register.html",
        response,
        title="初回 owner の登録",
        lead="このサーバーの最初の owner を登録します。",
        action=f"/auth/bootstrap/{token}",
        csrf_token=browser.csrf_token,
        next=None,
        email=None,
        email_locked=False,
        registration_kind="bootstrap",
    )


@router.post("/auth/bootstrap/{token}")
def bootstrap_submit(
    token: str,
    request: Request,
    email: str = Form(...),
    password: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(db_dependency),
) -> Response:
    limiter.check(client_key(_host(request), "bootstrap"), 10, 60)
    browser = load_browser_session(request, db, lock=True)
    if browser is None:
        raise ApiException(403, "forbidden", "セッションが無効です。ページを再読み込みしてください")
    check_csrf(browser, csrf_token)
    # トークン行をロックし、owner 作成と失効を同一 transaction で行う (同時使用・二重登録を防ぐ)
    row = accounts.find_valid_bootstrap(db, token, lock=True)
    if row is None or db.execute(select(User.id)).first() is not None:
        raise ApiException(409, "conflict", "この登録リンクは使用できません")
    if not password:
        return _render(
            request, "register.html", title="初回 owner の登録", lead="このサーバーの最初の owner を登録します。",
            action=f"/auth/bootstrap/{token}", csrf_token=browser.csrf_token,
            error="パスキーを登録するか、フォールバック用パスワードを入力してください",
            email=email, email_locked=False, next=None, registration_kind="bootstrap",
        )
    try:
        user, codes = accounts.create_user(db, email=email, role="owner", password=password)
    except ApiException as exc:
        return _render(
            request, "register.html", title="初回 owner の登録", lead="このサーバーの最初の owner を登録します。",
            action=f"/auth/bootstrap/{token}", csrf_token=browser.csrf_token, error=exc.message, email=email,
            email_locked=False, next=None, registration_kind="bootstrap",
        )
    row.used_at = now()
    audit.record(db, "bootstrap_consumed", actor_id=user.id, target_type="user", target_id=user.id)
    response = Response()
    new_browser = rotate_browser_session(response, db, browser, user)
    new_browser.pending_registration = {"purpose": "fresh_registration"}
    return _render(request, "registered.html", response, title="登録完了", recovery_codes=codes, csrf_token=new_browser.csrf_token, next=None)


# --- 招待 ------------------------------------------------------------------------


@router.get("/auth/invite/{token}")
def invite_form(token: str, request: Request, db: Session = Depends(db_dependency)) -> Response:
    limiter.check(client_key(_host(request), "invite"), 10, 60)
    invitation = accounts.find_valid_invitation(db, token)
    if invitation is None:
        return _render(request, "message.html", title="招待", message="この招待は無効か期限切れです。owner に再発行を依頼してください。")
    response = Response()
    browser = ensure_browser_session(request, response, db)
    return _render(
        request, "register.html", response, title="アカウント登録", lead="owner からの招待でアカウントを登録します。",
        action=f"/auth/invite/{token}", csrf_token=browser.csrf_token, email=invitation.email,
        email_locked=True, next=None, registration_kind="invite",
    )


@router.post("/auth/invite/{token}")
def invite_submit(
    token: str,
    request: Request,
    email: str = Form(...),
    password: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(db_dependency),
) -> Response:
    limiter.check(client_key(_host(request), "invite"), 10, 60)
    browser = load_browser_session(request, db, lock=True)
    if browser is None:
        raise ApiException(403, "forbidden", "セッションが無効です。ページを再読み込みしてください")
    check_csrf(browser, csrf_token)
    invitation = accounts.find_valid_invitation(db, token, lock=True)
    if invitation is None:
        raise ApiException(409, "conflict", "この招待は使用できません")
    if accounts.normalize_email(email) != invitation.email:
        raise ApiException(400, "invalid_request", "招待されたメールアドレスと一致しません")
    if not password:
        return _render(
            request, "register.html", title="アカウント登録", lead="owner からの招待でアカウントを登録します。",
            action=f"/auth/invite/{token}", csrf_token=browser.csrf_token,
            error="パスキーを登録するか、フォールバック用パスワードを入力してください",
            email=invitation.email, email_locked=True, next=None, registration_kind="invite",
        )
    try:
        user, codes = accounts.create_user(db, email=email, role=invitation.role, password=password)
    except ApiException as exc:
        return _render(
            request, "register.html", title="アカウント登録", lead="owner からの招待でアカウントを登録します。",
            action=f"/auth/invite/{token}", csrf_token=browser.csrf_token, error=exc.message,
            email=invitation.email, email_locked=True, next=None, registration_kind="invite",
        )
    invitation.used_at = now()
    audit.record(db, "invitation_consumed", actor_id=user.id, target_type="invitation", target_id=invitation.id)
    response = Response()
    new_browser = rotate_browser_session(response, db, browser, user)
    new_browser.pending_registration = {"purpose": "fresh_registration"}
    return _render(request, "registered.html", response, title="登録完了", recovery_codes=codes, csrf_token=new_browser.csrf_token, next=None)


def _registration_source(db: Session, kind: str, token: str, *, lock: bool = False):
    if kind == "bootstrap":
        row = accounts.find_valid_bootstrap(db, token, lock=lock)
        if row is None or db.execute(select(User.id)).first() is not None:
            return None
        return row, None, "owner"
    if kind == "invite":
        row = accounts.find_valid_invitation(db, token, lock=lock)
        if row is None:
            return None
        return row, row.email, row.role
    return None


async def _initial_passkey_options(
    kind: str,
    token: str,
    request: Request,
    db: Session,
) -> JSONResponse:
    limiter.check(client_key(_host(request), kind), 10, 60)
    body_error: ApiException | None = None
    submitted_email: str | None = None
    try:
        body = await request.json()
    except (UnicodeDecodeError, ValueError):
        body_error = ApiException(400, "invalid_request", "リクエストの形式が不正です")
    else:
        if not isinstance(body, dict):
            body_error = ApiException(400, "invalid_request", "リクエストの形式が不正です")
        else:
            raw_email = body.get("email")
            if raw_email is not None and not isinstance(raw_email, str):
                body_error = ApiException(400, "invalid_request", "メールアドレスの形式が不正です")
            elif raw_email:
                try:
                    submitted_email = accounts.normalize_email(raw_email)
                except ApiException as exc:
                    body_error = exc

    # このendpointはasyncだがDB sessionは同期式。body待ちの前に行ロックを取ると、
    # 同じbrowser sessionへの後続要求がevent loop上でロック待ちし、先行bodyを
    # 読み進められなくなる。DB transactionへ入る前にbodyを完全受信・検証する。
    browser = load_browser_session(request, db, lock=True)
    if browser is None:
        raise ApiException(403, "forbidden", "セッションが無効です。ページを再読み込みしてください")
    _csrf_from_header(request, browser)
    # bodyは先読み済みだが、入力エラーの開示順は従来どおりCSRF検証後に保つ。
    if body_error is not None:
        raise body_error
    source = _registration_source(db, kind, token, lock=True)
    if source is None:
        raise ApiException(409, "conflict", "この登録リンクは使用できません")
    row, locked_email, role = source
    email = locked_email or submitted_email
    if email is None:
        raise ApiException(400, "invalid_request", "メールアドレスの形式が不正です")
    if locked_email is not None and submitted_email is not None and submitted_email != locked_email:
        raise ApiException(400, "invalid_request", "招待されたメールアドレスと一致しません")
    if db.execute(select(User.id).where(User.email == email)).first() is not None:
        raise ApiException(409, "conflict", "この登録は完了できません")
    pending_user_id = uuid.uuid4()
    pending_user = User(id=pending_user_id, email=email, role=role)
    options, challenge = registration_options(pending_user, [])
    browser.pending_registration = {
        "purpose": kind,
        "token_hash": token_hash(token),
        "user_id": str(pending_user_id),
        "email": email,
        "role": role,
        "source_id": str(row.id),
    }
    browser.webauthn_challenge = challenge
    browser.webauthn_context = {
        "purpose": "initial_passkey_registration",
        "registration_kind": kind,
        "token_hash": token_hash(token),
    }
    return JSONResponse(options)


async def _initial_passkey_verify(
    kind: str,
    token: str,
    request: Request,
    db: Session,
) -> JSONResponse:
    limiter.check(client_key(_host(request), kind), 10, 60)
    credential = await request.json()
    browser = load_browser_session(request, db, lock=True)
    pending = browser.pending_registration if browser is not None else None
    context = browser.webauthn_context if browser is not None else None
    expected_hash = token_hash(token)
    if (
        browser is None
        or not browser.webauthn_challenge
        or not pending
        or pending.get("purpose") != kind
        or pending.get("token_hash") != expected_hash
        or (context or {}).get("purpose") != "initial_passkey_registration"
        or (context or {}).get("registration_kind") != kind
        or (context or {}).get("token_hash") != expected_hash
    ):
        raise ApiException(403, "forbidden", "登録セッションが無効です")
    _csrf_from_header(request, browser)
    source = _registration_source(db, kind, token, lock=True)
    if source is None or str(source[0].id) != pending.get("source_id"):
        raise ApiException(409, "conflict", "この登録リンクは使用できません")

    challenge = browser.webauthn_challenge
    # 検証の成否にかかわらずchallengeと登録途中状態は一回限り。
    browser.webauthn_challenge = None
    browser.webauthn_context = None
    browser.pending_registration = None
    try:
        credential_id, public_key, sign_count, transports = verify_registration(credential, challenge)
    except Exception as exc:  # noqa: BLE001
        commit_rejection(db)
        raise ApiException(400, "invalid_request", "パスキーの登録を検証できません") from exc
    if db.execute(select(WebAuthnCredential.id).where(WebAuthnCredential.credential_id == credential_id)).first():
        commit_rejection(db)
        raise ApiException(409, "conflict", "このパスキーは既に登録されています")
    try:
        user, codes = accounts.create_user(
            db,
            user_id=uuid.UUID(str(pending["user_id"])),
            email=str(pending["email"]),
            role=str(pending["role"]),
            password=None,
        )
    except ApiException:
        commit_rejection(db)
        raise
    db.add(
        WebAuthnCredential(
            user_id=user.id,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=sign_count,
            transports=transports,
            label=(credential.get("label") or None),
        )
    )
    source_row = source[0]
    source_row.used_at = now()
    event = "bootstrap_consumed" if kind == "bootstrap" else "invitation_consumed"
    target_type = "user" if kind == "bootstrap" else "invitation"
    target_id = user.id if kind == "bootstrap" else source_row.id
    audit.record(db, event, actor_id=user.id, target_type=target_type, target_id=target_id)
    audit.record(db, "passkey_registered", actor_id=user.id, target_type="user", target_id=user.id)
    response = JSONResponse({"registered": True, "recovery_codes": codes, "next": "/auth/login"})
    rotate_browser_session(response, db, browser, user)
    return response


@router.post("/auth/bootstrap/{token}/webauthn/options")
async def bootstrap_passkey_options(
    token: str, request: Request, db: Session = Depends(db_dependency)
) -> JSONResponse:
    return await _initial_passkey_options("bootstrap", token, request, db)


@router.post("/auth/bootstrap/{token}/webauthn/verify")
async def bootstrap_passkey_verify(
    token: str, request: Request, db: Session = Depends(db_dependency)
) -> JSONResponse:
    return await _initial_passkey_verify("bootstrap", token, request, db)


@router.post("/auth/invite/{token}/webauthn/options")
async def invite_passkey_options(
    token: str, request: Request, db: Session = Depends(db_dependency)
) -> JSONResponse:
    return await _initial_passkey_options("invite", token, request, db)


@router.post("/auth/invite/{token}/webauthn/verify")
async def invite_passkey_verify(
    token: str, request: Request, db: Session = Depends(db_dependency)
) -> JSONResponse:
    return await _initial_passkey_verify("invite", token, request, db)


# --- ログイン ---------------------------------------------------------------------


@router.get("/auth/login")
def login_form(request: Request, next: str | None = None, db: Session = Depends(db_dependency)) -> Response:
    response = Response()
    browser = ensure_browser_session(request, response, db)
    if browser.user_id is not None and next:
        return RedirectResponse(safe_next(next), status_code=303, headers=dict(response.headers))
    return _render(request, "login.html", response, title="ログイン", csrf_token=browser.csrf_token, next=safe_next(next) if next else None)


@router.post("/auth/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    csrf_token: str = Form(...),
    password: str = Form(""),
    recovery_code: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(db_dependency),
) -> Response:
    limiter.check(client_key(_host(request), "login"), 10, 60)
    browser = load_browser_session(request, db)
    if browser is None:
        raise ApiException(403, "forbidden", "セッションが無効です。ページを再読み込みしてください")
    check_csrf(browser, csrf_token)
    user: User | None = None
    if password:
        user = accounts.authenticate_password(db, email, password)
    elif recovery_code:
        candidate = db.execute(select(User).where(User.email == email.strip().lower())).scalars().first()
        if candidate is not None and candidate.disabled_at is None and accounts.consume_recovery_code(db, candidate, recovery_code):
            user = candidate
            audit.record(db, "recovery_code_used", actor_id=user.id, target_type="user", target_id=user.id)
    if user is None:
        audit.record(db, "login_failed", actor_id=None)
        return _render(
            request, "login.html", title="ログイン", csrf_token=browser.csrf_token, next=next or None,
            error="メールアドレスまたは認証情報が正しくありません",
        )
    audit.record(db, "login_succeeded", actor_id=user.id, target_type="user", target_id=user.id, metadata={"method": "password" if password else "recovery"})
    response = RedirectResponse(safe_next(next), status_code=303)
    rotate_browser_session(response, db, browser, user)
    return response


@router.api_route("/auth/logout", methods=["GET", "POST"])
def logout(request: Request, db: Session = Depends(db_dependency)) -> Response:
    browser = load_browser_session(request, db)
    response = RedirectResponse("/auth/login", status_code=303)
    if browser is not None and browser.user_id is not None:
        audit.record(db, "browser_logout", actor_id=browser.user_id)
    clear_browser_session(response, db, browser)
    return response


# --- WebAuthn --------------------------------------------------------------------


def _csrf_from_header(request: Request, browser) -> None:
    check_csrf(browser, request.headers.get("x-csrf-token"))


@router.post("/auth/webauthn/register/options")
def webauthn_register_options(request: Request, db: Session = Depends(db_dependency)) -> JSONResponse:
    browser = load_browser_session(request, db, lock=True)
    if (
        browser is None
        or browser.user_id is None
        or (browser.pending_registration or {}).get("purpose") != "fresh_registration"
    ):
        raise ApiException(403, "forbidden", "初回登録セッションが無効です")
    _csrf_from_header(request, browser)
    user = db.get(User, browser.user_id)
    existing = list(db.execute(select(WebAuthnCredential).where(WebAuthnCredential.user_id == user.id)).scalars())
    options, challenge = registration_options(user, existing)
    browser.webauthn_challenge = challenge
    browser.webauthn_context = {"purpose": "register"}
    return JSONResponse(options)


@router.post("/auth/webauthn/register/verify")
async def webauthn_register_verify(request: Request, db: Session = Depends(db_dependency)) -> JSONResponse:
    credential = await request.json()
    browser = load_browser_session(request, db, lock=True)
    if (
        browser is None
        or browser.user_id is None
        or (browser.pending_registration or {}).get("purpose") != "fresh_registration"
        or not browser.webauthn_challenge
        or (browser.webauthn_context or {}).get("purpose") != "register"
    ):
        raise ApiException(401, "unauthorized", "ログインが必要です")
    _csrf_from_header(request, browser)
    challenge = browser.webauthn_challenge
    # challenge は検証結果にかかわらず一回限り。先にロック下で消費し、拒否時も
    # commit_rejection で確定するため、失敗payloadや同時要求で再利用できない。
    browser.webauthn_challenge = None
    browser.webauthn_context = None
    try:
        credential_id, public_key, sign_count, transports = verify_registration(credential, challenge)
    except ApiException:
        commit_rejection(db)
        raise
    except Exception as exc:  # noqa: BLE001 - ライブラリの例外種別は多岐にわたる
        commit_rejection(db)
        raise ApiException(400, "invalid_request", "パスキーの登録を検証できません") from exc
    if db.execute(select(WebAuthnCredential.id).where(WebAuthnCredential.credential_id == credential_id)).first():
        commit_rejection(db)
        raise ApiException(409, "conflict", "このパスキーは既に登録されています")
    db.add(
        WebAuthnCredential(
            user_id=browser.user_id, credential_id=credential_id, public_key=public_key, sign_count=sign_count,
            transports=transports, label=(credential.get("label") or None),
        )
    )
    browser.pending_registration = None
    audit.record(db, "passkey_registered", actor_id=browser.user_id, target_type="user", target_id=browser.user_id)
    return JSONResponse({"registered": True})


@router.post("/auth/webauthn/login/options")
def webauthn_login_options(request: Request, db: Session = Depends(db_dependency)) -> JSONResponse:
    limiter.check(client_key(_host(request), "login"), 10, 60)
    browser = load_browser_session(request, db, lock=True)
    if browser is None:
        raise ApiException(403, "forbidden", "セッションが無効です")
    _csrf_from_header(request, browser)
    options, challenge = authentication_options()
    browser.webauthn_challenge = challenge
    browser.webauthn_context = {"purpose": "login"}
    return JSONResponse(options)


@router.post("/auth/webauthn/login/verify")
async def webauthn_login_verify(request: Request, db: Session = Depends(db_dependency)) -> JSONResponse:
    limiter.check(client_key(_host(request), "login"), 10, 60)
    body = await request.json()
    browser = load_browser_session(request, db, lock=True)
    if (
        browser is None
        or not browser.webauthn_challenge
        or (browser.webauthn_context or {}).get("purpose") != "login"
    ):
        raise ApiException(403, "forbidden", "セッションが無効です")
    _csrf_from_header(request, browser)
    challenge = browser.webauthn_challenge
    browser.webauthn_challenge = None
    browser.webauthn_context = None
    raw_id = body.get("rawId") or body.get("id")
    stored = db.execute(select(WebAuthnCredential).where(WebAuthnCredential.credential_id == raw_id)).scalars().first()
    if stored is None:
        commit_rejection(db)
        raise ApiException(401, "unauthorized", "パスキーを確認できません")
    try:
        new_count = verify_authentication({k: v for k, v in body.items() if k != "next"}, challenge, stored)
    except Exception as exc:  # noqa: BLE001
        audit.record(db, "login_failed", actor_id=None)
        commit_rejection(db)
        raise ApiException(401, "unauthorized", "パスキーを確認できません") from exc
    user = db.get(User, stored.user_id)
    if user is None or user.disabled_at is not None:
        commit_rejection(db)
        raise ApiException(401, "unauthorized", "パスキーを確認できません")
    stored.sign_count = new_count
    stored.last_used_at = now()
    audit.record(db, "login_succeeded", actor_id=user.id, target_type="user", target_id=user.id, metadata={"method": "passkey"})
    response = JSONResponse({"next": safe_next(body.get("next"))})
    rotate_browser_session(response, db, browser, user)
    return response


# --- nativeから開始するbrowser-bound再認証 --------------------------------------


def _reauth_request(db: Session, token: str, *, purpose: str = "reauth", lock: bool = False):
    row = accounts.find_browser_reauth(db, token, lock=lock)
    if row is None or row.purpose != purpose or row.expires_at <= now():
        return None
    return row


@router.get("/auth/reauth/completed")
def reauth_completed(request: Request) -> Response:
    return _render(
        request,
        "message.html",
        title="再認証完了",
        message="再認証が完了しました。このウィンドウを閉じてアプリへ戻ってください。",
    )


@router.get("/auth/reauth/{token}")
def reauth_form(token: str, request: Request, db: Session = Depends(db_dependency)) -> Response:
    row = accounts.find_browser_reauth(db, token)
    if row is None or row.expires_at <= now():
        return _render(request, "message.html", title="再認証", message="この再認証リンクは無効か期限切れです。")
    if row.completed_at is not None:
        return _render(
            request,
            "message.html",
            title="再認証完了",
            message="再認証は完了しています。このウィンドウを閉じてアプリへ戻ってください。",
        )
    user = db.get(User, row.user_id)
    if user is None or user.disabled_at is not None:
        return _render(request, "message.html", title="再認証", message="この再認証リンクは利用できません。")
    response = Response()
    browser = ensure_browser_session(request, response, db)
    passkey_count = len(
        list(db.execute(select(WebAuthnCredential.id).where(WebAuthnCredential.user_id == user.id)).all())
    )
    return _render(
        request,
        "reauth.html",
        response,
        title="再認証",
        token=token,
        csrf_token=browser.csrf_token,
        email=user.email,
        passkey_count=passkey_count,
        password_available=db.get(PasswordCredential, user.id) is not None,
        action=f"/auth/reauth/{token}",
        passkey_base=f"/auth/reauth/{token}/webauthn",
        passkey_success_url="/auth/reauth/completed",
    )


@router.post("/auth/reauth/{token}")
def reauth_password_submit(
    token: str,
    request: Request,
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(db_dependency),
) -> Response:
    browser = load_browser_session(request, db, lock=True)
    if browser is None:
        raise ApiException(403, "forbidden", "セッションが無効です。ページを再読み込みしてください")
    check_csrf(browser, csrf_token)
    row = _reauth_request(db, token, lock=True)
    if row is None or row.completed_at is not None:
        raise ApiException(409, "conflict", "再認証要求は無効か使用済みです")
    limiter.check(client_key(_host(request), f"reauth:{row.user_id}"), 10, 60)
    user = db.get(User, row.user_id)
    if user is None or not accounts.verify_user_password(db, user, password):
        audit.record(db, "reauth_failed", actor_id=row.user_id, target_type="reauth", target_id=row.id)
        return _render(
            request,
            "reauth.html",
            title="再認証",
            token=token,
            csrf_token=browser.csrf_token,
            email=user.email if user is not None else "",
            passkey_count=len(
                list(db.execute(select(WebAuthnCredential.id).where(WebAuthnCredential.user_id == row.user_id)).all())
            ),
            password_available=db.get(PasswordCredential, row.user_id) is not None,
            action=f"/auth/reauth/{token}",
            passkey_base=f"/auth/reauth/{token}/webauthn",
            passkey_success_url="/auth/reauth/completed",
            error="パスワードを確認できません",
        )
    accounts.complete_browser_reauth(db, row, user)
    return _render(
        request,
        "message.html",
        title="再認証完了",
        message="再認証が完了しました。このウィンドウを閉じてアプリへ戻ってください。",
    )


@router.post("/auth/reauth/{token}/webauthn/options")
def reauth_webauthn_options(token: str, request: Request, db: Session = Depends(db_dependency)) -> JSONResponse:
    browser = load_browser_session(request, db, lock=True)
    if browser is None:
        raise ApiException(403, "forbidden", "セッションが無効です")
    _csrf_from_header(request, browser)
    row = _reauth_request(db, token, lock=True)
    if row is None or row.completed_at is not None:
        raise ApiException(409, "conflict", "再認証要求は無効か使用済みです")
    existing = list(
        db.execute(select(WebAuthnCredential).where(WebAuthnCredential.user_id == row.user_id)).scalars()
    )
    if not existing:
        raise ApiException(409, "conflict", "このアカウントにパスキーは登録されていません")
    options, challenge = authentication_options(existing)
    browser.webauthn_challenge = challenge
    browser.webauthn_context = {"purpose": "reauth", "request_id": str(row.id)}
    return JSONResponse(options)


@router.post("/auth/reauth/{token}/webauthn/verify")
async def reauth_webauthn_verify(
    token: str, request: Request, db: Session = Depends(db_dependency)
) -> JSONResponse:
    body = await request.json()
    browser = load_browser_session(request, db, lock=True)
    row = _reauth_request(db, token, lock=True)
    if (
        browser is None
        or row is None
        or row.completed_at is not None
        or not browser.webauthn_challenge
        or (browser.webauthn_context or {}).get("purpose") != "reauth"
        or (browser.webauthn_context or {}).get("request_id") != str(row.id)
    ):
        raise ApiException(403, "forbidden", "再認証セッションが無効です")
    _csrf_from_header(request, browser)
    challenge = browser.webauthn_challenge
    browser.webauthn_challenge = None
    browser.webauthn_context = None
    raw_id = body.get("rawId") or body.get("id")
    stored = db.execute(
        select(WebAuthnCredential).where(
            WebAuthnCredential.credential_id == raw_id,
            WebAuthnCredential.user_id == row.user_id,
        )
    ).scalars().first()
    if stored is None:
        commit_rejection(db)
        raise ApiException(401, "unauthorized", "パスキーを確認できません")
    try:
        new_count = verify_authentication(body, challenge, stored)
    except Exception as exc:  # noqa: BLE001
        audit.record(db, "reauth_failed", actor_id=row.user_id, target_type="reauth", target_id=row.id)
        commit_rejection(db)
        raise ApiException(401, "unauthorized", "パスキーを確認できません") from exc
    user = db.get(User, row.user_id)
    if user is None or user.disabled_at is not None:
        commit_rejection(db)
        raise ApiException(401, "unauthorized", "パスキーを確認できません")
    stored.sign_count = new_count
    stored.last_used_at = now()
    accounts.complete_browser_reauth(db, row, user)
    return JSONResponse({"completed": True})


# --- 既存ユーザーのパスキー後日追加 -----------------------------------------------


def _passkey_registration_request(db: Session, token: str, *, lock: bool = False) -> BrowserReauthRequest | None:
    return _reauth_request(db, token, purpose="passkey_registration", lock=lock)


def _render_passkey_add(
    request: Request,
    token: str,
    browser,
    response: Response | None = None,
) -> HTMLResponse:
    return _render(
        request,
        "passkey_add.html",
        response,
        title="パスキーを追加",
        token=token,
        csrf_token=browser.csrf_token,
    )


@router.get("/auth/account/passkeys/{token}")
def account_passkey_form(token: str, request: Request, db: Session = Depends(db_dependency)) -> Response:
    row = _passkey_registration_request(db, token)
    if row is None:
        return _render(request, "message.html", title="パスキーを追加", message="この登録リンクは無効か期限切れです。")
    if row.action_completed_at is not None:
        return _render(
            request,
            "message.html",
            title="パスキー登録完了",
            message="パスキーを登録しました。このウィンドウを閉じてアプリへ戻ってください。",
        )
    user = db.get(User, row.user_id)
    if user is None or user.disabled_at is not None:
        return _render(request, "message.html", title="パスキーを追加", message="この登録リンクは利用できません。")
    response = Response()
    browser = ensure_browser_session(request, response, db)
    if row.completed_at is not None:
        if browser.user_id != row.user_id:
            return _render(
                request,
                "message.html",
                response,
                title="パスキーを追加",
                message="本人確認セッションが失われました。アプリから登録をやり直してください。",
            )
        return _render_passkey_add(request, token, browser, response)
    passkey_count = len(
        list(db.execute(select(WebAuthnCredential.id).where(WebAuthnCredential.user_id == user.id)).all())
    )
    return _render(
        request,
        "reauth.html",
        response,
        title="パスキー追加の本人確認",
        token=token,
        csrf_token=browser.csrf_token,
        email=user.email,
        passkey_count=passkey_count,
        password_available=db.get(PasswordCredential, user.id) is not None,
        action=f"/auth/account/passkeys/{token}",
        passkey_base=f"/auth/account/passkeys/{token}/reauth/webauthn",
        passkey_success_url=f"/auth/account/passkeys/{token}",
    )


@router.post("/auth/account/passkeys/{token}")
def account_passkey_password_reauth(
    token: str,
    request: Request,
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(db_dependency),
) -> Response:
    browser = load_browser_session(request, db, lock=True)
    if browser is None:
        raise ApiException(403, "forbidden", "セッションが無効です。ページを再読み込みしてください")
    check_csrf(browser, csrf_token)
    row = _passkey_registration_request(db, token, lock=True)
    if row is None or row.completed_at is not None or row.action_completed_at is not None:
        raise ApiException(409, "conflict", "パスキー登録要求は無効か使用済みです")
    limiter.check(client_key(_host(request), f"reauth:{row.user_id}"), 10, 60)
    user = db.get(User, row.user_id)
    if user is None or not accounts.verify_user_password(db, user, password):
        audit.record(db, "reauth_failed", actor_id=row.user_id, target_type="reauth", target_id=row.id)
        return _render(
            request,
            "reauth.html",
            title="パスキー追加の本人確認",
            token=token,
            csrf_token=browser.csrf_token,
            email=user.email if user is not None else "",
            passkey_count=len(
                list(db.execute(select(WebAuthnCredential.id).where(WebAuthnCredential.user_id == row.user_id)).all())
            ),
            password_available=db.get(PasswordCredential, row.user_id) is not None,
            action=f"/auth/account/passkeys/{token}",
            passkey_base=f"/auth/account/passkeys/{token}/reauth/webauthn",
            passkey_success_url=f"/auth/account/passkeys/{token}",
            error="パスワードを確認できません",
        )
    grant = accounts.complete_browser_reauth(db, row, user)
    row.expires_at = min(row.expires_at, grant.expires_at)
    response = RedirectResponse(f"/auth/account/passkeys/{token}", status_code=303)
    rotate_browser_session(response, db, browser, user)
    return response


@router.post("/auth/account/passkeys/{token}/reauth/webauthn/options")
def account_passkey_reauth_options(
    token: str, request: Request, db: Session = Depends(db_dependency)
) -> JSONResponse:
    browser = load_browser_session(request, db, lock=True)
    if browser is None:
        raise ApiException(403, "forbidden", "セッションが無効です")
    _csrf_from_header(request, browser)
    row = _passkey_registration_request(db, token, lock=True)
    if row is None or row.completed_at is not None or row.action_completed_at is not None:
        raise ApiException(409, "conflict", "パスキー登録要求は無効か使用済みです")
    existing = list(
        db.execute(select(WebAuthnCredential).where(WebAuthnCredential.user_id == row.user_id)).scalars()
    )
    if not existing:
        raise ApiException(409, "conflict", "このアカウントにパスキーは登録されていません")
    options, challenge = authentication_options(existing)
    browser.webauthn_challenge = challenge
    browser.webauthn_context = {"purpose": "passkey_registration_reauth", "request_id": str(row.id)}
    return JSONResponse(options)


@router.post("/auth/account/passkeys/{token}/reauth/webauthn/verify")
async def account_passkey_reauth_verify(
    token: str, request: Request, db: Session = Depends(db_dependency)
) -> JSONResponse:
    body = await request.json()
    browser = load_browser_session(request, db, lock=True)
    row = _passkey_registration_request(db, token, lock=True)
    if (
        browser is None
        or row is None
        or row.completed_at is not None
        or row.action_completed_at is not None
        or not browser.webauthn_challenge
        or (browser.webauthn_context or {}).get("purpose") != "passkey_registration_reauth"
        or (browser.webauthn_context or {}).get("request_id") != str(row.id)
    ):
        raise ApiException(403, "forbidden", "再認証セッションが無効です")
    _csrf_from_header(request, browser)
    challenge = browser.webauthn_challenge
    browser.webauthn_challenge = None
    browser.webauthn_context = None
    raw_id = body.get("rawId") or body.get("id")
    stored = db.execute(
        select(WebAuthnCredential).where(
            WebAuthnCredential.credential_id == raw_id,
            WebAuthnCredential.user_id == row.user_id,
        )
    ).scalars().first()
    if stored is None:
        commit_rejection(db)
        raise ApiException(401, "unauthorized", "パスキーを確認できません")
    try:
        new_count = verify_authentication(body, challenge, stored)
    except Exception as exc:  # noqa: BLE001
        audit.record(db, "reauth_failed", actor_id=row.user_id, target_type="reauth", target_id=row.id)
        commit_rejection(db)
        raise ApiException(401, "unauthorized", "パスキーを確認できません") from exc
    user = db.get(User, row.user_id)
    if user is None or user.disabled_at is not None:
        commit_rejection(db)
        raise ApiException(401, "unauthorized", "パスキーを確認できません")
    stored.sign_count = new_count
    stored.last_used_at = now()
    grant = accounts.complete_browser_reauth(db, row, user)
    row.expires_at = min(row.expires_at, grant.expires_at)
    response = JSONResponse({"completed": True, "next": f"/auth/account/passkeys/{token}"})
    rotate_browser_session(response, db, browser, user)
    return response


@router.post("/auth/account/passkeys/{token}/webauthn/register/options")
def account_passkey_register_options(
    token: str, request: Request, db: Session = Depends(db_dependency)
) -> JSONResponse:
    browser = load_browser_session(request, db, lock=True)
    row = _passkey_registration_request(db, token, lock=True)
    if (
        browser is None
        or row is None
        or row.completed_at is None
        or row.action_completed_at is not None
        or browser.user_id != row.user_id
    ):
        raise ApiException(403, "forbidden", "パスキー登録セッションが無効です")
    _csrf_from_header(request, browser)
    user = db.get(User, row.user_id)
    if user is None or user.disabled_at is not None:
        raise ApiException(403, "forbidden", "パスキー登録セッションが無効です")
    existing = list(
        db.execute(select(WebAuthnCredential).where(WebAuthnCredential.user_id == user.id)).scalars()
    )
    options, challenge = registration_options(user, existing)
    browser.webauthn_challenge = challenge
    browser.webauthn_context = {"purpose": "passkey_add", "request_id": str(row.id)}
    return JSONResponse(options)


@router.post("/auth/account/passkeys/{token}/webauthn/register/verify")
async def account_passkey_register_verify(
    token: str, request: Request, db: Session = Depends(db_dependency)
) -> JSONResponse:
    credential = await request.json()
    browser = load_browser_session(request, db, lock=True)
    row = _passkey_registration_request(db, token, lock=True)
    if (
        browser is None
        or row is None
        or row.completed_at is None
        or row.action_completed_at is not None
        or browser.user_id != row.user_id
        or not browser.webauthn_challenge
        or (browser.webauthn_context or {}).get("purpose") != "passkey_add"
        or (browser.webauthn_context or {}).get("request_id") != str(row.id)
    ):
        raise ApiException(403, "forbidden", "パスキー登録セッションが無効です")
    _csrf_from_header(request, browser)
    challenge = browser.webauthn_challenge
    browser.webauthn_challenge = None
    browser.webauthn_context = None
    try:
        credential_id, public_key, sign_count, transports = verify_registration(credential, challenge)
    except Exception as exc:  # noqa: BLE001
        commit_rejection(db)
        raise ApiException(400, "invalid_request", "パスキーの登録を検証できません") from exc
    if db.execute(select(WebAuthnCredential.id).where(WebAuthnCredential.credential_id == credential_id)).first():
        commit_rejection(db)
        raise ApiException(409, "conflict", "このパスキーは既に登録されています")
    db.add(
        WebAuthnCredential(
            user_id=row.user_id,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=sign_count,
            transports=transports,
            label=(credential.get("label") or None),
        )
    )
    row.action_completed_at = now()
    audit.record(db, "passkey_registered", actor_id=row.user_id, target_type="user", target_id=row.user_id)
    return JSONResponse({"registered": True})
