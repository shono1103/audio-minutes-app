"""議事録版のドメイン操作: 生成ジョブ投入、手動編集版、現在版選択、復元、比較。"""

from __future__ import annotations

import difflib
import json
import uuid
from typing import Any

from audio_minutes_contracts.models import InputKind, JobKind, MinutesJobSettings
from audio_minutes_contracts.models import MinutesVersion as MinutesVersionContract
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minutes_api import audit, jobs
from minutes_api.config import get_settings
from minutes_api.errors import ApiException
from minutes_api.models import Artifact, ClaudeConnection, MeetingSession, MinutesVersion, Transcript, User
from minutes_api.sessions_service import PROCESSING_STATUSES, artifact_store, latest_transcript


def claude_connection(db: Session) -> ClaudeConnection:
    row = db.get(ClaudeConnection, 1)
    if row is None:
        row = ClaudeConnection(id=1, state="checking")
        db.add(row)
        db.flush()
    return row


def check_can_send(db: Session, session: MeetingSession) -> ClaudeConnection:
    """送信可否を検査し、投入時点の接続状態 (snapshot 元) を返す。"""
    if not session.allow_external_send:
        raise ApiException(409, "external_send_forbidden", "このセッションは外部送信禁止です", stage="minutes", retryable=False)
    connection = claude_connection(db)
    if connection.connected_owner_id != session.owner_id:
        raise ApiException(
            409, "not_connected_owner", "Claude に接続した owner 本人のセッションだけ生成できます", stage="minutes", retryable=False
        )
    if connection.state != "logged_in":
        raise ApiException(409, "claude_not_authenticated", "Claude にログインしていません", stage="minutes", retryable=True)
    return connection


def lock_session(db: Session, session: MeetingSession) -> None:
    """版採番と現在版更新をセッション単位で直列化する (R16)。

    `expected_current_version_id` の比較・版番号の採番・current_minutes_version_id の
    更新を同じロックの内側で行う。ロック前に読んだ ORM の値と比較すると、並行保存で
    両方が検査を通過して片方の現在版を上書きしたり、version_number の unique 制約で
    500 になったりする。
    """
    db.refresh(session, with_for_update=True)


def next_version_number(db: Session, session_id: uuid.UUID) -> int:
    current = db.execute(select(func.max(MinutesVersion.version_number)).where(MinutesVersion.session_id == session_id)).scalar()
    return (current or 0) + 1


def enqueue_minutes(
    db: Session,
    session: MeetingSession,
    actor: User,
    *,
    kind: str,
    base_version_id: uuid.UUID | None,
    instructions: str | None,
    format_snapshot: dict[str, Any] | None,
    request_key: str | None = None,
) -> uuid.UUID:
    """議事録生成を投入する。

    送信許可 (`allow_external_send`) と接続 owner (`connected_owner_id`) を settings へ
    必ず載せる (R05)。worker はこの 2 つと実際のログイン状態を送信直前に再検査するため、
    欠けていると外部送信は行われない。

    `request_key` (HTTP の Idempotency-Key) があるときだけ同じ HTTP 要求の再送を
    同じ job へ集約し、無いときは要求世代を進めて必ず新しい job を作る (R07)。
    """
    settings = get_settings()
    if session.status in PROCESSING_STATUSES:
        raise ApiException(409, "conflict", "処理中のセッションです")
    connection = check_can_send(db, session)
    transcript = latest_transcript(db, session)
    if transcript is None:
        raise ApiException(409, "conflict", "文字起こしがまだありません", stage="minutes")
    artifacts = [
        {"artifact_id": transcript.json_artifact_id, "kind": "transcript_json", "track_id": None, "role": None,
         "start_offset_ms": None, "revision": transcript.revision}
    ]
    base: MinutesVersion | None = None
    if base_version_id is not None:
        base = db.get(MinutesVersion, base_version_id)
        if base is None or base.session_id != session.id:
            raise ApiException(404, "not_found", "基準となる版が見つかりません")
        artifacts.append(
            {"artifact_id": base.artifact_id, "kind": "minutes_md", "track_id": None, "role": None,
             "start_offset_ms": None, "revision": base.version_number}
        )
    snapshot = format_snapshot or session.format_snapshot
    job_settings = MinutesJobSettings(
        kind=kind,
        allow_external_send=session.allow_external_send,
        connected_owner_id=connection.connected_owner_id,
        title=session.title,
        title_edited_by_user=session.title_edited_by_user,
        input_kind=InputKind(session.input_kind),
        format_snapshot=snapshot,
        instructions=instructions,
        started_at=session.started_at,
        duration_ms=session.duration_ms,
        transcript_revision=transcript.revision,
        expected_current_version_id=session.current_minutes_version_id,
    )
    if request_key:
        key = f"minutes:{session.id}:req:{request_key}"
    else:
        session.minutes_generation = (session.minutes_generation or 0) + 1
        db.flush()
        key = f"minutes:{session.id}:gen:{session.minutes_generation}"
    enqueued = jobs.enqueue(
        db,
        kind=JobKind.MINUTES_GENERATION,
        session_id=session.id,
        owner_id=session.owner_id,
        input={"artifacts": artifacts, "transcript_revision": transcript.revision,
               "parent_minutes_version_id": str(base.id) if base else None},
        settings=json.loads(job_settings.model_dump_json()),
        idempotency_key=key,
        timeout_seconds=settings.minutes_timeout_seconds,
    )
    if not enqueued.created:
        # 同じ HTTP 要求の再送。終了済み job を queued として扱わない
        return enqueued.job_id
    session.minutes_job_id = enqueued.job_id
    session.status = "queued_minutes"
    session.failure = None
    return enqueued.job_id


def to_contract(row: MinutesVersion) -> dict[str, Any]:
    document = MinutesVersionContract(
        version_id=row.id,
        session_id=row.session_id,
        version_number=row.version_number,
        kind=row.kind,
        created_by=row.created_by,
        created_at=row.created_at,
        parent_version_id=row.parent_version_id,
        restored_from_version_id=row.restored_from_version_id,
        transcript_revision=row.transcript_revision,
        format_snapshot=row.format_snapshot,
        instructions=row.instructions,
        insufficient_information=row.insufficient_information or [],
        title_proposal=row.title_proposal,
        artifact_id=row.artifact_id,
        is_candidate=row.is_candidate,
    )
    return json.loads(document.model_dump_json())


def read_body(row: MinutesVersion) -> str:
    with artifact_store().open(row.artifact_id) as handle:
        return handle.read().decode("utf-8")


def _store_markdown(db: Session, session: MeetingSession, body: str) -> Artifact:
    stored = artifact_store().put_bytes(body.encode("utf-8"))
    artifact = Artifact(
        id=stored.artifact_id, session_id=session.id, kind="minutes_md", content_type="text/markdown; charset=utf-8",
        byte_size=stored.byte_size, sha256=stored.sha256,
    )
    db.add(artifact)
    return artifact


def save_manual_edit(
    db: Session, session: MeetingSession, actor: User, *, parent_version_id: uuid.UUID, body_markdown: str,
    expected_current_version_id: uuid.UUID | None,
) -> MinutesVersion:
    lock_session(db, session)
    if session.current_minutes_version_id != expected_current_version_id:
        raise ApiException(
            409, "conflict", "現在版が変更されています。最新の版を確認してから保存してください",
            details={"current_minutes_version_id": str(session.current_minutes_version_id) if session.current_minutes_version_id else None},
        )
    parent = db.get(MinutesVersion, parent_version_id)
    if parent is None or parent.session_id != session.id:
        raise ApiException(404, "not_found", "親となる版が見つかりません")
    if len(body_markdown.encode("utf-8")) > 5 * 1024 * 1024:
        raise ApiException(400, "limit_exceeded", "議事録本文が大きすぎます")
    artifact = _store_markdown(db, session, body_markdown)
    version = MinutesVersion(
        session_id=session.id,
        version_number=next_version_number(db, session.id),
        kind="manual_edit",
        created_by=str(actor.id),
        parent_version_id=parent.id,
        transcript_revision=parent.transcript_revision,
        format_snapshot=parent.format_snapshot,
        artifact_id=artifact.id,
    )
    db.add(version)
    db.flush()
    session.current_minutes_version_id = version.id
    if session.status == "transcribed":
        session.status = "completed"
    audit.record(db, "minutes_manual_edit", actor_id=actor.id, target_type="session", target_id=session.id)
    return version


def select_current(db: Session, session: MeetingSession, actor: User, version_id: uuid.UUID) -> MinutesVersion:
    lock_session(db, session)
    version = db.get(MinutesVersion, version_id)
    if version is None or version.session_id != session.id:
        raise ApiException(404, "not_found", "版が見つかりません")
    session.current_minutes_version_id = version.id
    version.is_candidate = False
    audit.record(db, "minutes_current_selected", actor_id=actor.id, target_type="session", target_id=session.id)
    return version


def restore(db: Session, session: MeetingSession, actor: User, version_id: uuid.UUID) -> MinutesVersion:
    lock_session(db, session)
    source = db.get(MinutesVersion, version_id)
    if source is None or source.session_id != session.id:
        raise ApiException(404, "not_found", "版が見つかりません")
    version = MinutesVersion(
        session_id=session.id,
        version_number=next_version_number(db, session.id),
        kind="restored",
        created_by=str(actor.id),
        parent_version_id=session.current_minutes_version_id,
        restored_from_version_id=source.id,
        transcript_revision=source.transcript_revision,
        format_snapshot=source.format_snapshot,
        artifact_id=source.artifact_id,  # 本文は同じ artifact を共有 (不変)
    )
    db.add(version)
    db.flush()
    session.current_minutes_version_id = version.id
    audit.record(db, "minutes_restored", actor_id=actor.id, target_type="session", target_id=session.id)
    return version


def compare(db: Session, session: MeetingSession, from_id: uuid.UUID, to_id: uuid.UUID) -> str:
    left = db.get(MinutesVersion, from_id)
    right = db.get(MinutesVersion, to_id)
    if left is None or right is None or left.session_id != session.id or right.session_id != session.id:
        raise ApiException(404, "not_found", "版が見つかりません")
    diff = difflib.unified_diff(
        read_body(left).splitlines(keepends=True),
        read_body(right).splitlines(keepends=True),
        fromfile=f"v{left.version_number:03d}",
        tofile=f"v{right.version_number:03d}",
    )
    return "".join(diff)


def transcript_for(db: Session, session: MeetingSession) -> Transcript:
    transcript = latest_transcript(db, session)
    if transcript is None:
        raise ApiException(404, "not_found", "文字起こしがまだありません")
    return transcript
