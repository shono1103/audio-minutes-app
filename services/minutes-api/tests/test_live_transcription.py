from __future__ import annotations

import hashlib
import json
import struct
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from conftest import auth_headers, issue_access_token, make_user

from minutes_api import jobs, live_transcription, models, sessions_service


def _wav(duration_ms: int = 1000) -> bytes:
    data = b"\0\0" * (16_000 * duration_ms // 1000)
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 16_000, 32_000, 2, 16)
        + b"data"
        + struct.pack("<I", len(data))
    )
    return header + data


def _start(client, token: str, session_id: uuid.UUID) -> dict:
    response = client.post(
        "/v1/sessions/live",
        headers=auth_headers(token),
        json={
            "session_id": str(session_id),
            "started_at": datetime.now(UTC).isoformat(),
            "title": "先行文字起こし試験",
            "title_edited_by_user": True,
            "language_mode": "ja",
            "allow_external_send": False,
            "format_profile_id": None,
            "source": {"kind": "app", "app_name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        },
    )
    assert response.status_code == 201
    return response.json()


def test_live_chunks_are_idempotent_and_pair_enqueues_one_job(client, db, monkeypatch) -> None:
    owner = make_user(db)
    token = issue_access_token(db, owner)
    session_id = uuid.uuid4()
    started = _start(client, token, session_id)
    assert started["status"] == "recording"

    payload = _wav()
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        live_transcription,
        "probe_audio",
        lambda _path: SimpleNamespace(container="wav", codec="pcm_s16le", duration_ms=1000),
    )
    enqueued: list[dict] = []

    def fake_enqueue(_db, **kwargs):
        enqueued.append(kwargs)
        return jobs.EnqueuedJob(job_id=uuid.uuid4(), status="queued", created=True)

    monkeypatch.setattr(live_transcription.jobs, "enqueue", fake_enqueue)
    query = f"?start_offset_ms=0&duration_ms=1000&sha256={digest}"
    app = client.put(
        f"/v1/sessions/{session_id}/live-chunks/app-audio/0{query}",
        headers={**auth_headers(token), "Content-Type": "audio/wav"},
        content=payload,
    )
    assert app.status_code == 200
    assert enqueued == []

    duplicate = client.put(
        f"/v1/sessions/{session_id}/live-chunks/app-audio/0{query}",
        headers={**auth_headers(token), "Content-Type": "audio/wav"},
        content=payload,
    )
    assert duplicate.status_code == 200

    microphone = client.put(
        f"/v1/sessions/{session_id}/live-chunks/microphone/0{query}",
        headers={**auth_headers(token), "Content-Type": "audio/wav"},
        content=payload,
    )
    assert microphone.status_code == 200
    assert len(enqueued) == 1
    assert {item["track_id"] for item in enqueued[0]["input"]["artifacts"]} == {"app-audio", "microphone"}
    assert enqueued[0]["settings"]["live_chunk_sequence"] == 0

    progress = client.get(f"/v1/sessions/{session_id}/live-chunks", headers=auth_headers(token))
    assert progress.status_code == 200
    assert progress.json()["uploaded"] == 2


def test_complete_package_promotes_live_session_and_creates_tus_uploads(client, db) -> None:
    owner = make_user(db)
    token = issue_access_token(db, owner)
    session_id = uuid.uuid4()
    started_at = datetime.now(UTC)
    _start(client, token, session_id)
    payload = _wav()
    digest = hashlib.sha256(payload).hexdigest()
    response = client.post(
        "/v1/sessions",
        headers=auth_headers(token),
        json={
            "schema_version": "recording-package/2",
            "session_id": str(session_id),
            "input_kind": "recorded_dual_track",
            "time_base": {"unit": "ms", "origin": "recording_start"},
            "started_at": started_at.isoformat(),
            "title": "先行文字起こし試験",
            "title_edited_by_user": True,
            "language_mode": "ja",
            "allow_external_send": False,
            "format_profile_id": None,
            "tracks": [
                {
                    "track_id": track_id,
                    "role": role,
                    "start_offset_ms": 0,
                    "container": "wav",
                    "codec": "pcm_s16le",
                    "sample_rate": 16000,
                    "channels": 1,
                    "duration_ms": 1000,
                    "byte_size": len(payload),
                    "sha256": digest,
                }
                for track_id, role in (("app-audio", "app"), ("microphone", "microphone"))
            ],
            "source": {"kind": "app", "app_name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "uploading"
    assert {track["track_id"] for track in body["tracks"]} == {"app-audio", "microphone"}
    assert all(track["upload_state"] == "pending" for track in body["tracks"])


def _transcript_document(session_id: uuid.UUID, app_artifact_id: str, microphone_artifact_id: str, text: str) -> dict:
    return {
        "schema_version": "transcript/1",
        "session_id": str(session_id),
        "revision": 1,
        "input_kind": "recorded_dual_track",
        "language_mode": "ja",
        "tracks": [
            {"track_id": "app-audio", "role": "app", "source_artifact_id": app_artifact_id,
             "start_offset_ms": 0, "duration_ms": 30_000},
            {"track_id": "microphone", "role": "microphone", "source_artifact_id": microphone_artifact_id,
             "start_offset_ms": 0, "duration_ms": 30_000},
        ],
        "segments": [
            {"id": "seg-1", "track_id": "app-audio", "source": "app", "start_ms": 1_000,
             "end_ms": 2_000, "text": text, "language": "ja", "model_id": "model",
             "model_revision": "revision", "engine": "faster-whisper", "engine_version": "1",
             "strategy": "fixed_ja"}
        ],
        "review": [],
        "processing": {
            "engine": "faster-whisper", "engine_version": "1", "strategy": "fixed_ja",
            "models": [{"profile": "ja", "model_id": "model", "model_revision": "revision"}],
            "backend": {"requested_backend": "cpu", "effective_backend": "cpu", "gpu_verified": False},
            "resources": {"cpu_arch": "arm64", "cpu_count": 4},
            "timings": {"total_ms": 100, "transcribe_ms": 80},
        },
    }


def test_merged_transcript_offsets_each_chunk_and_uses_full_audio(db, settings) -> None:
    owner = make_user(db)
    session = models.MeetingSession(
        id=uuid.uuid4(), owner_id=owner.id, title="merge", input_kind="recorded_dual_track",
        language_mode="ja", status="transcribing", duration_ms=31_000, finalized_at=datetime.now(UTC),
        package={
            "live_recording": False,
            "tracks": [
                {"track_id": "app-audio", "role": "app", "start_offset_ms": 5, "duration_ms": 31_000},
                {"track_id": "microphone", "role": "microphone", "start_offset_ms": 12, "duration_ms": 31_000},
            ],
        },
    )
    db.add(session)
    db.flush()
    store = sessions_service.artifact_store(settings)
    full_ids: dict[str, str] = {}
    for track_id, role in (("app-audio", "app"), ("microphone", "microphone")):
        stored = store.put_bytes(b"full-" + track_id.encode())
        full_ids[track_id] = stored.artifact_id
        db.add(models.Artifact(
            id=stored.artifact_id, session_id=session.id, kind="audio_track", track_id=track_id, role=role,
            content_type="audio/wav", byte_size=stored.byte_size, sha256=stored.sha256,
        ))

    rows: list[models.LiveAudioChunk] = []
    for sequence in range(2):
        chunk_ids: dict[str, str] = {}
        sequence_rows: list[models.LiveAudioChunk] = []
        for track_id, role in (("app-audio", "app"), ("microphone", "microphone")):
            stored = store.put_bytes(f"chunk-{sequence}-{track_id}".encode())
            chunk_ids[track_id] = stored.artifact_id
            db.add(models.Artifact(
                id=stored.artifact_id, session_id=session.id, kind="audio_chunk", track_id=track_id, role=role,
                content_type="audio/wav", byte_size=stored.byte_size, sha256=stored.sha256,
            ))
            sequence_rows.append(models.LiveAudioChunk(
                session_id=session.id, track_id=track_id, role=role, sequence=sequence,
                start_offset_ms=sequence * 29_000, duration_ms=30_000 if sequence == 0 else 2_000,
                byte_size=stored.byte_size,
                sha256=stored.sha256, audio_artifact_id=stored.artifact_id, state="transcribed",
                expires_at=datetime.now(UTC),
            ))
        document = _transcript_document(
            session.id, chunk_ids["app-audio"], chunk_ids["microphone"], "境界の発話"
        )
        document["segments"][0]["start_ms"] = 29_500 if sequence == 0 else 500
        document["segments"][0]["end_ms"] = 30_000 if sequence == 0 else 1_500
        if sequence == 1:
            document["segments"].append({
                **document["segments"][0], "id": "seg-2", "start_ms": 1_500, "end_ms": 1_900,
                "text": "後続の発話",
            })
        transcript = store.put_bytes(json.dumps(document).encode())
        db.add(models.Artifact(
            id=transcript.artifact_id, session_id=session.id, kind="transcript_json",
            content_type="application/json", byte_size=transcript.byte_size, sha256=transcript.sha256,
        ))
        for row in sequence_rows:
            row.transcript_json_artifact_id = transcript.artifact_id
        rows.extend(sequence_rows)
        db.add_all(sequence_rows)
    db.flush()

    merged = live_transcription.merged_transcript(db, session, rows)

    assert [(item.text, item.start_ms, item.end_ms) for item in merged.segments] == [
        ("境界の発話", 29_505, 30_505),
        ("後続の発話", 30_505, 30_905),
    ]
    assert {track.track_id: track.source_artifact_id for track in merged.tracks} == full_ids
