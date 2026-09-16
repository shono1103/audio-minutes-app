"""セッションのドメイン操作: 作成・表現・finalize・retry・削除。HTTP 層から分離する。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from audio_minutes_contracts.artifacts import ChecksumMismatch, LocalArtifactStore
from audio_minutes_contracts.models import (
    InputKind,
    JobKind,
    RecordingPackage,
    TranscriptionJobSettings,
)
from audio_minutes_contracts.models import (
    Session as SessionContract,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from minutes_api import audit, formats_service, jobs
from minutes_api.config import Settings, get_settings
from minutes_api.errors import ApiException
from minutes_api.models import (
    Artifact,
    MeetingSession,
    MinutesVersion,
    SessionShare,
    Transcript,
    Upload,
    User,
)
from minutes_api.probe import ProbeError, probe_audio
from minutes_api.retention import current as current_retention
from minutes_api.security import now

PROCESSING_STATUSES = {"validating", "queued", "transcribing", "queued_minutes", "generating_minutes"}
CONTENT_TYPES = {"wav": "audio/wav", "m4a": "audio/mp4", "mp3": "audio/mpeg", "flac": "audio/flac"}


@dataclass(frozen=True)
class SessionDeletionPlan:
    """DB tombstone の commit 後に冪等削除する実体の一覧。"""

    artifact_ids: tuple[str, ...]
    upload_ids: tuple[uuid.UUID, ...]


def artifact_store(settings: Settings | None = None) -> LocalArtifactStore:
    return LocalArtifactStore((settings or get_settings()).artifacts_dir)


def upload_part_path(upload_id: uuid.UUID, settings: Settings | None = None) -> Path:
    directory = (settings or get_settings()).uploads_dir
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{upload_id}.part"


def provisional_title(package: RecordingPackage) -> str:
    """表示用タイトルを決める。

    クライアントは未入力のときも録音元・ファイル名と日時から仮タイトルを作って送る
    (FR-107 / FR-141)。利用者入力かどうかは `title_edited_by_user` が正で、タイトル
    文字列の有無では判定しない。ここは title が無い場合の最後の受け皿。
    """
    if package.title and package.title.strip():
        return package.title.strip()[:200]
    stamp = package.started_at.strftime("%Y-%m-%d %H:%M")
    if package.input_kind is InputKind.IMPORTED_MIXED:
        return f"取り込み {stamp}"
    label = package.source.app_name or "録音"
    return f"{label} {stamp}"


def create_session(db: Session, owner: User, package: RecordingPackage) -> MeetingSession:
    """session_id で冪等。同じ owner の再送は既存を返し、別 owner の衝突は 409。"""
    settings = get_settings()
    required = set(package.required_track_ids())
    provided = {track.track_id for track in package.tracks}
    if required != provided:
        raise ApiException(
            400, "invalid_input", "入力種別に必要なトラックが一致しません", stage="validation",
            details={"required": sorted(required), "provided": sorted(provided)},
        )
    for track in package.tracks:
        if track.duration_ms > settings.max_audio_ms:
            raise ApiException(400, "limit_exceeded", "音声は最長 4 時間までです", stage="validation")
        if track.byte_size > settings.max_upload_bytes:
            raise ApiException(400, "limit_exceeded", "取り込みファイルは最大 2 GiB までです", stage="validation")
    existing = db.get(MeetingSession, package.session_id)
    if existing is not None:
        if existing.owner_id != owner.id or existing.deleted_at is not None:
            raise ApiException(409, "conflict", "このセッション ID は使用できません")
        # 録音開始時に作った先行文字起こし用sessionを、停止後の完全な
        # recording-packageで通常sessionへ昇格する。再送済みならそのまま返す。
        if existing.package.get("live_recording") is not True:
            return existing
        if existing.finalized_at is not None:
            return existing
        if existing.input_kind != package.input_kind.value:
            raise ApiException(409, "conflict", "先行文字起こしの入力種別と一致しません")
        existing.title = provisional_title(package)
        existing.title_edited_by_user = package.title_edited_by_user
        existing.language_mode = package.language_mode.value
        existing.allow_external_send = package.allow_external_send
        existing.package = json.loads(package.model_dump_json())
        existing.status = "uploading"
        existing.started_at = package.started_at
        existing.duration_ms = max(track.start_offset_ms + track.duration_ms for track in package.tracks)
        _add_uploads(db, existing, package)
        audit.record(db, "live_session_promoted", actor_id=owner.id, target_type="session", target_id=existing.id)
        db.flush()
        return existing
    snapshot = formats_service.snapshot_for(db, owner, package.format_profile_id)
    session = MeetingSession(
        id=package.session_id,
        owner_id=owner.id,
        title=provisional_title(package),
        # 非空のタイトル = 利用者入力、とは判定しない。クライアントの仮タイトルを
        # 利用者編集済みと誤認すると、未入力時に Claude の提案タイトルを採用できなくなる
        title_edited_by_user=package.title_edited_by_user,
        title_revision=0,
        input_kind=package.input_kind.value,
        language_mode=package.language_mode.value,
        allow_external_send=package.allow_external_send,
        format_snapshot=snapshot,
        package=json.loads(package.model_dump_json()),
        status="uploading",
        started_at=package.started_at,
        duration_ms=max(track.start_offset_ms + track.duration_ms for track in package.tracks),
    )
    db.add(session)
    db.flush()
    _add_uploads(db, session, package)
    audit.record(db, "session_created", actor_id=owner.id, target_type="session", target_id=session.id, metadata={"input_kind": session.input_kind})
    db.flush()
    return session


def _add_uploads(db: Session, session: MeetingSession, package: RecordingPackage) -> None:
    """完全WAV用tus行を一度だけ作る。live session昇格と通常作成で共有する。"""
    existing = set(db.execute(select(Upload.track_id).where(Upload.session_id == session.id)).scalars())
    expires = now() + timedelta(hours=current_retention(db, get_settings()).upload_hours)
    for track in package.tracks:
        if track.track_id in existing:
            continue
        db.add(
            Upload(
                session_id=session.id,
                track_id=track.track_id,
                role=track.role.value,
                expected_length=track.byte_size,
                expected_sha256=track.sha256,
                state="pending",
                expires_at=expires,
            )
        )


def serialize(db: Session, session: MeetingSession, viewer_id: uuid.UUID) -> dict[str, Any]:
    settings = get_settings()
    uploads = db.execute(select(Upload).where(Upload.session_id == session.id).order_by(Upload.track_id)).scalars().all()
    shares = db.execute(select(SessionShare.user_id).where(SessionShare.session_id == session.id)).scalars().all()
    tracks = []
    for upload in uploads:
        show_upload = upload.state in ("pending", "uploading") and session.owner_id == viewer_id
        tracks.append(
            {
                "track_id": upload.track_id,
                "role": upload.role,
                "upload_state": upload.state,
                "upload_id": str(upload.id) if show_upload else None,
                "upload_url": f"{settings.public_base_url}/v1/uploads/{upload.id}" if show_upload else None,
                "upload_expires_at": upload.expires_at if show_upload else None,
                "artifact_id": upload.artifact_id,
            }
        )
    document = SessionContract(
        session_id=session.id,
        owner_id=session.owner_id,
        title=session.title,
        title_edited_by_user=session.title_edited_by_user,
        title_revision=session.title_revision,
        input_kind=session.input_kind,
        language_mode=session.language_mode,
        allow_external_send=session.allow_external_send,
        format_snapshot=session.format_snapshot if session.owner_id == viewer_id else None,
        status=session.status,
        failure=session.failure,
        started_at=session.started_at,
        duration_ms=session.duration_ms,
        created_at=session.created_at,
        updated_at=session.updated_at,
        tracks=tracks,
        current_minutes_version_id=session.current_minutes_version_id,
        transcript_revision=session.transcript_revision,
        review_count=session.review_count,
        audio_retained=session.audio_retained,
        audio_expires_at=session.audio_expires_at,
        shared_with=list(shares),
        is_shared_view=session.owner_id != viewer_id,
    )
    return json.loads(document.model_dump_json())


def _transcription_settings(session: MeetingSession, revision: int, language_mode: str) -> dict[str, Any]:
    """producer/consumer 共有の契約 (TranscriptionJobSettings) で組み立てる。"""
    document = TranscriptionJobSettings(
        input_kind=InputKind(session.input_kind),
        language_mode=language_mode,
        transcript_revision=revision,
        max_audio_ms=get_settings().max_audio_ms,
        requested_backend=None,  # worker の profile 設定に従う
        strategy=None,
    )
    return json.loads(document.model_dump_json())


def _audio_artifacts(db: Session, session: MeetingSession) -> list[Artifact]:
    return list(
        db.execute(
            select(Artifact)
            .where(Artifact.session_id == session.id, Artifact.kind == "audio_track", Artifact.deleted_at.is_(None))
            .order_by(Artifact.track_id)
        ).scalars()
    )


def enqueue_transcription(
    db: Session, session: MeetingSession, *, language_mode: str | None = None, request_key: str | None = None
) -> uuid.UUID:
    """文字起こしを投入する。

    3 つの数え方を分けて扱う (R07):

    * `transcript_revision` — 成功済みの最新 transcript の版。次に作る版はこれ + 1。
    * `transcription_generation` — 利用者が再処理を要求した世代。queue の冪等キーに使う。
      失敗後の再実行でも必ず新しい job になり、終了済み job を再利用しない。
    * job の `attempt` — worker 側の自動再試行。queue が管理する。

    `request_key` (HTTP の Idempotency-Key) があるときだけ、同じ HTTP 要求の再送を
    同じ job に集約する。無いときは要求ごとに新しい job を作る。
    """
    settings = get_settings()
    latest = latest_transcript(db, session)
    revision = (latest.revision if latest is not None else 0) + 1
    audio = _audio_artifacts(db, session)
    if not audio:
        raise ApiException(409, "audio_deleted", "音声が削除されているため再文字起こしできません", stage="transcription", retryable=False)
    package_tracks = {track["track_id"]: track for track in session.package["tracks"]}
    input_doc = {
        "artifacts": [
            {
                "artifact_id": artifact.id,
                "kind": "audio_track",
                "track_id": artifact.track_id,
                "role": artifact.role,
                "start_offset_ms": package_tracks[artifact.track_id]["start_offset_ms"],
                "revision": None,
            }
            for artifact in audio
        ],
        "transcript_revision": None,
        "parent_minutes_version_id": None,
    }
    mode = language_mode or session.language_mode
    if request_key:
        key = f"transcription:{session.id}:req:{request_key}"
    else:
        session.transcription_generation = (session.transcription_generation or 0) + 1
        db.flush()
        key = f"transcription:{session.id}:gen:{session.transcription_generation}"
    enqueued = jobs.enqueue(
        db,
        kind=JobKind.TRANSCRIPTION,
        session_id=session.id,
        owner_id=session.owner_id,
        input=input_doc,
        settings=_transcription_settings(session, revision, mode),
        idempotency_key=key,
        timeout_seconds=settings.transcription_timeout_seconds,
    )
    if not enqueued.created:
        # 同じ HTTP 要求の再送。既存 job (終了済みのこともある) をそのまま返し、
        # セッション状態を「待機中」へ戻さない
        return enqueued.job_id
    session.transcription_job_id = enqueued.job_id
    session.minutes_job_id = None
    session.language_mode = mode
    session.status = "queued"
    session.failure = None
    return enqueued.job_id


def finalize(db: Session, session: MeetingSession, actor: User) -> MeetingSession:
    """全 track の完了・整合性を検証し、一度だけ文字起こしを投入する。二重 finalize は同じ結果。"""
    settings = get_settings()
    db.refresh(session, with_for_update=True)
    if session.deleted_at is not None:
        raise ApiException(404, "not_found", "セッションが見つかりません")
    if session.finalized_at is not None:
        return session
    uploads = db.execute(select(Upload).where(Upload.session_id == session.id).order_by(Upload.track_id)).scalars().all()
    incomplete = [upload.track_id for upload in uploads if upload.state != "completed"]
    if incomplete:
        raise ApiException(
            409, "upload_incomplete", "すべてのトラックのアップロードが完了していません", stage="upload",
            details={"incomplete_tracks": incomplete},
        )
    session.status = "validating"
    store = artifact_store(settings)
    package_tracks = {track["track_id"]: track for track in session.package["tracks"]}
    created: list[str] = []
    try:
        for upload in uploads:
            declared = package_tracks[upload.track_id]
            part = upload_part_path(upload.id, settings)
            if not part.is_file() or part.stat().st_size != upload.expected_length:
                raise ApiException(409, "upload_incomplete", "受信済みデータの大きさが一致しません", stage="upload")
            if part.stat().st_size > settings.max_upload_bytes:
                raise ApiException(400, "limit_exceeded", "取り込みファイルは最大 2 GiB までです", stage="validation")
            try:
                probe = probe_audio(part)
            except ProbeError as exc:
                raise ApiException(400, exc.code, exc.message, stage="validation") from exc
            if probe.container != declared["container"] or probe.codec != declared["codec"]:
                raise ApiException(
                    400, "unsupported_format", "申告された形式と実際の形式が一致しません", stage="validation",
                    details={"track_id": upload.track_id, "actual_container": probe.container, "actual_codec": probe.codec},
                )
            if probe.duration_ms > settings.max_audio_ms:
                raise ApiException(400, "limit_exceeded", "音声は最長 4 時間までです", stage="validation")
            tolerance = max(2000, int(declared["duration_ms"] * 0.02))
            if abs(probe.duration_ms - declared["duration_ms"]) > tolerance:
                raise ApiException(
                    400, "invalid_input", "申告された再生時間と実際の再生時間が一致しません", stage="validation",
                    details={"track_id": upload.track_id, "actual_duration_ms": probe.duration_ms},
                )
            try:
                stored = store.put_file(part, expected_sha256=upload.expected_sha256)
            except ChecksumMismatch as exc:
                raise ApiException(400, "checksum_mismatch", "checksum が一致しません", stage="validation", details={"track_id": upload.track_id}) from exc
            created.append(stored.artifact_id)
            db.add(
                Artifact(
                    id=stored.artifact_id,
                    session_id=session.id,
                    kind="audio_track",
                    track_id=upload.track_id,
                    role=upload.role,
                    content_type=CONTENT_TYPES.get(declared["container"], "application/octet-stream"),
                    byte_size=stored.byte_size,
                    sha256=stored.sha256,
                )
            )
            upload.artifact_id = stored.artifact_id
        db.flush()
        session.finalized_at = now()
        session.audio_expires_at = now() + timedelta(days=current_retention(db, settings).audio_days)
        # 録音中chunkが連続していれば、その完了を待って最終Transcriptへ結合する。
        # 欠番・失敗時はsettle_live_sessionが完全WAVの従来jobへ戻す。
        from minutes_api.reconciler import settle_live_session

        settle_live_session(db, session)
        audit.record(db, "session_finalized", actor_id=actor.id, target_type="session", target_id=session.id)
        db.flush()
    except Exception:
        for artifact_id in created:
            store.delete(artifact_id)
        session.status = "uploading"
        raise
    for upload in uploads:
        upload_part_path(upload.id, settings).unlink(missing_ok=True)
    return session


def retry(
    db: Session, session: MeetingSession, actor: User, *, stage: str, language_mode: str | None,
    request_key: str | None = None,
) -> uuid.UUID:
    # retention/finalize/reconciler と同じ session row lock を取り、期限切れ判定と
    # job 投入を直列化する。lock 前に読んだ audio_retained/status は使わない。
    db.refresh(session, with_for_update=True)
    if session.deleted_at is not None:
        raise ApiException(404, "not_found", "セッションが見つかりません")
    if session.status in PROCESSING_STATUSES:
        raise ApiException(409, "conflict", "処理中のセッションは再実行できません")
    if stage == "transcription":
        if not session.audio_retained:
            raise ApiException(409, "audio_deleted", "音声が削除されているため再文字起こしできません", stage="transcription", retryable=False)
        if session.finalized_at is None:
            raise ApiException(409, "upload_incomplete", "セッションが確定していません", stage="upload")
        job_id = enqueue_transcription(db, session, language_mode=language_mode, request_key=request_key)
        audit.record(db, "retry_transcription", actor_id=actor.id, target_type="session", target_id=session.id)
        return job_id
    if stage == "minutes":
        from minutes_api.minutes_service import enqueue_minutes

        job_id = enqueue_minutes(
            db, session, actor, kind="claude_generated", base_version_id=None, instructions=None,
            format_snapshot=None, request_key=request_key,
        )
        audit.record(db, "retry_minutes", actor_id=actor.id, target_type="session", target_id=session.id)
        return job_id
    raise ApiException(400, "invalid_request", "stage は transcription か minutes を指定してください")


def tombstone_session(db: Session, session: MeetingSession, actor: User) -> SessionDeletionPlan:
    """DB上のアクセス・job・参照を先に停止し、commit後の実体削除計画を返す。"""
    db.refresh(session, with_for_update=True)
    timestamp = session.deleted_at or now()
    newly_deleted = session.deleted_at is None
    session.deleted_at = timestamp
    session.status = "deleting"
    jobs.cancel_all_for_session(db, session.id)
    artifacts = list(db.execute(select(Artifact).where(Artifact.session_id == session.id)).scalars())
    uploads = list(db.execute(select(Upload).where(Upload.session_id == session.id)).scalars())
    for artifact in artifacts:
        artifact.deleted_at = artifact.deleted_at or timestamp
    for upload in uploads:
        if upload.state in ("pending", "uploading", "completed", "expired"):
            upload.state = "cancelled"
        upload.write_claim_id = None
        upload.write_claim_expires_at = None
    if newly_deleted:
        audit.record(db, "session_deleted", actor_id=actor.id, target_type="session", target_id=session.id)
    db.flush()
    return SessionDeletionPlan(
        artifact_ids=tuple(artifact.id for artifact in artifacts),
        upload_ids=tuple(upload.id for upload in uploads),
    )


def cleanup_deleted_session(plan: SessionDeletionPlan, settings: Settings | None = None) -> None:
    """tombstone 済みセッションのファイルを冪等に削除する。"""
    settings = settings or get_settings()
    store = artifact_store(settings)
    for upload_id in plan.upload_ids:
        upload_part_path(upload_id, settings).unlink(missing_ok=True)
    for artifact_id in plan.artifact_ids:
        store.delete(artifact_id)


def delete_session(db: Session, session: MeetingSession, actor: User) -> None:
    """DB tombstone/cancel を確定した後に実体を削除する。再実行しても同じ結果。"""
    plan = tombstone_session(db, session, actor)
    # db_dependency の終了時 commit に任せると、ファイル削除後の commit 失敗で
    # DB が参照中なのに実体だけ失われる。ここが明示的な永続境界。
    db.commit()
    try:
        cleanup_deleted_session(plan)
    except OSError as exc:
        # tombstone は既に確定済み。次回 DELETE / retention sweep で同じ計画を再実行できる。
        raise ApiException(503, "internal", "セッションの実体削除を完了できません", stage="storage", retryable=True) from exc


def latest_transcript(db: Session, session: MeetingSession) -> Transcript | None:
    return db.execute(
        select(Transcript).where(Transcript.session_id == session.id).order_by(Transcript.revision.desc())
    ).scalars().first()


def current_minutes(db: Session, session: MeetingSession) -> MinutesVersion | None:
    if session.current_minutes_version_id is None:
        return None
    return db.get(MinutesVersion, session.current_minutes_version_id)
