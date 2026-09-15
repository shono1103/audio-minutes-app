"""health (未認証・最小) と capabilities (認証)。"""

from __future__ import annotations

from datetime import timedelta

from audio_minutes_contracts import CONTRACT_VERSIONS
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from minutes_api import __version__, jobs
from minutes_api.config import get_settings
from minutes_api.db import db_dependency
from minutes_api.deps import Principal, current_principal
from minutes_api.models import ClaudeConnection
from minutes_api.security import now

router = APIRouter(prefix="/v1", tags=["health"])


def _worker_ready(row: dict, alive: bool) -> bool:
    """heartbeat だけではなく backend と固定モデルの準備完了を要求する。"""
    if not alive:
        return False
    if row.get("kind") != "transcription":
        return True
    backend = row.get("backend") if isinstance(row.get("backend"), dict) else {}
    models = row.get("models") if isinstance(row.get("models"), list) else []
    return backend.get("ready") is True and len(models) >= 2 and all(
        isinstance(model, dict) and model.get("ready") is True for model in models
    )


@router.get("/health", summary="到達性 (未認証)")
def health(db: Session = Depends(db_dependency)) -> dict:
    try:
        db.execute(text("SELECT 1"))
        status = "ok"
    except Exception:  # noqa: BLE001
        db.rollback()
        status = "degraded"
    return {"status": status, "version": __version__}


@router.get("/capabilities", summary="DB・worker・契約版・backend の状態")
def capabilities(principal: Principal = Depends(current_principal), db: Session = Depends(db_dependency)) -> dict:
    settings = get_settings()
    heartbeats = jobs.worker_heartbeats(db)
    cutoff = now() - timedelta(seconds=180)
    workers = []
    for row in heartbeats:
        alive = row["heartbeat_at"] >= cutoff
        workers.append(
            {
                "worker_id": row["worker_id"],
                "kind": row["kind"],
                "version": row["version"],
                "alive": alive,
                "ready": _worker_ready(row, alive),
                "heartbeat_at": row["heartbeat_at"].isoformat(),
                "backend": row.get("backend"),
                "models": row.get("models"),
                "claude_state": row.get("claude_state"),
            }
        )
    connection = db.get(ClaudeConnection, 1)
    return {
        "version": __version__,
        "contracts": CONTRACT_VERSIONS,
        "database": "ok",
        "workers": workers,
        "transcription_available": any(w["kind"] == "transcription" and w["ready"] for w in workers),
        "minutes_available": any(w["kind"] == "minutes" and w["ready"] for w in workers),
        "claude": {
            "state": connection.state if connection else "unknown",
            "connected_owner_is_me": bool(connection and connection.connected_owner_id == principal.id),
        },
        "limits": {"max_audio_ms": settings.max_audio_ms, "max_upload_bytes": settings.max_upload_bytes},
        "poll_interval_ms": settings.poll_interval_ms,
    }
