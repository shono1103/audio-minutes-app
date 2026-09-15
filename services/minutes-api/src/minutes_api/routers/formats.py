"""議事録フォーマットプロファイル (本人のみ)。組み込み標準は複製のみ。"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from minutes_api import audit, formats_service
from minutes_api.db import db_dependency
from minutes_api.deps import Principal, current_principal
from minutes_api.errors import ApiException, not_found
from minutes_api.models import FormatProfile, User

router = APIRouter(prefix="/v1/formats", tags=["formats"])


class SectionInput(BaseModel):
    key: Literal["summary", "decisions", "action_items", "open_questions", "topics", "transcript_references", "custom"]
    title: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    instructions: str | None = Field(default=None, max_length=2000)


class FormatInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    output_language: Literal["ja", "en"] = "ja"
    sections: list[SectionInput] = Field(min_length=1)
    additional_instructions: str = Field(default="", max_length=4000)
    template_markdown: str = Field(min_length=1, max_length=20000)


def _validate(body: FormatInput) -> list[dict]:
    sections = [section.model_dump() for section in body.sections]
    problems = formats_service.validate_template(body.template_markdown, sections)
    if problems:
        raise ApiException(400, "invalid_input", "フォーマットの検証に失敗しました", details={"problems": problems})
    return sections


def _serialize(row: FormatProfile, user: User) -> dict:
    return formats_service.to_contract(row, is_default=(row.id == formats_service.default_profile_id(user)))


def _load_own(db: Session, user: User, profile_id: uuid.UUID) -> FormatProfile:
    row = db.get(FormatProfile, profile_id)
    if row is None or row.deleted_at is not None or row.owner_id != user.id:
        raise not_found("フォーマットプロファイルが見つかりません")
    return row


@router.get("", summary="一覧 (組み込み + 本人)")
def list_formats(principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> dict:
    return {"items": [_serialize(row, principal.user) for row in formats_service.list_for_user(db, principal.user)]}


@router.post("", status_code=201, summary="作成")
def create_format(body: FormatInput, principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> dict:
    sections = _validate(body)
    row = FormatProfile(
        owner_id=principal.id, version=1, name=body.name, output_language=body.output_language, sections=sections,
        additional_instructions=body.additional_instructions, template_markdown=body.template_markdown,
    )
    db.add(row)
    db.flush()
    audit.record(db, "format_created", actor_id=principal.id, target_type="format", target_id=row.id)
    return _serialize(row, principal.user)


@router.post("/preview", summary="副作用のないプレビュー")
def preview(body: FormatInput, principal: Principal = Depends(current_principal)) -> dict:
    sections = _validate(body)
    document = {"sections": sections, "template_markdown": body.template_markdown}
    return {"markdown": formats_service.render_preview(document)}


@router.get("/{profile_id}", summary="取得")
def get_format(profile_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> dict:
    row = formats_service.load_accessible(db, principal.user, profile_id)
    return _serialize(row, principal.user)


@router.put("/{profile_id}", summary="更新 (版を進める)")
def update_format(profile_id: uuid.UUID, body: FormatInput, principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> dict:
    row = db.get(FormatProfile, profile_id)
    if row is None or row.deleted_at is not None:
        raise not_found("フォーマットプロファイルが見つかりません")
    if row.builtin:
        raise ApiException(409, "conflict", "組み込みプロファイルは編集できません。複製してください")
    if row.owner_id != principal.id:
        raise not_found("フォーマットプロファイルが見つかりません")
    sections = _validate(body)
    row.name = body.name
    row.output_language = body.output_language
    row.sections = sections
    row.additional_instructions = body.additional_instructions
    row.template_markdown = body.template_markdown
    row.version += 1
    db.flush()
    audit.record(db, "format_updated", actor_id=principal.id, target_type="format", target_id=row.id)
    return _serialize(row, principal.user)


@router.delete("/{profile_id}", status_code=204, summary="削除 (組み込み不可、過去セッションのスナップショットは変わらない)")
def delete_format(profile_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> None:
    row = db.get(FormatProfile, profile_id)
    if row is None or row.deleted_at is not None:
        raise not_found("フォーマットプロファイルが見つかりません")
    if row.builtin:
        raise ApiException(409, "conflict", "組み込みプロファイルは削除できません")
    if row.owner_id != principal.id:
        raise not_found("フォーマットプロファイルが見つかりません")
    from minutes_api.security import now

    row.deleted_at = now()
    if principal.user.default_format_profile_id == row.id:
        principal.user.default_format_profile_id = None
    audit.record(db, "format_deleted", actor_id=principal.id, target_type="format", target_id=row.id)


@router.post("/{profile_id}/duplicate", status_code=201, summary="複製")
def duplicate_format(profile_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> dict:
    source = formats_service.load_accessible(db, principal.user, profile_id)
    row = FormatProfile(
        owner_id=principal.id, version=1, name=f"{source.name} のコピー"[:100], output_language=source.output_language,
        sections=source.sections, additional_instructions=source.additional_instructions, template_markdown=source.template_markdown,
    )
    db.add(row)
    db.flush()
    audit.record(db, "format_duplicated", actor_id=principal.id, target_type="format", target_id=row.id)
    return _serialize(row, principal.user)


@router.post("/{profile_id}/default", summary="既定に設定")
def set_default(profile_id: uuid.UUID, principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> dict:
    row = formats_service.load_accessible(db, principal.user, profile_id)
    principal.user.default_format_profile_id = None if row.builtin else row.id
    db.flush()
    audit.record(db, "format_default_set", actor_id=principal.id, target_type="format", target_id=row.id)
    return _serialize(row, principal.user)
