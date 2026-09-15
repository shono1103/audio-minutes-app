"""議事録フォーマットプロファイル: 検証・組み込み標準の seed・スナップショット・プレビュー。"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from audio_minutes_contracts.models import FormatProfile as FormatProfileModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from minutes_api.errors import ApiException
from minutes_api.models import FormatProfile, User

BUILTIN_STANDARD_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
_VARIABLE_RE = re.compile(r"\{\{\s*([a-zA-Z_:]+)\s*\}\}")
_ALLOWED_VARIABLES = {"title", "started_at", "duration"}
_SECTION_KEYS = {"summary", "decisions", "action_items", "open_questions", "topics", "transcript_references", "custom"}


def _fixture_path() -> Path:
    for candidate in (
        Path(__file__).resolve().parents[4] / "contracts" / "fixtures" / "format-profile.standard.json",
        Path("/app/contracts/fixtures/format-profile.standard.json"),
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("format-profile.standard.json")


def seed_builtin(db: Session) -> None:
    if db.get(FormatProfile, BUILTIN_STANDARD_ID) is not None:
        return
    document = json.loads(_fixture_path().read_text(encoding="utf-8"))
    db.add(
        FormatProfile(
            id=BUILTIN_STANDARD_ID,
            owner_id=None,
            version=document["version"],
            name=document["name"],
            output_language=document["output_language"],
            builtin=True,
            sections=document["sections"],
            additional_instructions=document["additional_instructions"],
            template_markdown=document["template_markdown"],
        )
    )


def validate_template(template: str, sections: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    keys = {section["key"] for section in sections}
    seen = set()
    for section in sections:
        if section["key"] not in _SECTION_KEYS:
            problems.append(f"未対応のセクション: {section['key']}")
        if section["key"] != "custom":
            if section["key"] in seen:
                problems.append(f"セクションが重複しています: {section['key']}")
            seen.add(section["key"])
        if not section.get("title"):
            problems.append("セクションのタイトルが空です")
    for match in _VARIABLE_RE.finditer(template):
        name = match.group(1)
        if name in _ALLOWED_VARIABLES:
            continue
        if name.startswith("section:"):
            key = name.split(":", 1)[1]
            if key not in keys:
                problems.append(f"テンプレートが未定義のセクションを参照しています: {key}")
            continue
        problems.append(f"未対応の変数: {name}")
    if "{{title}}" not in template.replace(" ", ""):
        problems.append("テンプレートには {{title}} が必要です")
    return problems


def to_contract(row: FormatProfile, is_default: bool) -> dict[str, Any]:
    document = FormatProfileModel(
        profile_id=row.id,
        owner_id=row.owner_id,
        version=row.version,
        name=row.name,
        output_language=row.output_language,
        builtin=row.builtin,
        is_default=is_default,
        sections=row.sections,
        additional_instructions=row.additional_instructions,
        template_markdown=row.template_markdown,
        updated_at=row.updated_at,
    )
    return json.loads(document.model_dump_json())


def default_profile_id(user: User) -> uuid.UUID:
    return user.default_format_profile_id or BUILTIN_STANDARD_ID


def load_accessible(db: Session, user: User, profile_id: uuid.UUID) -> FormatProfile:
    # fresh DB でも最初のセッション作成・一覧取得から標準形式を使えるよう、
    # DB access と同じ transaction 内で欠けている場合だけ作る。
    seed_builtin(db)
    row = db.get(FormatProfile, profile_id)
    if row is None or row.deleted_at is not None or (not row.builtin and row.owner_id != user.id):
        raise ApiException(404, "not_found", "フォーマットプロファイルが見つかりません")
    return row


def snapshot_for(db: Session, user: User, profile_id: uuid.UUID | None) -> dict[str, Any]:
    row = load_accessible(db, user, profile_id or default_profile_id(user))
    return to_contract(row, is_default=(row.id == default_profile_id(user)))


def render_preview(document: dict[str, Any]) -> str:
    """サンプルデータで Markdown を組み立てる (副作用なし)。"""
    sample = {
        "title": "サンプル会議",
        "started_at": "2026-09-12 10:00",
        "duration": "00:30:00",
    }
    section_bodies = {
        "summary": "- 例: 次期リリースの範囲を確認した",
        "decisions": "- 例: 9 月末までにベータ版を配布する",
        "action_items": "- 例: 田中: 要件の見直し (期限: 9/20)",
        "open_questions": "- 例: 予算の上限は未決",
        "topics": "### ロードマップ\n- 例: 各機能の優先度を議論した",
        "transcript_references": "- 例: seg-0001 〜 seg-0003",
        "custom": "- 例: カスタムセクション",
    }

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in sample:
            return sample[name]
        key = name.split(":", 1)[1]
        section = next((item for item in document["sections"] if item["key"] == key), None)
        if section is None or not section.get("enabled", True):
            return ""
        return f"## {section['title']}\n\n{section_bodies.get(key, '')}"

    return _VARIABLE_RE.sub(replace, document["template_markdown"])


def list_for_user(db: Session, user: User) -> list[FormatProfile]:
    seed_builtin(db)
    db.flush()
    rows = db.execute(
        select(FormatProfile)
        .where(FormatProfile.deleted_at.is_(None))
        .where((FormatProfile.builtin.is_(True)) | (FormatProfile.owner_id == user.id))
        .order_by(FormatProfile.builtin.desc(), FormatProfile.created_at)
    ).scalars()
    return list(rows)
