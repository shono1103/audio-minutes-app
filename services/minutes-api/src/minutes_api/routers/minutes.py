"""議事録版: 履歴・版取得・手動編集版・再生成・現在版選択・復元・比較 (所有者のみ)。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from minutes_api import formats_service, minutes_service, sessions_service
from minutes_api.db import db_dependency
from minutes_api.deps import SessionAccess, session_owner_access
from minutes_api.errors import ApiException, not_found
from minutes_api.models import MinutesVersion

router = APIRouter(prefix="/v1/sessions/{session_id}/minutes", tags=["minutes"])


class ManualEditRequest(BaseModel):
    parent_version_id: uuid.UUID
    body_markdown: str = Field(min_length=1)
    expected_current_version_id: uuid.UUID | None = None


class RegenerateRequest(BaseModel):
    base_version_id: uuid.UUID | None = None
    instructions: str | None = Field(default=None, max_length=8000)
    format_profile_id: uuid.UUID | None = None
    use_snapshot: bool = True


class SelectCurrentRequest(BaseModel):
    version_id: uuid.UUID


@router.get("/versions", summary="版履歴 (所有者)")
def list_versions(access: SessionAccess = Depends(session_owner_access), db: Session = Depends(db_dependency)) -> dict:
    rows = db.execute(
        select(MinutesVersion).where(MinutesVersion.session_id == access.session.id).order_by(MinutesVersion.version_number)
    ).scalars()
    return {
        "items": [minutes_service.to_contract(row) for row in rows],
        "current_minutes_version_id": str(access.session.current_minutes_version_id) if access.session.current_minutes_version_id else None,
    }


@router.get("/versions/{version_id}", summary="版取得 (所有者)")
def get_version(version_id: uuid.UUID, access: SessionAccess = Depends(session_owner_access), db: Session = Depends(db_dependency)) -> dict:
    row = db.get(MinutesVersion, version_id)
    if row is None or row.session_id != access.session.id:
        raise not_found("版が見つかりません")
    return {"version": minutes_service.to_contract(row), "body_markdown": minutes_service.read_body(row)}


@router.post("/versions", status_code=201, summary="手動編集版の保存 (所有者、競合検査)")
def save_manual_edit(body: ManualEditRequest, access: SessionAccess = Depends(session_owner_access), db: Session = Depends(db_dependency)) -> dict:
    version = minutes_service.save_manual_edit(
        db, access.session, access.principal.user, parent_version_id=body.parent_version_id, body_markdown=body.body_markdown,
        expected_current_version_id=body.expected_current_version_id,
    )
    db.flush()
    return {"version": minutes_service.to_contract(version)}


@router.post("/regenerate", status_code=202, summary="Claude 再生成 (所有者、非同期)")
def regenerate(
    body: RegenerateRequest,
    access: SessionAccess = Depends(session_owner_access),
    db: Session = Depends(db_dependency),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200),
) -> dict:
    """`Idempotency-Key` を付けた再送は同じ job になる。無い場合は要求ごとに新しい job を作る。"""
    session = access.session
    base_id = body.base_version_id or session.current_minutes_version_id
    snapshot = None
    if body.format_profile_id is not None:
        snapshot = formats_service.snapshot_for(db, access.principal.user, body.format_profile_id)
    elif not body.use_snapshot:
        snapshot = formats_service.snapshot_for(db, access.principal.user, None)
    kind = "claude_regenerated" if base_id is not None else "claude_generated"
    job_id = minutes_service.enqueue_minutes(
        db, session, access.principal.user, kind=kind, base_version_id=base_id, instructions=body.instructions,
        format_snapshot=snapshot, request_key=idempotency_key,
    )
    db.flush()
    return {"job_id": str(job_id), "session": sessions_service.serialize(db, session, access.principal.id)}


@router.post("/current", summary="現在版の選択 (所有者)")
def select_current(body: SelectCurrentRequest, access: SessionAccess = Depends(session_owner_access), db: Session = Depends(db_dependency)) -> dict:
    version = minutes_service.select_current(db, access.session, access.principal.user, body.version_id)
    db.flush()
    return {"version": minutes_service.to_contract(version)}


@router.post("/versions/{version_id}/restore", status_code=201, summary="過去版の復元 (新しい版として、所有者)")
def restore(version_id: uuid.UUID, access: SessionAccess = Depends(session_owner_access), db: Session = Depends(db_dependency)) -> dict:
    version = minutes_service.restore(db, access.session, access.principal.user, version_id)
    db.flush()
    return {"version": minutes_service.to_contract(version)}


@router.get("/compare", summary="版の比較 (unified diff、所有者)")
def compare(
    from_: uuid.UUID = Query(alias="from"), to: uuid.UUID = Query(), access: SessionAccess = Depends(session_owner_access), db: Session = Depends(db_dependency)
) -> dict:
    if from_ == to:
        raise ApiException(400, "invalid_request", "異なる版を指定してください")
    return {"from": str(from_), "to": str(to), "diff": minutes_service.compare(db, access.session, from_, to)}
