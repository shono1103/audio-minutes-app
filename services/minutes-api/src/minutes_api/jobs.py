"""API 側のジョブ操作。jobs テーブル (contracts/sql/jobs.sql) を SQLAlchemy の同じ transaction で扱う。
worker 側の claim/heartbeat/complete は audio_minutes_contracts.queue にある。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from audio_minutes_contracts.models import JobKind
from sqlalchemy import text
from sqlalchemy.orm import Session

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")


@dataclass(frozen=True)
class EnqueuedJob:
    """投入結果。`created` が False なら同じ idempotency_key の既存 job を返している。

    既存 job は終了済み (terminal) のこともあるため、呼び出し側は `created` を見て
    セッション状態を「待機中」へ進めるかどうかを決める。
    """

    job_id: uuid.UUID
    status: str
    created: bool

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def enqueue(
    db: Session,
    *,
    kind: JobKind,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    input: dict[str, Any],
    settings: dict[str, Any],
    idempotency_key: str,
    timeout_seconds: int,
    max_attempts: int = 3,
) -> EnqueuedJob:
    """同じ idempotency_key の再送は既存 job を返す (新規投入かどうかも返す)。

    `xmax = 0` は「この行を今回の INSERT が作った」ことを表す PostgreSQL の判定。
    UPDATE 側 (衝突) では 0 にならないため、再送と新規投入を区別できる。
    """
    row = db.execute(
        text(
            """
            INSERT INTO jobs (job_id, kind, session_id, owner_id, input, settings, idempotency_key, status,
                              timeout_seconds, max_attempts)
            VALUES (:job_id, :kind, :session_id, :owner_id, CAST(:input AS jsonb), CAST(:settings AS jsonb), :key,
                    'queued', :timeout, :max_attempts)
            ON CONFLICT (idempotency_key) DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
            RETURNING job_id, status, (xmax = 0) AS created
            """
        ),
        {
            "job_id": uuid.uuid4(),
            "kind": kind.value,
            "session_id": session_id,
            "owner_id": owner_id,
            "input": json.dumps(input, ensure_ascii=False),
            "settings": json.dumps(settings, ensure_ascii=False, default=str),
            "key": idempotency_key,
            "timeout": timeout_seconds,
            "max_attempts": max_attempts,
        },
    ).first()
    return EnqueuedJob(job_id=row[0], status=row[1], created=bool(row[2]))


def cancel_minutes_jobs_not_for_owner(db: Session, connected_owner_id: uuid.UUID | None) -> int:
    """接続 owner が変わった/ログアウトした時点で、合致しない議事録ジョブを取り消す。

    worker は投入時のスナップショット (connected_owner_id) しか見られないので、
    接続状態が変わった時点で API 側が queue を掃除する。`connected_owner_id` が None
    (ログアウト) なら待機中の議事録ジョブをすべて取り消す。実行中のものは
    cancel_requested を立てて worker の応答を待つ。
    """
    result = db.execute(
        text(
            """
            UPDATE jobs SET status = 'cancelled', finished_at = now(), revision = revision + 1
             WHERE kind = 'minutes_generation' AND status = 'queued'
               AND (CAST(:owner AS text) IS NULL
                    OR settings ->> 'connected_owner_id' IS DISTINCT FROM CAST(:owner AS text))
            """
        ),
        {"owner": str(connected_owner_id) if connected_owner_id else None},
    )
    db.execute(
        text(
            """
            UPDATE jobs SET cancel_requested = true, revision = revision + 1
             WHERE kind = 'minutes_generation' AND status IN ('leased', 'running')
               AND (CAST(:owner AS text) IS NULL
                    OR settings ->> 'connected_owner_id' IS DISTINCT FROM CAST(:owner AS text))
            """
        ),
        {"owner": str(connected_owner_id) if connected_owner_id else None},
    )
    return result.rowcount or 0


def cancel_all_for_session(db: Session, session_id: uuid.UUID) -> int:
    """セッション削除時に全世代の active job を同じ transaction で停止する。"""
    queued = db.execute(
        text(
            """
            UPDATE jobs
               SET status = 'cancelled', cancel_requested = true, finished_at = now(),
                   lease_owner = NULL, lease_expires_at = NULL, revision = revision + 1
             WHERE session_id = :session_id AND status = 'queued'
            """
        ),
        {"session_id": session_id},
    )
    running = db.execute(
        text(
            """
            UPDATE jobs SET cancel_requested = true, revision = revision + 1
             WHERE session_id = :session_id
               AND status IN ('leased', 'running') AND cancel_requested = false
            """
        ),
        {"session_id": session_id},
    )
    return (queued.rowcount or 0) + (running.rowcount or 0)


def get(db: Session, job_id: uuid.UUID) -> dict[str, Any] | None:
    row = db.execute(
        text(
            "SELECT job_id, kind, session_id, owner_id, input, settings, idempotency_key, status, attempt, max_attempts,"
            " timeout_seconds, cancel_requested, lease_owner, lease_expires_at, heartbeat_at, available_at,"
            " external_dispatch_started_at, result,"
            " failure, revision, created_at, started_at, finished_at FROM jobs WHERE job_id = :job_id"
        ),
        {"job_id": job_id},
    ).mappings().first()
    return dict(row) if row else None


def list_for_session(db: Session, session_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            "SELECT job_id, kind, status, attempt, max_attempts, cancel_requested, failure, created_at, started_at,"
            " finished_at FROM jobs WHERE session_id = :session_id ORDER BY created_at"
        ),
        {"session_id": session_id},
    ).mappings()
    return [dict(row) for row in rows]


def request_cancel(db: Session, session_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, Any] | None:
    """指定セッションの job へ冪等に取消要求を反映する。

    queued はその場で cancelled、実行中は cancel_requested を立てて worker の応答を
    待つ。terminal job の再送は状態を変えず同じ行を返す。session_id も更新条件へ
    含め、別セッションの job ID 差し替えを許可しない。
    """
    db.execute(
        text(
            "UPDATE jobs SET status = 'cancelled', cancel_requested = true, finished_at = now(),"
            " revision = revision + 1"
            " WHERE job_id = :job_id AND session_id = :session_id AND status = 'queued'"
        ),
        {"job_id": job_id, "session_id": session_id},
    )
    db.execute(
        text(
            "UPDATE jobs SET cancel_requested = true, revision = revision + 1"
            " WHERE job_id = :job_id AND session_id = :session_id"
            " AND status IN ('leased', 'running') AND cancel_requested = false"
        ),
        {"job_id": job_id, "session_id": session_id},
    )
    row = get(db, job_id)
    if row is None or row["session_id"] != session_id:
        return None
    return row


def worker_heartbeats(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        text("SELECT worker_id, kind, version, backend, models, claude_state, heartbeat_at FROM worker_heartbeats")
    ).mappings()
    return [dict(row) for row in rows]
