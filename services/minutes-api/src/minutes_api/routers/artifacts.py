"""成果物: 音声 (Range)、文字起こし、現在版議事録。所有者と共有先が閲覧できる。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from minutes_api import minutes_service, sessions_service
from minutes_api.db import db_dependency
from minutes_api.deps import SessionAccess, session_access
from minutes_api.errors import ApiException, not_found
from minutes_api.models import Artifact

router = APIRouter(prefix="/v1/sessions/{session_id}", tags=["artifacts"])
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_CHUNK = 1024 * 1024


def _iter_file(path, start: int, end: int) -> Iterator[bytes]:
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = handle.read(min(_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@router.get("/audio/{track_id}", summary="音声 (Range 対応)")
def get_audio(track_id: str, request: Request, access: SessionAccess = Depends(session_access), db: Session = Depends(db_dependency)):
    artifact = db.execute(
        select(Artifact).where(
            Artifact.session_id == access.session.id, Artifact.kind == "audio_track", Artifact.track_id == track_id, Artifact.deleted_at.is_(None)
        )
    ).scalars().first()
    if artifact is None:
        if not access.session.audio_retained:
            raise ApiException(410, "audio_deleted", "保持期限により音声は削除されています", stage="storage", retryable=False)
        raise not_found("音声が見つかりません")
    path = sessions_service.artifact_store().path(artifact.id)
    if not path.is_file():
        raise not_found("音声が見つかりません")
    size = artifact.byte_size
    headers = {"Accept-Ranges": "bytes", "Content-Type": artifact.content_type, "Cache-Control": "private, no-store"}
    range_header = request.headers.get("range")
    if range_header:
        match = _RANGE_RE.match(range_header.strip())
        if not match:
            raise ApiException(416, "invalid_request", "Range の形式が不正です", headers={"Content-Range": f"bytes */{size}"})
        start_text, end_text = match.groups()
        if start_text == "" and end_text == "":
            raise ApiException(416, "invalid_request", "Range の形式が不正です", headers={"Content-Range": f"bytes */{size}"})
        if start_text == "":
            length = min(int(end_text), size)
            start, end = size - length, size - 1
        else:
            start = int(start_text)
            end = min(int(end_text), size - 1) if end_text else size - 1
        if start >= size or start > end:
            raise ApiException(416, "invalid_request", "Range が範囲外です", headers={"Content-Range": f"bytes */{size}"})
        headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end - start + 1)})
        return StreamingResponse(_iter_file(path, start, end), status_code=206, headers=headers)
    headers["Content-Length"] = str(size)
    return StreamingResponse(_iter_file(path, 0, size - 1), status_code=200, headers=headers)


@router.get("/transcript", summary="文字起こし (transcript.v1)")
def get_transcript(access: SessionAccess = Depends(session_access), db: Session = Depends(db_dependency)) -> Response:
    transcript = minutes_service.transcript_for(db, access.session)
    with sessions_service.artifact_store().open(transcript.json_artifact_id) as handle:
        data = handle.read()
    return Response(content=data, media_type="application/json", headers={"Cache-Control": "private, no-store"})


@router.get("/transcript.md", summary="文字起こし (Markdown)")
def get_transcript_markdown(access: SessionAccess = Depends(session_access), db: Session = Depends(db_dependency)) -> Response:
    transcript = minutes_service.transcript_for(db, access.session)
    store = sessions_service.artifact_store()
    if transcript.md_artifact_id and store.exists(transcript.md_artifact_id):
        with store.open(transcript.md_artifact_id) as handle:
            body = handle.read().decode("utf-8")
    else:
        from audio_minutes_contracts.models import Transcript as TranscriptContract

        with store.open(transcript.json_artifact_id) as handle:
            body = TranscriptContract.model_validate(json.loads(handle.read())).to_markdown()
    return PlainTextResponse(body, media_type="text/markdown; charset=utf-8", headers={"Cache-Control": "private, no-store"})


@router.get("/minutes", summary="現在版の議事録 (メタ + Markdown)")
def get_current_minutes(access: SessionAccess = Depends(session_access), db: Session = Depends(db_dependency)) -> JSONResponse:
    version = sessions_service.current_minutes(db, access.session)
    if version is None:
        raise not_found("議事録がまだありません")
    document = minutes_service.to_contract(version)
    if not access.is_owner:
        # 共有先には版履歴に関わる情報 (指示・親版・候補) を出さない
        document = {
            key: document[key]
            for key in ("schema_version", "version_id", "session_id", "version_number", "kind", "created_at", "artifact_id")
        }
    return JSONResponse({"version": document, "body_markdown": minutes_service.read_body(version)}, headers={"Cache-Control": "private, no-store"})
