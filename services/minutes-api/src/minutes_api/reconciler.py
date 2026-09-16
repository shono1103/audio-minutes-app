"""worker の jobs.result を API 所有の業務テーブルへ冪等に確定する。"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from audio_minutes_contracts.models import JobFailure, JobKind, WorkerResult
from audio_minutes_contracts.models import Transcript as TranscriptContract
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from minutes_api import live_transcription, minutes_service
from minutes_api.models import (
    Artifact,
    ClaudeConnection,
    LiveAudioChunk,
    MeetingSession,
    MinutesVersion,
    Transcript,
    User,
)
from minutes_api.sessions_service import artifact_store

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "leased", "running")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")


@dataclass(frozen=True)
class ReconcileStats:
    active_updated: int = 0
    terminal_reconciled: int = 0


class InvalidWorkerResult(ValueError):
    pass


def _json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _failure(code: str, stage: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return json.loads(
        JobFailure(
            code=code,
            stage=stage,
            retryable=retryable,
            message=message,
            retained_artifacts=[],
        ).model_dump_json()
    )


def _current_job(session: MeetingSession, job: Mapping[str, Any]) -> bool:
    pointer = session.transcription_job_id if job["kind"] == JobKind.TRANSCRIPTION.value else session.minutes_job_id
    return pointer == job["job_id"]


def _validated_artifact(
    db: Session,
    session: MeetingSession,
    result_artifact: Any,
) -> Artifact:
    stat = _validate_artifact_file(result_artifact)
    existing = db.get(Artifact, result_artifact.artifact_id)
    content_type = result_artifact.content_type or (
        "application/json" if result_artifact.kind == "transcript_json" else "text/markdown; charset=utf-8"
    )
    if existing is not None:
        if (
            existing.session_id != session.id
            or existing.kind != result_artifact.kind
            or existing.byte_size != stat.byte_size
            or existing.sha256 != stat.sha256
        ):
            raise InvalidWorkerResult(f"artifact ID が別の成果物と衝突しました: {result_artifact.artifact_id}")
        return existing
    row = Artifact(
        id=result_artifact.artifact_id,
        session_id=session.id,
        kind=result_artifact.kind,
        content_type=content_type,
        byte_size=stat.byte_size,
        sha256=stat.sha256,
    )
    db.add(row)
    db.flush()
    return row


def _validate_artifact_file(result_artifact: Any):
    store = artifact_store()
    try:
        stat = store.stat(result_artifact.artifact_id)
    except (FileNotFoundError, ValueError) as exc:
        raise InvalidWorkerResult(f"artifact が存在しません: {result_artifact.artifact_id}") from exc
    if stat.byte_size != result_artifact.byte_size or stat.sha256 != result_artifact.sha256:
        raise InvalidWorkerResult(f"artifact の size/checksum が一致しません: {result_artifact.artifact_id}")
    return stat


def _reconcile_transcription(db: Session, session: MeetingSession, job: Mapping[str, Any], result: WorkerResult) -> None:
    if result.kind is not JobKind.TRANSCRIPTION or result.outcome != "succeeded":
        raise InvalidWorkerResult("文字起こし job の result 種別が一致しません")
    by_kind = {item.kind: item for item in result.artifacts}
    if (
        "transcript_json" not in by_kind
        or set(by_kind) - {"transcript_json", "transcript_md"}
        or len(by_kind) != len(result.artifacts)
    ):
        raise InvalidWorkerResult("文字起こし成果物の構成が不正です")
    for item in by_kind.values():
        _validate_artifact_file(item)
    try:
        with artifact_store().open(by_kind["transcript_json"].artifact_id) as handle:
            transcript_doc = TranscriptContract.model_validate_json(handle.read())
    except Exception as exc:  # noqa: BLE001 - schema/JSON/IO を同じ worker result 不正として扱う
        raise InvalidWorkerResult("transcript.json を検証できません") from exc
    expected_revision = int((_json(job.get("settings")) or {}).get("transcript_revision") or 0)
    if transcript_doc.session_id != session.id or transcript_doc.revision != expected_revision:
        raise InvalidWorkerResult("transcript の session/revision が job と一致しません")
    input_ids = {
        item.get("artifact_id")
        for item in (_json(job.get("input")) or {}).get("artifacts", [])
        if item.get("kind") == "audio_track"
    }
    if {track.source_artifact_id for track in transcript_doc.tracks} != input_ids:
        raise InvalidWorkerResult("transcript の入力音声が job と一致しません")

    row = db.execute(select(Transcript).where(Transcript.job_id == job["job_id"])).scalars().first()
    if row is None:
        same_revision = db.execute(
            select(Transcript).where(Transcript.session_id == session.id, Transcript.revision == transcript_doc.revision)
        ).scalars().first()
        if same_revision is not None:
            raise InvalidWorkerResult("transcript revision が別 job で既に確定されています")
        json_artifact = _validated_artifact(db, session, by_kind["transcript_json"])
        md_artifact = _validated_artifact(db, session, by_kind["transcript_md"]) if "transcript_md" in by_kind else None
        row = Transcript(
            session_id=session.id,
            revision=transcript_doc.revision,
            json_artifact_id=json_artifact.id,
            md_artifact_id=md_artifact.id if md_artifact else None,
            processing=json.loads(transcript_doc.processing.model_dump_json()),
            review_count=sum(1 for item in transcript_doc.review if not item.resolved),
            job_id=job["job_id"],
        )
        db.add(row)
        db.flush()
    session.transcript_revision = row.revision
    session.review_count = row.review_count
    session.status = "transcribed"
    session.failure = None

    # 外部送信禁止は正常停止。許可済みでも接続 owner が一致しない場合は transcript を
    # 確定したまま止め、owner の明示的な再実行を待つ。
    if not session.allow_external_send:
        return
    owner = db.get(User, session.owner_id)
    connection = db.get(ClaudeConnection, 1)
    if owner is None or owner.disabled_at is not None or connection is None or connection.state != "logged_in":
        session.failure = _failure(
            "claude_not_authenticated", "minutes", "文字起こしは完了しました。Claude への接続後に議事録生成を再実行してください", retryable=True
        )
        return
    if connection.connected_owner_id != session.owner_id:
        session.failure = _failure(
            "not_connected_owner", "minutes", "文字起こしは完了しました。接続 owner 本人が議事録生成を再実行してください"
        )
        return
    minutes_service.enqueue_minutes(
        db,
        session,
        owner,
        kind="claude_generated",
        base_version_id=None,
        instructions=None,
        format_snapshot=None,
    )


def _is_live_chunk_job(job: Mapping[str, Any]) -> bool:
    return (_json(job.get("settings")) or {}).get("live_chunk_sequence") is not None


def _reconcile_live_chunk(db: Session, session: MeetingSession, job: Mapping[str, Any], result: WorkerResult) -> None:
    """chunk成果物を仮保存する。公開TranscriptとClaude処理は停止後の全体統合まで進めない。"""
    if result.kind is not JobKind.TRANSCRIPTION or result.outcome != "succeeded":
        raise InvalidWorkerResult("先行文字起こしjobのresult種別が一致しません")
    by_kind = {item.kind: item for item in result.artifacts}
    if "transcript_json" not in by_kind or set(by_kind) - {"transcript_json", "transcript_md"}:
        raise InvalidWorkerResult("先行文字起こし成果物の構成が不正です")
    try:
        with artifact_store().open(by_kind["transcript_json"].artifact_id) as handle:
            document = TranscriptContract.model_validate_json(handle.read())
    except (OSError, ValueError) as exc:
        raise InvalidWorkerResult("先行文字起こし成果物を読み込めません") from exc
    if document.session_id != session.id:
        raise InvalidWorkerResult("先行文字起こしのsessionがjobと一致しません")
    input_ids = {
        item.get("artifact_id")
        for item in (_json(job.get("input")) or {}).get("artifacts", [])
        if item.get("kind") == "audio_track"
    }
    if {track.source_artifact_id for track in document.tracks} != input_ids:
        raise InvalidWorkerResult("先行文字起こしの入力音声がjobと一致しません")
    json_artifact = _validated_artifact(db, session, by_kind["transcript_json"])
    md_artifact = _validated_artifact(db, session, by_kind["transcript_md"]) if "transcript_md" in by_kind else None
    rows = list(db.execute(select(LiveAudioChunk).where(LiveAudioChunk.job_id == job["job_id"])).scalars())
    if {row.audio_artifact_id for row in rows} != input_ids:
        raise InvalidWorkerResult("先行文字起こしchunkとjob入力が一致しません")
    for row in rows:
        row.transcript_json_artifact_id = json_artifact.id
        row.transcript_md_artifact_id = md_artifact.id if md_artifact else None
        row.state = "transcribed"
        row.failure = None


def _publish_live_transcript(db: Session, session: MeetingSession, rows: list[LiveAudioChunk]) -> None:
    transcript = live_transcription.merged_transcript(db, session, rows)
    store = artifact_store()
    json_stored = store.put_bytes(transcript.model_dump_json().encode("utf-8"))
    md_stored = store.put_bytes(transcript.to_markdown().encode("utf-8"))
    result = WorkerResult(
        kind=JobKind.TRANSCRIPTION,
        outcome="succeeded",
        artifacts=[
            {
                "artifact_id": json_stored.artifact_id,
                "kind": "transcript_json",
                "byte_size": json_stored.byte_size,
                "sha256": json_stored.sha256,
                "content_type": "application/json",
            },
            {
                "artifact_id": md_stored.artifact_id,
                "kind": "transcript_md",
                "byte_size": md_stored.byte_size,
                "sha256": md_stored.sha256,
                "content_type": "text/markdown",
            },
        ],
        processing=transcript.processing,
    )
    audio = list(
        db.execute(
            select(Artifact)
            .where(Artifact.session_id == session.id, Artifact.kind == "audio_track", Artifact.deleted_at.is_(None))
            .order_by(Artifact.track_id)
        ).scalars()
    )
    synthetic_job = {
        "job_id": uuid.uuid5(uuid.NAMESPACE_URL, f"audio-minutes:live-final:{session.id}:{transcript.revision}"),
        "kind": JobKind.TRANSCRIPTION.value,
        "session_id": session.id,
        "input": {
            "artifacts": [
                {"artifact_id": item.id, "kind": "audio_track", "track_id": item.track_id}
                for item in audio
            ]
        },
        "settings": {"transcript_revision": transcript.revision},
    }
    _reconcile_transcription(db, session, synthetic_job, result)


def settle_live_session(db: Session, session: MeetingSession) -> str:
    """停止後のchunk群を公開するか、完全WAVの通常jobへフォールバックする。"""
    from minutes_api.sessions_service import enqueue_transcription

    rows = live_transcription.rows_for_session(db, session.id)
    state = live_transcription.finalization_state(session, rows)
    if state == "ready":
        _publish_live_transcript(db, session, rows)
        return "merged"
    if state in {"absent", "fallback"}:
        if session.transcription_job_id is None and session.transcript_revision is None:
            enqueue_transcription(db, session)
        return "fallback"
    session.status = "transcribing"
    return "waiting"


def _reconcile_minutes(db: Session, session: MeetingSession, job: Mapping[str, Any], result: WorkerResult) -> None:
    if result.kind is not JobKind.MINUTES_GENERATION:
        raise InvalidWorkerResult("議事録 job の result 種別が一致しません")
    if result.outcome == "unknown":
        if result.artifacts:
            raise InvalidWorkerResult("outcome=unknown に成果物が含まれています")
        session.status = "failed"
        session.failure = _failure(
            "claude_unknown_outcome",
            "minutes",
            "Claude への送信後に応答を確認できませんでした。自動再送せず、状態確認後の明示的な再実行を待ちます",
        )
        return
    if result.outcome != "succeeded" or len(result.artifacts) != 1 or result.artifacts[0].kind != "minutes_md":
        raise InvalidWorkerResult("議事録成果物の構成が不正です")
    _validate_artifact_file(result.artifacts[0])
    existing = db.execute(select(MinutesVersion).where(MinutesVersion.job_id == job["job_id"])).scalars().first()
    if existing is not None:
        return
    settings = _json(job.get("settings")) or {}
    input_doc = _json(job.get("input")) or {}
    expected_raw = settings.get("expected_current_version_id")
    expected = uuid.UUID(expected_raw) if expected_raw else None
    parent_raw = input_doc.get("parent_minutes_version_id")
    parent = uuid.UUID(parent_raw) if parent_raw else None
    artifact = _validated_artifact(db, session, result.artifacts[0])
    version = MinutesVersion(
        session_id=session.id,
        version_number=minutes_service.next_version_number(db, session.id),
        kind=settings.get("kind", "claude_generated"),
        created_by="system:minutes-worker",
        parent_version_id=parent,
        transcript_revision=settings.get("transcript_revision"),
        format_snapshot=settings.get("format_snapshot"),
        instructions=settings.get("instructions"),
        insufficient_information=result.insufficient_information,
        title_proposal=result.title_proposal,
        artifact_id=artifact.id,
        is_candidate=session.current_minutes_version_id != expected,
        job_id=job["job_id"],
    )
    db.add(version)
    db.flush()
    if not version.is_candidate:
        session.current_minutes_version_id = version.id
        session.status = "completed"
        session.failure = None
        if not session.title_edited_by_user and result.title_proposal:
            session.title = result.title_proposal[:200]
    else:
        # 手動編集・別生成が先に current を進めた。生成物は候補版として残すが上書きしない。
        session.status = "completed" if session.current_minutes_version_id else "transcribed"


def reconcile_job(db: Session, job: Mapping[str, Any]) -> None:
    """ロック済み terminal job 一件を反映する。古い世代・削除済みは公開しない。"""
    session = db.execute(
        select(MeetingSession).where(MeetingSession.id == job["session_id"]).with_for_update()
    ).scalars().first()
    live_chunk = _is_live_chunk_job(job)
    if session is None or session.deleted_at is not None or (not live_chunk and not _current_job(session, job)):
        return
    if live_chunk:
        rows = list(db.execute(select(LiveAudioChunk).where(LiveAudioChunk.job_id == job["job_id"])).scalars())
        if job["status"] in {"failed", "cancelled"}:
            for row in rows:
                row.state = job["status"]
                row.failure = _json(job.get("failure")) or _failure("internal", "transcription", "先行文字起こしに失敗しました")
        else:
            try:
                _reconcile_live_chunk(db, session, job, WorkerResult.model_validate(_json(job.get("result"))))
            except (InvalidWorkerResult, ValueError, TypeError) as exc:
                logger.warning("先行文字起こし成果物を確定できません job=%s reason=%s", job["job_id"], exc)
                for row in rows:
                    row.state = "failed"
                    row.failure = _failure("internal", "storage", "先行文字起こし成果物を検証できません")
        if session.finalized_at is not None:
            settle_live_session(db, session)
        return
    if job["status"] == "failed":
        session.status = "failed"
        stage = "transcription" if job["kind"] == JobKind.TRANSCRIPTION.value else "minutes"
        session.failure = _json(job.get("failure")) or _failure("internal", stage, "worker が失敗しました")
        return
    if job["status"] == "cancelled":
        session.status = "failed"
        session.failure = _failure("cancelled", "transcription" if job["kind"] == "transcription" else "minutes", "処理は取り消されました")
        return
    try:
        result = WorkerResult.model_validate(_json(job.get("result")))
        if job["kind"] == JobKind.TRANSCRIPTION.value:
            _reconcile_transcription(db, session, job, result)
        elif job["kind"] == JobKind.MINUTES_GENERATION.value:
            _reconcile_minutes(db, session, job, result)
        else:
            raise InvalidWorkerResult("未対応の job kind です")
    except (InvalidWorkerResult, ValueError, TypeError) as exc:
        logger.warning("worker result を確定できません job=%s reason=%s", job["job_id"], exc)
        session.status = "failed"
        session.failure = _failure("internal", "storage", "worker 成果物を検証できません")


def _update_active_states(db: Session) -> int:
    rows = db.execute(
        text(
            "SELECT job_id, kind, session_id, status FROM jobs "
            "WHERE status IN ('queued', 'leased', 'running') ORDER BY created_at"
        )
    ).mappings()
    changed = 0
    for job in rows:
        session = db.get(MeetingSession, job["session_id"])
        if session is None or session.deleted_at is not None or not _current_job(session, job):
            continue
        target = (
            "queued" if job["kind"] == "transcription" and job["status"] == "queued"
            else "transcribing" if job["kind"] == "transcription"
            else "queued_minutes" if job["status"] == "queued"
            else "generating_minutes"
        )
        if session.status != target:
            session.status = target
            changed += 1
    return changed


def reconcile_once(db: Session, *, limit: int = 50) -> ReconcileStats:
    """短い1 transactionでactive状態とterminal結果を取り込む。複数APIではSKIP LOCKED。"""
    active_updated = _update_active_states(db)
    statement = (
        "SELECT job_id, kind, session_id, owner_id, input, settings, status, result, failure, finished_at "
        "FROM jobs WHERE status IN ('succeeded', 'failed', 'cancelled') AND reconciled_at IS NULL "
        "ORDER BY finished_at NULLS FIRST, job_id FOR UPDATE SKIP LOCKED LIMIT :limit"
    )
    rows = list(db.execute(text(statement), {"limit": limit}).mappings())
    for job in rows:
        reconcile_job(db, job)
        db.execute(text("UPDATE jobs SET reconciled_at = now() WHERE job_id = :job_id"), {"job_id": job["job_id"]})
    return ReconcileStats(active_updated=active_updated, terminal_reconciled=len(rows))


__all__ = ["InvalidWorkerResult", "ReconcileStats", "reconcile_job", "reconcile_once"]
