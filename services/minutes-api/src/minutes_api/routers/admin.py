"""owner 専用管理: 招待、ユーザー、保持期間、監査ログ、Claude 接続 (再認証必須)。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from minutes_api import accounts, audit, jobs, minutes_service, retention
from minutes_api.config import get_settings
from minutes_api.db import db_dependency
from minutes_api.deps import Principal, require_owner, require_owner_reauth
from minutes_api.errors import ApiException, not_found
from minutes_api.models import AuditLog, ClaudeAuthSession, Invitation, Setting, User
from minutes_api.security import after, new_token, now, token_hash
from minutes_api.worker_client import WorkerControlClient

router = APIRouter(prefix="/v1/admin", tags=["admin"])
CLAUDE_LOGIN_FAILURE_CODES = {
    "claude_not_authenticated",
    "claude_cli_incompatible",
    "cli_missing",
    "cli_incompatible",
    "credential_conflict",
    "rate_limited",
    "expired",
    "internal",
}


class InvitationRequest(BaseModel):
    email: str
    role: Literal["owner", "member"] = "member"
    expires_hours: int = Field(default=24, ge=1, le=168)


class UserPatch(BaseModel):
    role: Literal["owner", "member"] | None = None
    disabled: bool | None = None


class RetentionSettings(BaseModel):
    upload_hours: int = Field(ge=1, le=24 * 30)
    audio_days: int = Field(ge=1, le=3650)
    log_days: int = Field(ge=1, le=3650)


class ClaudeLoginCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=512)


class ClaudeLoginActionResponse(BaseModel):
    auth_session_id: str
    state: Literal["pending", "url_ready", "completed", "failed", "cancelled", "expired"]


class ClaudeLoginStateResponse(ClaudeLoginActionResponse):
    expires_at: datetime | None = None
    url: str | None = None
    failure_code: str | None = None


def _worker_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _owner_count(db: Session) -> int:
    return db.execute(select(func.count()).select_from(User).where(User.role == "owner", User.disabled_at.is_(None))).scalar() or 0


def _lock_account_administration(db: Session) -> None:
    """最後の owner 判定を配置全体で直列化する。

    対象user行だけのlockでは、owner A/Bを別transactionから同時に降格すると両方が
    count=2を観測できる。PostgreSQLではtransaction advisory lockを一つ取り、その他の
    DB（単体試験）ではactive owner行を固定順でlockする。
    """
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(742091733)"))
    else:
        list(
            db.execute(
                select(User.id)
                .where(User.role == "owner", User.disabled_at.is_(None))
                .order_by(User.id)
                .with_for_update()
            ).scalars()
        )


# --- 招待 ---------------------------------------------------------------------


@router.get("/invitations", summary="招待一覧")
def list_invitations(principal: Principal = Depends(require_owner), db: Session = Depends(db_dependency)) -> dict:
    rows = db.execute(select(Invitation).order_by(Invitation.created_at.desc())).scalars()
    return {
        "items": [
            {
                "id": str(row.id), "email": row.email, "role": row.role, "expires_at": row.expires_at.isoformat(),
                "used_at": row.used_at.isoformat() if row.used_at else None,
                "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            }
            for row in rows
        ]
    }


@router.post("/invitations", status_code=201, summary="招待発行 (URL は一度だけ返す)")
def create_invitation(body: InvitationRequest, principal: Principal = Depends(require_owner_reauth), db: Session = Depends(db_dependency)) -> dict:
    email = accounts.normalize_email(body.email)
    if db.execute(select(User.id).where(User.email == email)).first() is not None:
        raise ApiException(409, "conflict", "この招待は発行できません")
    token = new_token()
    row = Invitation(token_hash=token_hash(token), email=email, role=body.role, created_by=principal.id, expires_at=after(body.expires_hours * 3600))
    db.add(row)
    db.flush()
    audit.record(db, "invitation_issued", actor_id=principal.id, target_type="invitation", target_id=row.id, metadata={"role": body.role})
    return {"id": str(row.id), "email": email, "role": body.role, "expires_at": row.expires_at.isoformat(), "url": f"{get_settings().public_base_url}/auth/invite/{token}"}


@router.delete("/invitations/{invitation_id}", status_code=204, summary="招待取消")
def revoke_invitation(invitation_id: uuid.UUID, principal: Principal = Depends(require_owner_reauth), db: Session = Depends(db_dependency)) -> None:
    row = db.get(Invitation, invitation_id)
    if row is None:
        raise not_found("招待が見つかりません")
    if row.used_at is not None:
        raise ApiException(409, "conflict", "使用済みの招待は取り消せません")
    row.revoked_at = now()
    audit.record(db, "invitation_revoked", actor_id=principal.id, target_type="invitation", target_id=row.id)


# --- ユーザー -----------------------------------------------------------------


@router.get("/users", summary="ユーザー一覧")
def list_users(principal: Principal = Depends(require_owner), db: Session = Depends(db_dependency)) -> dict:
    rows = db.execute(select(User).order_by(User.created_at)).scalars()
    return {
        "items": [
            {"id": str(row.id), "email": row.email, "role": row.role, "disabled": row.disabled_at is not None, "created_at": row.created_at.isoformat()}
            for row in rows
        ]
    }


@router.patch("/users/{user_id}", summary="権限変更・無効化 (最後の owner は保護)")
def patch_user(user_id: uuid.UUID, body: UserPatch, principal: Principal = Depends(require_owner_reauth), db: Session = Depends(db_dependency)) -> dict:
    _lock_account_administration(db)
    user = db.execute(select(User).where(User.id == user_id).with_for_update()).scalars().first()
    if user is None:
        raise not_found("ユーザーが見つかりません")
    if body.role is not None and body.role != user.role:
        if user.role == "owner" and body.role == "member" and _owner_count(db) <= 1:
            raise ApiException(409, "conflict", "最後の owner は降格できません")
        user.role = body.role
        accounts.revoke_all_for_user(db, user.id)
        audit.record(db, "user_role_changed", actor_id=principal.id, target_type="user", target_id=user.id, metadata={"role": body.role})
    if body.disabled is not None:
        if body.disabled and user.disabled_at is None:
            if user.role == "owner" and _owner_count(db) <= 1:
                raise ApiException(409, "conflict", "最後の owner は無効化できません")
            user.disabled_at = now()
            accounts.revoke_all_for_user(db, user.id)
            audit.record(db, "user_disabled", actor_id=principal.id, target_type="user", target_id=user.id)
        elif not body.disabled and user.disabled_at is not None:
            user.disabled_at = None
            audit.record(db, "user_enabled", actor_id=principal.id, target_type="user", target_id=user.id)
    db.flush()
    return {"id": str(user.id), "email": user.email, "role": user.role, "disabled": user.disabled_at is not None}


# --- 保持期間・監査 ------------------------------------------------------------


def current_retention(db: Session) -> dict:
    policy = retention.current(db)
    return {"upload_hours": policy.upload_hours, "audio_days": policy.audio_days, "log_days": policy.log_days}


@router.get("/retention", summary="保持期間")
def get_retention(principal: Principal = Depends(require_owner), db: Session = Depends(db_dependency)) -> dict:
    return current_retention(db)


@router.put("/retention", summary="保持期間の変更")
def put_retention(body: RetentionSettings, principal: Principal = Depends(require_owner_reauth), db: Session = Depends(db_dependency)) -> dict:
    row = db.get(Setting, "retention")
    if row is None:
        row = Setting(key="retention", value=body.model_dump())
        db.add(row)
    else:
        row.value = body.model_dump()
    retention.apply_to_existing(db, retention.RetentionPolicy(**body.model_dump()))
    audit.record(db, "retention_changed", actor_id=principal.id, metadata=body.model_dump())
    return body.model_dump()


@router.get("/audit", summary="監査ログ")
def get_audit(principal: Principal = Depends(require_owner), db: Session = Depends(db_dependency), cursor: int | None = None, limit: int = Query(default=100, ge=1, le=500)) -> dict:
    statement = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit + 1)
    if cursor:
        statement = statement.where(AuditLog.id < cursor)
    rows = list(db.execute(statement).scalars())
    return {
        "items": [
            {"id": row.id, "actor_id": str(row.actor_id) if row.actor_id else None, "action": row.action, "target_type": row.target_type,
             "target_id": row.target_id, "metadata": row.metadata_, "created_at": row.created_at.isoformat()}
            for row in rows[:limit]
        ],
        # cursor は最後に返した行の id。検索は id < cursor (排他) なので、
        # 返していない rows[limit] を cursor にすると境界で 1 件飛ぶ。
        "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
    }


# --- Claude 接続 ---------------------------------------------------------------


def _sync_connection(db: Session, status: dict, principal: Principal | None) -> dict:
    connection = minutes_service.claude_connection(db)
    connection.state = status.get("state", "checking")
    connection.cli_version = status.get("cli_version")
    connection.checked_at = now()
    if connection.state in ("logged_out", "cli_missing"):
        if connection.connected_owner_id is not None:
            # 接続が切れた時点で、待機中の議事録ジョブを取り消す。worker は投入時の
            # snapshot しか見られないため、接続状態の変化は API 側で反映する (R05)
            jobs.cancel_minutes_jobs_not_for_owner(db, None)
        connection.connected_owner_id = None
    return {
        "state": connection.state,
        "cli_version": connection.cli_version,
        "cli_supported": status.get("cli_supported"),
        "conflict_env_vars": status.get("conflict_env_vars", []),
        "connected_owner_id": str(connection.connected_owner_id) if connection.connected_owner_id else None,
        "connected_owner_is_me": bool(principal and connection.connected_owner_id == principal.id),
        "checked_at": connection.checked_at.isoformat(),
    }


@router.get("/claude", summary="Claude 接続状態 (owner)")
def claude_status(principal: Principal = Depends(require_owner), db: Session = Depends(db_dependency)) -> dict:
    status = WorkerControlClient().status()
    return _sync_connection(db, status, principal)


@router.post(
    "/claude/login",
    status_code=201,
    response_model=ClaudeLoginStateResponse,
    response_model_exclude_none=True,
    summary="Claude ログイン開始 (owner、再認証必須)",
)
def claude_login(principal: Principal = Depends(require_owner_reauth), db: Session = Depends(db_dependency)) -> dict:
    active = db.execute(select(ClaudeAuthSession).where(ClaudeAuthSession.state.in_(("pending", "url_ready")))).scalars().first()
    if active is not None and (active.expires_at is None or active.expires_at > now()):
        raise ApiException(409, "conflict", "別のログイン処理が進行中です", details={"auth_session_id": active.id})
    result = WorkerControlClient().start_login()
    expires_at = _worker_datetime(result.get("expires_at"))
    row = ClaudeAuthSession(
        id=result["auth_session_id"], requested_by=principal.id, state="pending", expires_at=expires_at
    )
    db.merge(row)
    audit.record(db, "claude_login_started", actor_id=principal.id, target_type="claude_auth_session", target_id=row.id)
    return {"auth_session_id": row.id, "expires_at": expires_at, "state": "pending"}


@router.get(
    "/claude/login/{auth_session_id}",
    response_model=ClaudeLoginStateResponse,
    response_model_exclude_none=True,
    summary="ログイン状態 (URL は要求 owner にだけ返し保存しない)",
)
def claude_login_state(auth_session_id: str, principal: Principal = Depends(require_owner), db: Session = Depends(db_dependency)) -> dict:
    row = db.get(ClaudeAuthSession, auth_session_id)
    if row is None or row.requested_by != principal.id:
        raise not_found("ログイン処理が見つかりません")
    result = WorkerControlClient().login_state(auth_session_id)
    new_state = result.get("state", row.state)
    if new_state != row.state:
        row.state = new_state
        if new_state == "completed":
            connection = minutes_service.claude_connection(db)
            connection.connected_owner_id = principal.id
            connection.state = "logged_in"
            connection.checked_at = now()
            # 別 owner 向けに投入済みの待機中ジョブは、接続 owner が変わった時点で取り消す
            jobs.cancel_minutes_jobs_not_for_owner(db, principal.id)
            audit.record(db, "claude_login_completed", actor_id=principal.id, target_type="claude_auth_session", target_id=row.id)
        elif new_state in ("failed", "expired"):
            audit.record(db, f"claude_login_{new_state}", actor_id=principal.id, target_type="claude_auth_session", target_id=row.id)
    response = {"auth_session_id": row.id, "state": row.state, "expires_at": result.get("expires_at")}
    if row.state == "url_ready" and result.get("url"):
        response["url"] = result["url"]  # 中継のみ。DB・ログへ書かない
    failure_code = result.get("failure_code")
    if failure_code in CLAUDE_LOGIN_FAILURE_CODES:
        response["failure_code"] = failure_code  # 安全な分類だけ中継し、DBへは保存しない
    return response


@router.post(
    "/claude/login/{auth_session_id}/code",
    response_model=ClaudeLoginActionResponse,
    summary="Claude認可codeの中継 (owner、再認証必須)",
)
def claude_login_code(
    auth_session_id: str,
    body: ClaudeLoginCodeRequest,
    principal: Principal = Depends(require_owner_reauth),
    db: Session = Depends(db_dependency),
) -> dict:
    # 要求ownerと状態をlock下で確認する。Bearer+直近再認証がbrowser requestに対する
    # CSRF相当の結び付きであり、認可codeそのものはDB・audit metadata・logへ残さない。
    row = db.execute(
        select(ClaudeAuthSession).where(ClaudeAuthSession.id == auth_session_id).with_for_update()
    ).scalars().first()
    if row is None or row.requested_by != principal.id:
        raise not_found("ログイン処理が見つかりません")
    if row.expires_at is not None and row.expires_at <= now():
        row.state = "expired"
        raise ApiException(409, "conflict", "ログイン処理の有効期限が切れています")
    if row.state not in ("pending", "url_ready"):
        raise ApiException(409, "conflict", "ログイン処理は認可codeを受け付けられません")
    code = body.code.strip()
    if not code:
        raise ApiException(400, "invalid_request", "認可codeが空です")
    result = WorkerControlClient().submit_login_code(auth_session_id, code)
    row.state = result.get("state", row.state)
    audit.record(
        db,
        "claude_login_code_submitted",
        actor_id=principal.id,
        target_type="claude_auth_session",
        target_id=row.id,
    )
    return {"auth_session_id": row.id, "state": row.state}


@router.post(
    "/claude/login/{auth_session_id}/cancel",
    response_model=ClaudeLoginActionResponse,
    summary="ログイン取消",
)
def claude_login_cancel(auth_session_id: str, principal: Principal = Depends(require_owner), db: Session = Depends(db_dependency)) -> dict:
    row = db.get(ClaudeAuthSession, auth_session_id)
    if row is None or row.requested_by != principal.id:
        raise not_found("ログイン処理が見つかりません")
    result = WorkerControlClient().cancel_login(auth_session_id)
    row.state = result.get("state", "cancelled")
    audit.record(db, "claude_login_cancelled", actor_id=principal.id, target_type="claude_auth_session", target_id=row.id)
    return {"auth_session_id": row.id, "state": row.state}


@router.post("/claude/logout", summary="Claude ログアウト (owner、再認証必須)")
def claude_logout(principal: Principal = Depends(require_owner_reauth), db: Session = Depends(db_dependency)) -> dict:
    result = WorkerControlClient().logout()
    connection = minutes_service.claude_connection(db)
    jobs.cancel_minutes_jobs_not_for_owner(db, None)
    connection.connected_owner_id = None
    connection.state = result.get("state", "logged_out")
    connection.checked_at = now()
    audit.record(db, "claude_logout", actor_id=principal.id)
    return {"state": connection.state}
