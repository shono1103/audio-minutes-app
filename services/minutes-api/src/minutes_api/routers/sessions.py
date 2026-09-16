"""セッション: 作成 (tus upload 発行)、一覧、取得、更新、削除、finalize、retry、jobs、共有。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from audio_minutes_contracts.models import RecordingPackage
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from minutes_api import audit, jobs, live_transcription, sessions_service
from minutes_api.db import db_dependency
from minutes_api.deps import Principal, SessionAccess, current_principal, session_access, session_owner_access
from minutes_api.errors import ApiException, not_found
from minutes_api.models import MeetingSession, SessionShare, User

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    language_mode: Literal["auto", "ja", "en", "mixed"] | None = None
    allow_external_send: bool | None = None


class RetryRequest(BaseModel):
    stage: Literal["transcription", "minutes"]
    language_mode: Literal["auto", "ja", "en", "mixed"] | None = None


class ShareRequest(BaseModel):
    user_id: uuid.UUID | None = None
    email: str | None = None


class LiveSessionStart(BaseModel):
    session_id: uuid.UUID
    started_at: datetime
    title: str = Field(min_length=1, max_length=200)
    title_edited_by_user: bool = False
    language_mode: Literal["auto", "ja", "en", "mixed"] = "auto"
    allow_external_send: bool = True
    format_profile_id: uuid.UUID | None = None
    source: dict[str, Any]


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": str(job["job_id"]),
        "kind": job["kind"],
        "status": job["status"],
        "attempt": job["attempt"],
        "max_attempts": job["max_attempts"],
        "cancel_requested": job["cancel_requested"],
        "failure": job["failure"],
        "created_at": job["created_at"].isoformat() if job["created_at"] else None,
        "started_at": job["started_at"].isoformat() if job["started_at"] else None,
        "finished_at": job["finished_at"].isoformat() if job["finished_at"] else None,
    }


@router.post("", status_code=201, summary="セッション作成 (recording-package)")
def create_session(package: RecordingPackage, response: Response, principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> dict:
    existing = db.get(MeetingSession, package.session_id)
    session = sessions_service.create_session(db, principal.user, package)
    if existing is not None:
        response.status_code = 200
    db.flush()
    return sessions_service.serialize(db, session, principal.id)


@router.post("/live", status_code=201, summary="録音中の先行文字起こしsessionを開始")
def begin_live_session(
    body: LiveSessionStart,
    response: Response,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(db_dependency),
) -> dict:
    existing = db.get(MeetingSession, body.session_id)
    session = live_transcription.begin_session(db, principal.user, body.model_dump())
    if existing is not None:
        response.status_code = 200
    return sessions_service.serialize(db, session, principal.id)


@router.put("/{session_id}/live-chunks/{track_id}/{sequence}", summary="録音中の不変WAV chunkを冪等登録")
async def put_live_chunk(
    track_id: str,
    sequence: int,
    request: Request,
    start_offset_ms: int = Query(ge=0),
    duration_ms: int = Query(ge=1, le=120_000),
    sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    access: SessionAccess = Depends(session_owner_access),
    db: Session = Depends(db_dependency),
) -> dict:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > live_transcription.MAX_CHUNK_BYTES:
        raise ApiException(400, "limit_exceeded", "chunkの大きさが上限を超えています", stage="upload")
    body = bytearray()
    async for part in request.stream():
        if len(body) + len(part) > live_transcription.MAX_CHUNK_BYTES:
            raise ApiException(400, "limit_exceeded", "chunkの大きさが上限を超えています", stage="upload")
        body.extend(part)
    row = live_transcription.put_chunk(
        db,
        access.session,
        access.principal.user,
        track_id=track_id,
        sequence=sequence,
        start_offset_ms=start_offset_ms,
        duration_ms=duration_ms,
        sha256=sha256,
        body=bytes(body),
    )
    return live_transcription.chunk_status(row)


@router.get("/{session_id}/live-chunks", summary="先行文字起こしchunkの進捗")
def live_chunk_status(
    access: SessionAccess = Depends(session_owner_access), db: Session = Depends(db_dependency)
) -> dict:
    rows = live_transcription.rows_for_session(db, access.session.id)
    return {
        "items": [live_transcription.chunk_status(row) for row in rows],
        "uploaded": len(rows),
        "transcribed": sum(row.state == "transcribed" for row in rows),
        "failed": sum(row.state in ("failed", "cancelled") for row in rows),
    }


@router.get("", summary="セッション一覧 (所有 + 共有先)")
def list_sessions(
    principal: Principal = Depends(current_principal),
    db: Session = Depends(db_dependency),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    shared_ids = select(SessionShare.session_id).where(SessionShare.user_id == principal.id)
    statement = (
        select(MeetingSession)
        .where(MeetingSession.deleted_at.is_(None))
        .where(or_(MeetingSession.owner_id == principal.id, MeetingSession.id.in_(shared_ids)))
        .order_by(MeetingSession.created_at.desc(), MeetingSession.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        try:
            cursor_id = uuid.UUID(cursor)
        except ValueError as exc:
            raise ApiException(400, "invalid_request", "cursor が不正です") from exc
        anchor = db.get(MeetingSession, cursor_id)
        if anchor is not None:
            statement = statement.where(
                or_(
                    MeetingSession.created_at < anchor.created_at,
                    (MeetingSession.created_at == anchor.created_at) & (MeetingSession.id < anchor.id),
                )
            )
    rows = list(db.execute(statement).scalars())
    # cursor は「最後に返した行」。次ページはこの行より後ろ (排他) を取る。
    # 返していない rows[limit] を cursor にすると、その 1 件が両ページから漏れる。
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    items = [sessions_service.serialize(db, row, principal.id) for row in rows[:limit]]
    return {"items": items, "next_cursor": next_cursor}


@router.get("/{session_id}", summary="セッション取得")
def get_session(access: SessionAccess = Depends(session_access), db: Session = Depends(db_dependency)) -> dict:
    return sessions_service.serialize(db, access.session, access.principal.id)


@router.patch("/{session_id}", summary="タイトル・言語モード・外部送信可否の更新 (所有者)")
def patch_session(body: SessionPatch, access: SessionAccess = Depends(session_owner_access), db: Session = Depends(db_dependency)) -> dict:
    session = access.session
    if body.title is not None:
        session.title = body.title.strip()
        session.title_edited_by_user = True
        session.title_revision += 1
        audit.record(db, "session_title_updated", actor_id=access.principal.id, target_type="session", target_id=session.id)
    if body.language_mode is not None:
        if session.status in sessions_service.PROCESSING_STATUSES:
            raise ApiException(409, "conflict", "処理中は言語モードを変更できません")
        session.language_mode = body.language_mode
    if body.allow_external_send is not None:
        if session.status in sessions_service.PROCESSING_STATUSES:
            raise ApiException(409, "conflict", "処理中は外部送信可否を変更できません")
        session.allow_external_send = body.allow_external_send
        audit.record(
            db, "session_external_send_changed", actor_id=access.principal.id, target_type="session", target_id=session.id,
            metadata={"allow_external_send": body.allow_external_send},
        )
    db.flush()
    return sessions_service.serialize(db, session, access.principal.id)


@router.delete("/{session_id}", status_code=204, summary="セッション削除 (所有者)")
def delete_session(
    session_id: uuid.UUID,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(db_dependency),
) -> None:
    # 削除済み行も同じ所有者だけは再取得し、I/O cleanup の冪等な再試行を許可する。
    session = db.execute(select(MeetingSession).where(MeetingSession.id == session_id).with_for_update()).scalars().first()
    if session is None or session.owner_id != principal.id:
        raise not_found("セッションが見つかりません")
    sessions_service.delete_session(db, session, principal.user)


@router.post("/{session_id}/finalize", status_code=202, summary="全トラック検証後に文字起こしを一度だけ投入")
def finalize(response: Response, access: SessionAccess = Depends(session_owner_access), db: Session = Depends(db_dependency)) -> dict:
    session = access.session
    already = session.finalized_at is not None
    sessions_service.finalize(db, session, access.principal.user)
    if already:
        response.status_code = 200
    db.flush()
    return sessions_service.serialize(db, session, access.principal.id)


@router.post("/{session_id}/retry", status_code=202, summary="文字起こしまたは議事録生成の再実行 (所有者)")
def retry(
    body: RetryRequest,
    access: SessionAccess = Depends(session_owner_access),
    db: Session = Depends(db_dependency),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200),
) -> dict:
    """`Idempotency-Key` を付けた再送は同じ job になる。無い場合は要求ごとに新しい job を作る。"""
    job_id = sessions_service.retry(
        db, access.session, access.principal.user, stage=body.stage, language_mode=body.language_mode,
        request_key=idempotency_key,
    )
    db.flush()
    return {"job_id": str(job_id), "session": sessions_service.serialize(db, access.session, access.principal.id)}


@router.get("/{session_id}/jobs", summary="工程別ジョブ状態")
def list_jobs(access: SessionAccess = Depends(session_access), db: Session = Depends(db_dependency)) -> dict:
    return {"items": [_job_view(job) for job in jobs.list_for_session(db, access.session.id)]}


@router.post("/{session_id}/jobs/{job_id}/cancel", status_code=202, summary="ジョブ取消要求 (所有者)")
def cancel_job(
    job_id: uuid.UUID,
    access: SessionAccess = Depends(session_owner_access),
    db: Session = Depends(db_dependency),
) -> dict:
    """受付済み要求を返す。terminal job への再送も同じ状態を返して冪等に扱う。"""
    job = jobs.request_cancel(db, access.session.id, job_id)
    if job is None:
        raise not_found("ジョブが見つかりません")
    audit.record(
        db,
        "job_cancel_requested",
        actor_id=access.principal.id,
        target_type="job",
        target_id=job_id,
        metadata={"session_id": str(access.session.id), "status": job["status"]},
    )
    return _job_view(job)


@router.get("/{session_id}/shares", summary="共有先一覧 (所有者)")
def list_shares(access: SessionAccess = Depends(session_owner_access), db: Session = Depends(db_dependency)) -> dict:
    rows = db.execute(
        select(SessionShare, User).join(User, User.id == SessionShare.user_id).where(SessionShare.session_id == access.session.id)
    ).all()
    return {"items": [{"user_id": str(user.id), "email": user.email, "created_at": share.created_at.isoformat()} for share, user in rows]}


@router.post("/{session_id}/shares", status_code=201, summary="登録済みユーザーへ閲覧共有 (所有者)")
def add_share(body: ShareRequest, access: SessionAccess = Depends(session_owner_access), db: Session = Depends(db_dependency)) -> dict:
    if body.user_id is None and not body.email:
        raise ApiException(400, "invalid_request", "user_id または email が必要です")
    statement = select(User).where(User.disabled_at.is_(None))
    statement = statement.where(User.id == body.user_id) if body.user_id else statement.where(User.email == body.email.strip().lower())
    target = db.execute(statement).scalars().first()
    if target is None:
        raise not_found("共有先ユーザーが見つかりません")
    if target.id == access.session.owner_id:
        raise ApiException(409, "conflict", "所有者自身には共有できません")
    existing = db.execute(
        select(SessionShare).where(SessionShare.session_id == access.session.id, SessionShare.user_id == target.id)
    ).scalars().first()
    if existing is None:
        db.add(SessionShare(session_id=access.session.id, user_id=target.id))
        audit.record(db, "session_shared", actor_id=access.principal.id, target_type="session", target_id=access.session.id, metadata={"user_id": str(target.id)})
    db.flush()
    return {"user_id": str(target.id), "email": target.email}


@router.delete("/{session_id}/shares/{user_id}", status_code=204, summary="共有解除 (所有者)")
def remove_share(user_id: uuid.UUID, access: SessionAccess = Depends(session_owner_access), db: Session = Depends(db_dependency)) -> None:
    row = db.execute(
        select(SessionShare).where(SessionShare.session_id == access.session.id, SessionShare.user_id == user_id)
    ).scalars().first()
    if row is None:
        raise not_found("共有が見つかりません")
    db.delete(row)
    audit.record(db, "session_unshared", actor_id=access.principal.id, target_type="session", target_id=access.session.id, metadata={"user_id": str(user_id)})
