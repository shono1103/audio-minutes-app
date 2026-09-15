"""配置単位の保持設定と、期限切れデータの回収計画。

DB の状態変更を先に commit し、その後にファイルを消す二段階にする。ファイル削除に
失敗しても次の sweep が削除済み行を再度対象にでき、DB が参照中なのに実体だけ消える
transaction rollback を避ける。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from minutes_api.config import Settings, get_settings
from minutes_api.models import Artifact, AuditLog, MeetingSession, Setting, Upload
from minutes_api.security import now


@dataclass(frozen=True)
class RetentionPolicy:
    upload_hours: int
    audio_days: int
    log_days: int


@dataclass(frozen=True)
class SweepPlan:
    upload_ids: tuple[uuid.UUID, ...]
    artifact_ids: tuple[str, ...]
    expired_uploads: int
    expired_audio_sessions: int
    audit_logs: int


def current(db: Session, settings: Settings | None = None) -> RetentionPolicy:
    settings = settings or get_settings()
    row = db.get(Setting, "retention")
    values = row.value if row is not None else {}
    return RetentionPolicy(
        upload_hours=int(values.get("upload_hours", settings.retention_upload_hours)),
        audio_days=int(values.get("audio_days", settings.retention_audio_days)),
        log_days=int(values.get("log_days", settings.retention_log_days)),
    )


def apply_to_existing(db: Session, policy: RetentionPolicy) -> None:
    """設定変更を既存の未完了 upload / 保持中音声にも反映する。"""
    for upload in db.execute(
        select(Upload).join(MeetingSession).where(
            MeetingSession.finalized_at.is_(None), Upload.state.in_(("pending", "uploading", "completed"))
        )
    ).scalars():
        upload.expires_at = upload.created_at + timedelta(hours=policy.upload_hours)
    for session in db.execute(
        select(MeetingSession).where(
            MeetingSession.finalized_at.is_not(None), MeetingSession.audio_retained.is_(True), MeetingSession.deleted_at.is_(None)
        )
    ).scalars():
        session.audio_expires_at = session.finalized_at + timedelta(days=policy.audio_days)


def _has_active_transcription(db: Session, session_id: uuid.UUID) -> bool:
    """session row lock 取得後に jobs の現在値を再検査する。"""
    try:
        return db.execute(
            text(
                "SELECT 1 FROM jobs WHERE session_id = :session_id AND kind = 'transcription' "
                "AND status IN ('queued', 'leased', 'running') LIMIT 1"
            ),
            {"session_id": session_id},
        ).first() is not None
    except SQLAlchemyError:
        # 業務テーブルだけを作る SQLite 単体試験向け。productionでjobsを読めない場合は
        # activeなしと誤認して音声を消さず、sweep全体を失敗させる。
        if db.get_bind().dialect.name == "sqlite":
            return False
        raise


def _lock_session(db: Session, session_id: uuid.UUID) -> MeetingSession | None:
    return db.execute(
        select(MeetingSession).where(MeetingSession.id == session_id).with_for_update()
    ).scalars().first()


def _job_artifact_ids(db: Session) -> set[str]:
    """未調停 result と実行中 input を孤立 artifact 回収から保護する。"""
    protected: set[str] = set()
    try:
        rows = db.execute(
            text(
                "SELECT input, result FROM jobs WHERE reconciled_at IS NULL "
                "OR status IN ('queued', 'leased', 'running')"
            )
        ).mappings()
    except SQLAlchemyError:
        if db.get_bind().dialect.name == "sqlite":
            return protected
        raise
    for row in rows:
        for value in (row["input"], row["result"]):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    continue
            if not isinstance(value, dict):
                continue
            for artifact in value.get("artifacts", []):
                if isinstance(artifact, dict) and isinstance(artifact.get("artifact_id"), str):
                    protected.add(artifact["artifact_id"])
    return protected


def plan_sweep(
    db: Session,
    *,
    at: datetime | None = None,
    active_transcription_sessions: set[uuid.UUID] | None = None,
) -> SweepPlan:
    """期限切れをDBへ反映し、commit 後に消すファイルIDを返す。冪等。"""
    at = at or now()
    policy = current(db)
    expired_upload_ids: list[uuid.UUID] = []
    expired_upload_count = 0
    upload_session_ids = list(
        db.execute(
            select(Upload.session_id).join(MeetingSession).where(
                MeetingSession.finalized_at.is_(None),
                MeetingSession.deleted_at.is_(None),
                Upload.state.in_(("pending", "uploading", "completed", "expired")),
                Upload.expires_at <= at,
            ).distinct()
        ).scalars()
    )
    for session_id in upload_session_ids:
        session = _lock_session(db, session_id)
        if session is None or session.deleted_at is not None or session.finalized_at is not None:
            continue
        uploads = db.execute(
            select(Upload).where(
                Upload.session_id == session_id,
                Upload.state.in_(("pending", "uploading", "completed", "expired")),
                Upload.expires_at <= at,
            )
        ).scalars()
        changed = False
        for upload in uploads:
            if (
                upload.write_claim_id is not None
                and upload.write_claim_expires_at is not None
                and upload.write_claim_expires_at > at
            ):
                continue
            if upload.state != "expired":
                upload.state = "expired"
                upload.write_claim_id = None
                upload.write_claim_expires_at = None
                expired_upload_count += 1
                changed = True
            expired_upload_ids.append(upload.id)
        if changed and session.finalized_at is None:
            session.status = "failed"
            session.failure = {
                "code": "upload_expired",
                "stage": "storage",
                "retryable": False,
                "message": "未完了アップロードの保持期限が切れました",
                "retained_artifacts": [],
            }

    artifact_ids: list[str] = []
    expired_audio_sessions = 0
    audio_session_ids = list(
        db.execute(
            select(MeetingSession.id).where(
                MeetingSession.deleted_at.is_(None),
                MeetingSession.audio_retained.is_(True),
                MeetingSession.audio_expires_at.is_not(None),
                MeetingSession.audio_expires_at <= at,
            )
        ).scalars()
    )
    for session_id in audio_session_ids:
        session = _lock_session(db, session_id)
        # candidate 選択後に finalize/retry/delete が先行した可能性があるため、
        # lock の内側で期限・保持・active job をすべて再評価する。
        if (
            session is None
            or session.deleted_at is not None
            or not session.audio_retained
            or session.audio_expires_at is None
            or session.audio_expires_at > at
            or session.status in ("queued", "transcribing", "validating")
        ):
            continue
        active = (
            session.id in active_transcription_sessions
            if active_transcription_sessions is not None
            else _has_active_transcription(db, session.id)
        )
        if active:
            continue
        audio = list(
            db.execute(
                select(Artifact).where(
                    Artifact.session_id == session.id,
                    Artifact.kind == "audio_track",
                    Artifact.deleted_at.is_(None),
                )
            ).scalars()
        )
        for artifact in audio:
            artifact.deleted_at = at
            artifact_ids.append(artifact.id)
        session.audio_retained = False
        expired_audio_sessions += 1

    # 既に deleted_at の行は、前回ファイル削除だけ失敗した場合の再試行対象にする。
    artifact_ids.extend(
        db.execute(select(Artifact.id).where(Artifact.deleted_at.is_not(None))).scalars()
    )
    cutoff = at - timedelta(days=policy.log_days)
    audit_result = db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
    return SweepPlan(
        upload_ids=tuple(dict.fromkeys(expired_upload_ids)),
        artifact_ids=tuple(dict.fromkeys(artifact_ids)),
        expired_uploads=expired_upload_count,
        expired_audio_sessions=expired_audio_sessions,
        audit_logs=audit_result.rowcount or 0,
    )

def delete_planned_files(plan: SweepPlan, settings: Settings | None = None) -> None:
    from minutes_api.sessions_service import artifact_store, upload_part_path

    settings = settings or get_settings()
    store = artifact_store(settings)
    for upload_id in plan.upload_ids:
        upload_part_path(upload_id, settings).unlink(missing_ok=True)
    for artifact_id in plan.artifact_ids:
        store.delete(artifact_id)


def sweep_orphan_artifacts(
    db: Session, *, settings: Settings | None = None, grace_seconds: int = 24 * 3600
) -> int:
    """DB/未調停jobから参照されず、十分古い不変artifactだけを削除する。"""
    settings = settings or get_settings()
    from minutes_api.sessions_service import artifact_store

    store = artifact_store(settings)
    referenced = set(db.execute(select(Artifact.id).where(Artifact.deleted_at.is_(None))).scalars())
    referenced.update(_job_artifact_ids(db))
    cutoff = time.time() - grace_seconds
    removed = 0
    for artifact_id in store.iter_ids():
        path = store.path(artifact_id)
        if artifact_id not in referenced and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def sweep_log_files(settings: Settings | None, policy: RetentionPolicy, *, at: datetime | None = None) -> int:
    settings = settings or get_settings()
    cutoff = (at or now()).timestamp() - policy.log_days * 86400
    removed = 0
    if not settings.log_dir.is_dir():
        return 0
    # maintenance marker 等の制御ファイルは対象外。runner が作る archive だけを扱う。
    for path in settings.log_dir.rglob("*.log"):
        if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def sweep_temp_files(db: Session, settings: Settings | None, *, older_than_seconds: int) -> int:
    """worker の途中artifactと、DB行を持たない古いupload partを回収する。"""
    settings = settings or get_settings()
    from minutes_api.sessions_service import artifact_store

    removed = artifact_store(settings).sweep_temp(older_than_seconds)
    cutoff = time.time() - older_than_seconds
    if settings.uploads_dir.is_dir():
        for path in settings.uploads_dir.glob("*.part"):
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                try:
                    upload_id = uuid.UUID(path.stem)
                except ValueError:
                    path.unlink(missing_ok=True)
                    removed += 1
                    continue
                # upload 行がある part は、期限・write claim と整合を取る plan_sweep が
                # 所有する。クラッシュで DB transaction だけ rollback した UUID part は
                # 参照がないため、grace 経過後にここで回収する。
                if db.get(Upload, upload_id) is None:
                    path.unlink(missing_ok=True)
                    removed += 1
    return removed


__all__ = [
    "RetentionPolicy",
    "SweepPlan",
    "apply_to_existing",
    "current",
    "delete_planned_files",
    "plan_sweep",
    "sweep_log_files",
    "sweep_orphan_artifacts",
    "sweep_temp_files",
]
