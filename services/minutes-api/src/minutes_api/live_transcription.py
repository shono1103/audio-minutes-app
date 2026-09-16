"""録音中chunkの受付・投入・停止後のTranscript結合。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import timedelta
from typing import Any, Literal

from audio_minutes_contracts.models import JobKind
from audio_minutes_contracts.models import Transcript as TranscriptContract
from sqlalchemy import select
from sqlalchemy.orm import Session

from minutes_api import audit, formats_service, jobs
from minutes_api.config import get_settings
from minutes_api.errors import ApiException
from minutes_api.models import Artifact, LiveAudioChunk, MeetingSession, User
from minutes_api.probe import ProbeError, probe_audio
from minutes_api.retention import current as current_retention
from minutes_api.security import now
from minutes_api.sessions_service import artifact_store

TRACK_ROLES = {"app-audio": "app", "microphone": "microphone"}
MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_CHUNK_DURATION_MS = 120_000


def begin_session(db: Session, owner: User, data: dict[str, Any]) -> MeetingSession:
    """録音開始時に、完全WAVの長さが未確定でも所有者付きsessionを確保する。"""
    session_id = data["session_id"]
    existing = db.get(MeetingSession, session_id)
    if existing is not None:
        if (
            existing.owner_id != owner.id
            or existing.deleted_at is not None
            or existing.finalized_at is not None
            or existing.package.get("live_recording") is not True
        ):
            raise ApiException(409, "conflict", "このセッション ID は使用できません")
        return existing
    title = (data.get("title") or "録音").strip()[:200]
    package = {
        "live_recording": True,
        "session_id": str(session_id),
        "input_kind": "recorded_dual_track",
        "started_at": data["started_at"].isoformat(),
        "title": title,
        "title_edited_by_user": bool(data.get("title_edited_by_user")),
        "language_mode": data["language_mode"],
        "allow_external_send": bool(data["allow_external_send"]),
        "format_profile_id": str(data["format_profile_id"]) if data.get("format_profile_id") else None,
        "source": data["source"],
        "tracks": [],
    }
    row = MeetingSession(
        id=session_id,
        owner_id=owner.id,
        title=title,
        title_edited_by_user=bool(data.get("title_edited_by_user")),
        title_revision=0,
        input_kind="recorded_dual_track",
        language_mode=data["language_mode"],
        allow_external_send=bool(data["allow_external_send"]),
        format_snapshot=formats_service.snapshot_for(db, owner, data.get("format_profile_id")),
        package=package,
        status="recording",
        started_at=data["started_at"],
        duration_ms=0,
    )
    db.add(row)
    audit.record(db, "live_session_started", actor_id=owner.id, target_type="session", target_id=session_id)
    db.flush()
    return row


def put_chunk(
    db: Session,
    session: MeetingSession,
    actor: User,
    *,
    track_id: str,
    sequence: int,
    start_offset_ms: int,
    duration_ms: int,
    sha256: str,
    body: bytes,
) -> LiveAudioChunk:
    """不変chunkを冪等保存し、同じsequenceの2trackが揃えば1jobを投入する。"""
    if (
        session.status != "recording"
        or session.finalized_at is not None
        or session.package.get("live_recording") is not True
    ):
        raise ApiException(409, "conflict", "このセッションは録音中ではありません")
    if track_id not in TRACK_ROLES:
        raise ApiException(400, "invalid_input", "先行文字起こしのtrack_idが不正です", stage="upload")
    if not 0 < duration_ms <= MAX_CHUNK_DURATION_MS or start_offset_ms < 0 or sequence < 0:
        raise ApiException(400, "invalid_input", "chunkの時刻またはsequenceが不正です", stage="upload")
    if not body or len(body) > MAX_CHUNK_BYTES:
        raise ApiException(400, "limit_exceeded", "chunkの大きさが上限を超えています", stage="upload")
    actual_sha = hashlib.sha256(body).hexdigest()
    if actual_sha != sha256:
        raise ApiException(400, "checksum_mismatch", "chunkのchecksumが一致しません", stage="upload")

    existing = db.execute(
        select(LiveAudioChunk).where(
            LiveAudioChunk.session_id == session.id,
            LiveAudioChunk.track_id == track_id,
            LiveAudioChunk.sequence == sequence,
        )
    ).scalars().first()
    if existing is not None:
        if (
            existing.sha256 != sha256
            or existing.byte_size != len(body)
            or existing.start_offset_ms != start_offset_ms
            or existing.duration_ms != duration_ms
        ):
            raise ApiException(409, "conflict", "同じsequenceに異なるchunkが登録されています", stage="upload")
        return existing

    store = artifact_store()
    stored = store.put_bytes(body)
    try:
        probe = probe_audio(store.path(stored.artifact_id))
        if probe.container != "wav" or probe.codec != "pcm_s16le":
            raise ApiException(400, "unsupported_format", "chunkはPCM16 WAVである必要があります", stage="validation")
        tolerance = max(200, int(duration_ms * 0.02))
        if abs(probe.duration_ms - duration_ms) > tolerance:
            raise ApiException(400, "invalid_input", "chunkの再生時間が申告値と一致しません", stage="validation")
    except ProbeError as exc:
        store.delete(stored.artifact_id)
        raise ApiException(400, exc.code, exc.message, stage="validation") from exc
    except Exception:
        store.delete(stored.artifact_id)
        raise

    db.add(
        Artifact(
            id=stored.artifact_id,
            session_id=session.id,
            kind="audio_chunk",
            track_id=track_id,
            role=TRACK_ROLES[track_id],
            content_type="audio/wav",
            byte_size=stored.byte_size,
            sha256=stored.sha256,
        )
    )
    row = LiveAudioChunk(
        session_id=session.id,
        track_id=track_id,
        role=TRACK_ROLES[track_id],
        sequence=sequence,
        start_offset_ms=start_offset_ms,
        duration_ms=duration_ms,
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        audio_artifact_id=stored.artifact_id,
        state="uploaded",
        expires_at=now() + timedelta(hours=current_retention(db).upload_hours),
    )
    db.add(row)
    db.flush()
    _enqueue_pair(db, session, sequence)
    audit.record(
        db,
        "live_chunk_uploaded",
        actor_id=actor.id,
        target_type="session",
        target_id=session.id,
        metadata={"track_id": track_id, "sequence": sequence},
    )
    db.flush()
    return row


def _enqueue_pair(db: Session, session: MeetingSession, sequence: int) -> None:
    rows = list(
        db.execute(
            select(LiveAudioChunk)
            .where(LiveAudioChunk.session_id == session.id, LiveAudioChunk.sequence == sequence)
            .order_by(LiveAudioChunk.track_id)
        ).scalars()
    )
    if {row.track_id for row in rows} != set(TRACK_ROLES) or any(row.job_id is not None for row in rows):
        return
    input_doc = {
        "artifacts": [
            {
                "artifact_id": row.audio_artifact_id,
                "kind": "audio_track",
                "track_id": row.track_id,
                "role": row.role,
                # 各chunkのworker出力はchunk先頭基準にし、最終統合時に録音全体の
                # offsetへ変換する。ここへ全体offsetを渡すとRTFの母数も膨らむ。
                "start_offset_ms": 0,
                "revision": None,
            }
            for row in rows
        ],
        "transcript_revision": None,
        "parent_minutes_version_id": None,
    }
    settings = {
        "input_kind": "recorded_dual_track",
        "language_mode": session.language_mode,
        "transcript_revision": 1,
        "max_audio_ms": get_settings().max_audio_ms,
        "requested_backend": None,
        "strategy": None,
        "live_chunk_sequence": sequence,
        "live_chunk_duration_ms": max(row.duration_ms for row in rows),
    }
    enqueued = jobs.enqueue(
        db,
        kind=JobKind.TRANSCRIPTION,
        session_id=session.id,
        owner_id=session.owner_id,
        input=input_doc,
        settings=settings,
        idempotency_key=f"live-transcription:{session.id}:chunk:{sequence}",
        timeout_seconds=get_settings().transcription_timeout_seconds,
    )
    for row in rows:
        row.job_id = enqueued.job_id
        row.state = "queued"


def rows_for_session(db: Session, session_id: uuid.UUID) -> list[LiveAudioChunk]:
    return list(
        db.execute(
            select(LiveAudioChunk)
            .where(LiveAudioChunk.session_id == session_id)
            .order_by(LiveAudioChunk.sequence, LiveAudioChunk.track_id)
        ).scalars()
    )


def topology_complete(session: MeetingSession, rows: list[LiveAudioChunk]) -> bool:
    """最終packageとchunk列が連続し、両trackで同じsequence集合ならtrue。"""
    if not rows or session.package.get("live_recording") is True:
        return False
    by_track = {track_id: [row for row in rows if row.track_id == track_id] for track_id in TRACK_ROLES}
    sequence_sets = [{row.sequence for row in track_rows} for track_rows in by_track.values()]
    if not sequence_sets[0] or sequence_sets[0] != sequence_sets[1]:
        return False
    expected = set(range(max(sequence_sets[0]) + 1))
    if sequence_sets[0] != expected:
        return False
    package_tracks = {track["track_id"]: track for track in session.package["tracks"]}
    for track_id, track_rows in by_track.items():
        if track_id not in package_tracks:
            return False
        ordered = sorted(track_rows, key=lambda row: row.sequence)
        if ordered[0].start_offset_ms != 0:
            return False
        for previous, current in zip(ordered, ordered[1:], strict=False):
            overlap = previous.start_offset_ms + previous.duration_ms - current.start_offset_ms
            if not 800 <= overlap <= 1_200:
                return False
        total = max(row.start_offset_ms + row.duration_ms for row in ordered)
        duration = int(package_tracks[track_id]["duration_ms"])
        if abs(total - duration) > max(500, int(duration * 0.02)):
            return False
    for sequence in sequence_sets[0]:
        pair = [row for row in rows if row.sequence == sequence]
        if max(row.start_offset_ms for row in pair) - min(row.start_offset_ms for row in pair) > 200:
            return False
    return True


def finalization_state(session: MeetingSession, rows: list[LiveAudioChunk]) -> Literal["absent", "waiting", "ready", "fallback"]:
    if not topology_complete(session, rows):
        return "absent"
    states = {row.state for row in rows}
    if states <= {"transcribed"}:
        return "ready"
    if states & {"failed", "cancelled"}:
        return "fallback"
    return "waiting"


def merged_transcript(db: Session, session: MeetingSession, rows: list[LiveAudioChunk]) -> TranscriptContract:
    """sequenceごとのTranscriptを完全WAVのartifact参照へ置換して1版へ結合する。"""
    if finalization_state(session, rows) != "ready":
        raise ValueError("先行文字起こしchunkが確定していません")
    store = artifact_store()
    documents: list[tuple[int, dict[str, Any]]] = []
    for sequence in sorted({row.sequence for row in rows}):
        artifact_id = next(
            (row.transcript_json_artifact_id for row in rows if row.sequence == sequence and row.transcript_json_artifact_id),
            None,
        )
        if artifact_id is None:
            raise ValueError(f"chunk {sequence} の文字起こし成果物がありません")
        try:
            with store.open(artifact_id) as handle:
                documents.append((sequence, json.load(handle)))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"chunk {sequence} の文字起こし成果物を読み込めません") from exc
    base = documents[0][1]
    package_tracks = {track["track_id"]: track for track in session.package["tracks"]}
    full_artifacts = {
        row.track_id: row
        for row in db.execute(
            select(Artifact).where(
                Artifact.session_id == session.id,
                Artifact.kind == "audio_track",
                Artifact.deleted_at.is_(None),
            )
        ).scalars()
    }
    segments: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    timing_keys = ("total_ms", "decode_audio_ms", "vad_ms", "language_detection_ms", "model_load_ms", "transcribe_ms", "review_retry_ms", "merge_ms")
    timings = {key: 0 for key in timing_keys}
    peak_memory = 0
    covered_until: dict[str, int] = {}
    for sequence, doc in documents:
        chunk_offsets = {
            row.track_id: row.start_offset_ms
            for row in rows
            if row.sequence == sequence
        }
        for segment in doc["segments"]:
            item = dict(segment)
            offset = int(package_tracks[item["track_id"]]["start_offset_ms"]) + chunk_offsets[item["track_id"]]
            item["id"] = f"chunk-{sequence}-{item['id']}"
            item["start_ms"] += offset
            item["end_ms"] += offset
            if item.get("replaced_by_review"):
                item["replaced_by_review"] = f"chunk-{sequence}-{item['replaced_by_review']}"
            if item.get("words"):
                item["words"] = [{**word, "start_ms": word["start_ms"] + offset, "end_ms": word["end_ms"] + offset} for word in item["words"]]
            cutoff = covered_until.get(item["track_id"], 0)
            duplicate_index = next(
                (
                    index
                    for index in range(len(segments) - 1, -1, -1)
                    if segments[index]["track_id"] == item["track_id"]
                    and segments[index]["start_ms"] < item["end_ms"]
                    and item["start_ms"] < segments[index]["end_ms"]
                    and _same_utterance(segments[index]["text"], item["text"])
                ),
                None,
            )
            if duplicate_index is not None:
                previous = segments[duplicate_index]
                if item["end_ms"] > previous["end_ms"] or len(item["text"]) > len(previous["text"]):
                    segments[duplicate_index] = item
            elif item["end_ms"] > cutoff:
                segments.append(item)
        for review in doc.get("review", []):
            item = dict(review)
            offset = int(package_tracks[item["track_id"]]["start_offset_ms"]) + chunk_offsets[item["track_id"]]
            item["id"] = f"chunk-{sequence}-{item['id']}"
            item["start_ms"] += offset
            item["end_ms"] += offset
            if item["end_ms"] > covered_until.get(item["track_id"], 0):
                reviews.append(item)
        for row in rows:
            if row.sequence == sequence:
                covered_until[row.track_id] = max(
                    covered_until.get(row.track_id, 0),
                    int(package_tracks[row.track_id]["start_offset_ms"]) + row.start_offset_ms + row.duration_ms,
                )
        current = doc["processing"]["timings"]
        for key in timing_keys:
            timings[key] += int(current.get(key) or 0)
        peak_memory = max(peak_memory, int(doc["processing"]["resources"].get("peak_memory_bytes") or 0))
    duration = int(session.duration_ms or 0)
    timings["audio_duration_ms"] = duration
    timings["rtf"] = (timings["transcribe_ms"] / duration) if duration else None
    processing = dict(base["processing"])
    processing["timings"] = timings
    processing["decode"] = {**(processing.get("decode") or {}), "measurement": "recording_time_chunks"}
    processing["resources"] = {**processing["resources"], "peak_memory_bytes": peak_memory or None}
    tracks = []
    for track_id in ("app-audio", "microphone"):
        source = full_artifacts.get(track_id)
        if source is None:
            raise ValueError(f"完全WAVのartifactがありません: {track_id}")
        package_track = package_tracks[track_id]
        sample = next(track for _, doc in documents for track in doc["tracks"] if track["track_id"] == track_id)
        tracks.append(
            {
                "track_id": track_id,
                "role": package_track["role"],
                "source_artifact_id": source.id,
                "start_offset_ms": package_track["start_offset_ms"],
                "duration_ms": package_track["duration_ms"],
                "normalization": sample.get("normalization"),
            }
        )
    document = {
        "schema_version": "transcript/1",
        "session_id": str(session.id),
        "revision": int(session.transcript_revision or 0) + 1,
        "input_kind": session.input_kind,
        "language_mode": session.language_mode,
        "tracks": tracks,
        "segments": sorted(segments, key=lambda item: (item["start_ms"], item["track_id"])),
        "review": sorted(reviews, key=lambda item: (item["start_ms"], item["track_id"])),
        "processing": processing,
    }
    return TranscriptContract.model_validate(document)


def _same_utterance(left: str, right: str) -> bool:
    """1秒の重複窓に現れた同じ発話を、表記揺れを許して判定する。"""
    normalized_left = re.sub(r"[\s。、，,.!?！？]+", "", left).casefold()
    normalized_right = re.sub(r"[\s。、，,.!?！？]+", "", right).casefold()
    if not normalized_left or not normalized_right:
        return False
    return normalized_left in normalized_right or normalized_right in normalized_left


def chunk_status(row: LiveAudioChunk) -> dict[str, Any]:
    return {
        "track_id": row.track_id,
        "sequence": row.sequence,
        "state": row.state,
        "job_id": str(row.job_id) if row.job_id else None,
        "duration_ms": row.duration_ms,
    }
