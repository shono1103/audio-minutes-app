"""tus 1.0 (Core / Creation / Expiration / Termination) を API 内で実装する (ADR-0002)。
全操作でセッション所有者を検証し、offset はサーバー DB が正。

PATCH は DB transaction とストリーム受信の責務を分ける (R04)。同期 SQLAlchemy の呼び出しは
すべて threadpool へ移し、event loop を塞がない。追送の排他は行ロックの待ち合わせではなく
uploads.write_claim_* による短い transaction で取り、競合する 2 本目は待たずに 409 を返す。
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from minutes_api import audit, sessions_service
from minutes_api.config import get_settings
from minutes_api.db import db_dependency
from minutes_api.deps import Principal, SessionAccess, current_principal, load_session, session_owner_access
from minutes_api.errors import ApiException, not_found
from minutes_api.models import Upload
from minutes_api.security import now

router = APIRouter(prefix="/v1", tags=["uploads"])

TUS_VERSION = "1.0.0"
TUS_EXTENSIONS = "creation,expiration,termination"
_CHUNK = 1024 * 1024
# claim の有効期間。プロセス異常終了で残った claim はこの期間を過ぎたら引き継ぐ。
# 受信中は書き込みのたびに延長するので、通常の長時間 upload では期限切れにならない。
_CLAIM_SECONDS = 60
_WRITE_BUFFER = 4 * _CHUNK


def _tus_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Tus-Resumable": TUS_VERSION, "Cache-Control": "no-store"}
    if extra:
        headers.update(extra)
    return headers


def _http_date(value: datetime) -> str:
    """PostgreSQLのUTC tzinfoも、HTTP-dateが要求するdatetime.UTCへ正規化する。"""
    return format_datetime(value.astimezone(UTC), usegmt=True)


def _require_tus(request: Request) -> None:
    if request.headers.get("tus-resumable") != TUS_VERSION:
        raise ApiException(412, "invalid_request", "Tus-Resumable: 1.0.0 が必要です", headers=_tus_headers())


def _parse_metadata(header: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not header:
        return result
    for pair in header.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, value = pair.partition(" ")
        if value:
            try:
                result[key] = base64.b64decode(value, validate=True).decode("utf-8")
            except Exception as exc:  # noqa: BLE001
                raise ApiException(400, "invalid_request", "Upload-Metadata の形式が不正です") from exc
        else:
            result[key] = ""
    return result


def _load_upload(db: Session, upload_id: uuid.UUID, principal: Principal, *, lock: bool = False) -> Upload:
    statement = select(Upload).where(Upload.id == upload_id)
    if lock:
        statement = statement.with_for_update()
    upload = db.execute(statement).scalars().first()
    if upload is None:
        raise not_found("upload が見つかりません")
    session = load_session(db, upload.session_id)
    if session is None or session.owner_id != principal.id:
        raise not_found("upload が見つかりません")
    return upload


def _expired(upload: Upload) -> bool:
    return upload.state == "expired" or (upload.state in ("pending", "uploading") and upload.expires_at <= now())


@router.options("/uploads", include_in_schema=True, summary="tus 能力")
def options_uploads() -> Response:
    return Response(status_code=204, headers=_tus_headers({"Tus-Version": TUS_VERSION, "Tus-Extension": TUS_EXTENSIONS, "Tus-Max-Size": str(get_settings().max_upload_bytes)}))


@router.post("/sessions/{session_id}/uploads", status_code=201, summary="tus Creation: セッションのトラックに対応する upload を返す")
def create_upload(request: Request, access: SessionAccess = Depends(session_owner_access), db: Session = Depends(db_dependency)) -> Response:
    _require_tus(request)
    metadata = _parse_metadata(request.headers.get("upload-metadata"))
    track_id = metadata.get("track_id")
    if not track_id:
        raise ApiException(400, "invalid_request", "Upload-Metadata に track_id が必要です")
    length_header = request.headers.get("upload-length")
    if length_header is None or not length_header.isdigit():
        raise ApiException(400, "invalid_request", "Upload-Length が必要です")
    upload = db.execute(
        select(Upload).where(Upload.session_id == access.session.id, Upload.track_id == track_id)
    ).scalars().first()
    if upload is None:
        raise not_found("このセッションに該当するトラックはありません")
    if upload.state == "completed":
        raise ApiException(409, "conflict", "このトラックは既に完了しています")
    if int(length_header) != upload.expected_length:
        raise ApiException(400, "invalid_input", "Upload-Length が recording-package の byte_size と一致しません")
    if _expired(upload):
        upload.state = "expired"
        raise ApiException(410, "upload_expired", "upload の期限が切れています", stage="upload")
    if upload.state == "cancelled":
        raise ApiException(409, "conflict", "この upload は取り消されています")
    location = f"{get_settings().public_base_url}/v1/uploads/{upload.id}"
    return Response(
        status_code=201,
        headers=_tus_headers(
            {"Location": location, "Upload-Offset": str(upload.offset), "Upload-Expires": _http_date(upload.expires_at)}
        ),
    )


@router.head("/uploads/{upload_id}", summary="tus Core: 受信済み offset")
def head_upload(upload_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> Response:
    _require_tus(request)
    upload = _load_upload(db, upload_id, principal)
    if _expired(upload):
        upload.state = "expired"
        raise ApiException(410, "upload_expired", "upload の期限が切れています", stage="upload", headers=_tus_headers())
    if upload.state == "cancelled":
        raise not_found("upload が見つかりません")
    return Response(
        status_code=200,
        headers=_tus_headers(
            {"Upload-Offset": str(upload.offset), "Upload-Length": str(upload.expected_length), "Upload-Expires": _http_date(upload.expires_at)}
        ),
    )


class _Claim:
    """PATCH 1 回分の書き込み権。ストリーム受信中は DB transaction を持たない。"""

    __slots__ = ("claim_id", "upload_id", "start_offset", "expected_length", "expires_at")

    def __init__(self, claim_id: uuid.UUID, upload_id: uuid.UUID, start_offset: int, expected_length: int, expires_at) -> None:
        self.claim_id = claim_id
        self.upload_id = upload_id
        self.start_offset = start_offset
        self.expected_length = expected_length
        self.expires_at = expires_at


def _acquire_claim(db: Session, upload_id: uuid.UUID, principal: Principal, requested_offset: int) -> _Claim:
    """検証と claim 取得だけを行い、commit して行ロックを手放す。"""
    upload = _load_upload(db, upload_id, principal, lock=True)
    if _expired(upload):
        upload.state = "expired"
        db.commit()
        raise ApiException(410, "upload_expired", "upload の期限が切れています", stage="upload", headers=_tus_headers())
    if upload.state == "cancelled":
        raise not_found("upload が見つかりません")
    if upload.state == "completed":
        raise ApiException(409, "conflict", "この upload は完了しています", headers=_tus_headers({"Upload-Offset": str(upload.offset)}))
    held = upload.write_claim_id is not None and upload.write_claim_expires_at is not None and upload.write_claim_expires_at > now()
    if held:
        # 競合する 2 本目は待たずに拒否する。待つと受信中の 1 本目も終われない
        raise ApiException(
            409, "conflict", "この upload は追送中です", headers=_tus_headers({"Upload-Offset": str(upload.offset)})
        )
    if requested_offset != upload.offset:
        raise ApiException(409, "conflict", "Upload-Offset がサーバーの受信済み位置と一致しません", headers=_tus_headers({"Upload-Offset": str(upload.offset)}))
    claim_id = uuid.uuid4()
    upload.write_claim_id = claim_id
    upload.write_claim_expires_at = now() + timedelta(seconds=_CLAIM_SECONDS)
    upload.state = "uploading"
    db.commit()
    return _Claim(claim_id, upload.id, upload.offset, upload.expected_length, upload.expires_at)


def _reload_upload(db: Session, upload_id: uuid.UUID, *, lock: bool) -> Upload | None:
    """DB の現在値で読み直す。

    同じ Session 内に前の transaction で読んだ Upload が残っていると、claim を
    他のリクエストへ奪われても古い値のままになる。populate_existing で必ず上書きする。
    """
    statement = select(Upload).where(Upload.id == upload_id)
    if lock:
        statement = statement.with_for_update()
    return db.execute(statement.execution_options(populate_existing=True)).scalars().first()


def _extend_claim(db: Session, claim: _Claim) -> None:
    """受信中に claim の期限だけを延ばす。offset は触らない。"""
    upload = _reload_upload(db, claim.upload_id, lock=False)
    if upload is None or upload.write_claim_id != claim.claim_id:
        db.rollback()
        return
    upload.write_claim_expires_at = now() + timedelta(seconds=_CLAIM_SECONDS)
    db.commit()


def _release_claim(db: Session, claim: _Claim, written: int, principal: Principal) -> Upload:
    """受信結果を反映して claim を解放する。claim を失っていれば書き込みを破棄する。"""
    upload = _reload_upload(db, claim.upload_id, lock=True)
    if upload is None:
        raise not_found("upload が見つかりません")
    if upload.write_claim_id != claim.claim_id:
        # 期限切れで他の PATCH が引き継いだ。DB の offset を正とし、この書き込みは捨てる
        db.commit()
        raise ApiException(409, "conflict", "追送の書き込み権を失いました", headers=_tus_headers({"Upload-Offset": str(upload.offset)}))
    upload.write_claim_id = None
    upload.write_claim_expires_at = None
    if upload.state not in ("cancelled", "expired"):
        upload.offset = claim.start_offset + written
        upload.state = "completed" if upload.offset == upload.expected_length else "uploading"
        if upload.state == "completed":
            audit.record(db, "upload_completed", actor_id=principal.id, target_type="upload", target_id=upload.id)
    db.commit()
    return upload


def _abandon_claim(db: Session, claim: _Claim) -> None:
    """切断・上限超過などで受信を中断したときに claim だけを解放する。

    offset は進めない。次の PATCH は DB の offset を正としてファイルを切り詰める。
    """
    try:
        upload = _reload_upload(db, claim.upload_id, lock=True)
        if upload is not None and upload.write_claim_id == claim.claim_id:
            upload.write_claim_id = None
            upload.write_claim_expires_at = None
        db.commit()
    except Exception:  # noqa: BLE001 - 解放に失敗しても期限切れで引き継げる
        db.rollback()


@router.patch("/uploads/{upload_id}", summary="tus Core: 追送")
async def patch_upload(upload_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> Response:
    _require_tus(request)
    if request.headers.get("content-type") != "application/offset+octet-stream":
        raise ApiException(415, "invalid_request", "Content-Type は application/offset+octet-stream にしてください", headers=_tus_headers())
    offset_header = request.headers.get("upload-offset")
    if offset_header is None or not offset_header.isdigit():
        raise ApiException(400, "invalid_request", "Upload-Offset が必要です", headers=_tus_headers())

    # db (Session) は thread をまたぐが、run_in_threadpool を必ず await してから次へ進むため
    # 同時アクセスは発生しない。以降で db を直接呼ばないこと。
    claim = await run_in_threadpool(_acquire_claim, db, upload_id, principal, int(offset_header))
    part = sessions_service.upload_part_path(claim.upload_id)
    written = 0
    over_limit = False
    try:
        handle = await run_in_threadpool(part.open, "ab")
        try:
            if await run_in_threadpool(handle.tell) != claim.start_offset:
                # ファイルと DB がずれていれば DB を正としてファイルを切り詰める
                await run_in_threadpool(handle.truncate, claim.start_offset)
                await run_in_threadpool(handle.seek, claim.start_offset)
            # ASGI の chunk ごとに thread へ渡すと往復が多すぎるので、_WRITE_BUFFER まで
            # 貯めてからまとめて書く。上限超過時は buffer ごと捨てて切り詰める。
            buffer = bytearray()
            async for chunk in request.stream():
                if not chunk:
                    continue
                if claim.start_offset + written + len(chunk) > claim.expected_length:
                    buffer.clear()
                    await run_in_threadpool(handle.truncate, claim.start_offset)
                    over_limit = True
                    break
                buffer += chunk
                written += len(chunk)
                if len(buffer) >= _WRITE_BUFFER:
                    payload = bytes(buffer)
                    buffer.clear()
                    await run_in_threadpool(handle.write, payload)
                    await run_in_threadpool(_extend_claim, db, claim)
            if buffer:
                await run_in_threadpool(handle.write, bytes(buffer))
            await run_in_threadpool(handle.flush)
        finally:
            await run_in_threadpool(handle.close)
    except Exception:
        await run_in_threadpool(_abandon_claim, db, claim)
        raise
    if over_limit:
        await run_in_threadpool(_abandon_claim, db, claim)
        raise ApiException(400, "limit_exceeded", "Upload-Length を超えるデータです", headers=_tus_headers())

    upload = await run_in_threadpool(_release_claim, db, claim, written, principal)
    return Response(status_code=204, headers=_tus_headers({"Upload-Offset": str(upload.offset), "Upload-Expires": _http_date(upload.expires_at)}))


@router.delete("/uploads/{upload_id}", status_code=204, summary="tus Termination: 取消")
def delete_upload(upload_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> Response:
    _require_tus(request)
    upload = _load_upload(db, upload_id, principal, lock=True)
    if upload.state == "completed":
        raise ApiException(409, "conflict", "完了した upload は取り消せません", headers=_tus_headers())
    upload.state = "cancelled"
    sessions_service.upload_part_path(upload.id).unlink(missing_ok=True)
    audit.record(db, "upload_cancelled", actor_id=principal.id, target_type="upload", target_id=upload.id)
    return Response(status_code=204, headers=_tus_headers())
