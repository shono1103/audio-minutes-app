"""PostgreSQL の jobs テーブルを使う永続ジョブキュー (ADR-0003)。

worker は am_worker ロールで接続し、この module の操作だけを使う。
lease は短い transaction で取得し、推論中に DB ロックを保持しない。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from audio_minutes_contracts.models import JobFailure, JobKind, WorkerResult


@dataclass(frozen=True)
class ClaimedJob:
    job_id: uuid.UUID
    kind: JobKind
    session_id: uuid.UUID
    owner_id: uuid.UUID
    input: dict[str, Any]
    settings: dict[str, Any]
    attempt: int
    max_attempts: int
    timeout_seconds: int
    lease_expires_at: datetime
    revision: int


class JobQueue(Protocol):
    def claim(self, kinds: Sequence[str], worker_id: str, lease_seconds: int) -> ClaimedJob | None: ...

    def heartbeat(self, job: ClaimedJob, worker_id: str, lease_seconds: int) -> bool: ...

    def mark_running(self, job: ClaimedJob, worker_id: str) -> bool: ...

    def complete(self, job: ClaimedJob, worker_id: str, result: WorkerResult) -> bool: ...

    def fail(self, job: ClaimedJob, worker_id: str, failure: JobFailure, backoff_seconds: int = 0) -> bool: ...

    def cancel_requested(self, job: ClaimedJob) -> bool: ...

    def progress(self, job: ClaimedJob, worker_id: str, detail: dict[str, Any]) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PostgresJobQueue:
    def __init__(self, dsn: str, *, connect_timeout: int = 10) -> None:
        self._dsn = dsn
        self._connect_timeout = connect_timeout

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=self._connect_timeout)

    # --- worker 側操作 -------------------------------------------------------

    def claim(self, kinds: Sequence[str], worker_id: str, lease_seconds: int) -> ClaimedJob | None:
        """queued、または lease 切れの leased/running を 1 件取得して lease する。"""
        with self._connect() as conn, conn.transaction():
            row = conn.execute(
                """
                SELECT job_id FROM jobs
                WHERE kind = ANY(%(kinds)s)
                  AND cancel_requested = false
                  AND available_at <= now()
                  AND (
                        status = 'queued'
                     OR (status IN ('leased', 'running') AND lease_expires_at < now())
                  )
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                {"kinds": list(kinds)},
            ).fetchone()
            if row is None:
                return None
            job_id = row["job_id"]
            current = conn.execute("SELECT attempt, max_attempts, status FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
            if current["status"] in ("leased", "running") and current["attempt"] >= current["max_attempts"]:
                # lease 切れで再取得したが attempt 上限。失敗として確定する
                conn.execute(
                    """
                    UPDATE jobs SET status = 'failed', revision = revision + 1, finished_at = now(),
                        failure = %(failure)s, lease_owner = NULL, lease_expires_at = NULL
                    WHERE job_id = %(job_id)s
                    """,
                    {
                        "job_id": job_id,
                        "failure": Jsonb(
                            {
                                "code": "timeout",
                                "stage": "transcription" if current else "storage",
                                "retryable": False,
                                "message": "lease が切れ attempt 上限に達しました",
                                "retained_artifacts": [],
                            }
                        ),
                    },
                )
                conn.execute(
                    "INSERT INTO job_events (job_id, attempt, worker_id, event, detail) VALUES (%s, %s, %s, 'lease_lost', %s)",
                    (job_id, current["attempt"], worker_id, Jsonb({"reason": "max_attempts"})),
                )
                return None
            updated = conn.execute(
                """
                UPDATE jobs
                SET status = 'leased', attempt = attempt + 1, lease_owner = %(worker)s,
                    lease_expires_at = now() + make_interval(secs => %(lease)s), heartbeat_at = now(),
                    started_at = COALESCE(started_at, now()), revision = revision + 1
                WHERE job_id = %(job_id)s
                RETURNING job_id, kind, session_id, owner_id, input, settings, attempt, max_attempts,
                          timeout_seconds, lease_expires_at, revision
                """,
                {"worker": worker_id, "lease": lease_seconds, "job_id": job_id},
            ).fetchone()
            conn.execute(
                "INSERT INTO job_events (job_id, attempt, worker_id, event) VALUES (%s, %s, %s, 'claimed')",
                (job_id, updated["attempt"], worker_id),
            )
            return ClaimedJob(
                job_id=updated["job_id"],
                kind=JobKind(updated["kind"]),
                session_id=updated["session_id"],
                owner_id=updated["owner_id"],
                input=updated["input"],
                settings=updated["settings"],
                attempt=updated["attempt"],
                max_attempts=updated["max_attempts"],
                timeout_seconds=updated["timeout_seconds"],
                lease_expires_at=updated["lease_expires_at"],
                revision=updated["revision"],
            )

    def _guarded_update(self, job: ClaimedJob, worker_id: str, set_sql: str, params: dict[str, Any]) -> int:
        """attempt・lease_owner が一致する行だけ更新する。0 なら lease を失っている。"""
        params = {**params, "job_id": job.job_id, "attempt": job.attempt, "worker": worker_id}
        with self._connect() as conn, conn.transaction():
            result = conn.execute(
                f"""
                UPDATE jobs SET {set_sql}, revision = revision + 1
                WHERE job_id = %(job_id)s AND attempt = %(attempt)s AND lease_owner = %(worker)s
                  AND status IN ('leased', 'running')
                """,
                params,
            )
            if result.rowcount == 0:
                conn.execute(
                    "INSERT INTO job_events (job_id, attempt, worker_id, event) VALUES (%s, %s, %s, 'lease_lost')",
                    (job.job_id, job.attempt, worker_id),
                )
            return result.rowcount

    def mark_running(self, job: ClaimedJob, worker_id: str) -> bool:
        return self._guarded_update(job, worker_id, "status = 'running', heartbeat_at = now()", {}) == 1

    def heartbeat(self, job: ClaimedJob, worker_id: str, lease_seconds: int) -> bool:
        return (
            self._guarded_update(
                job,
                worker_id,
                "heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => %(lease)s)",
                {"lease": lease_seconds},
            )
            == 1
        )

    def complete(self, job: ClaimedJob, worker_id: str, result: WorkerResult) -> bool:
        ok = (
            self._guarded_update(
                job,
                worker_id,
                "status = 'succeeded', result = %(result)s, finished_at = now(), lease_owner = NULL, lease_expires_at = NULL",
                {"result": Jsonb(json.loads(result.model_dump_json()))},
            )
            == 1
        )
        if ok:
            self._event(job, worker_id, "completed", {"outcome": result.outcome, "artifacts": len(result.artifacts)})
        return ok

    def fail(self, job: ClaimedJob, worker_id: str, failure: JobFailure, backoff_seconds: int = 0) -> bool:
        retry = failure.retryable and job.attempt < job.max_attempts
        set_sql = (
            "status = 'queued', failure = %(failure)s, lease_owner = NULL, lease_expires_at = NULL, "
            "available_at = now() + make_interval(secs => %(backoff)s)"
            if retry
            else "status = 'failed', failure = %(failure)s, finished_at = now(), lease_owner = NULL, lease_expires_at = NULL"
        )
        ok = (
            self._guarded_update(
                job,
                worker_id,
                set_sql,
                {"failure": Jsonb(json.loads(failure.model_dump_json())), "backoff": backoff_seconds},
            )
            == 1
        )
        if ok:
            self._event(job, worker_id, "failed", {"code": failure.code, "retry": retry})
        return ok

    def cancel_requested(self, job: ClaimedJob) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT cancel_requested FROM jobs WHERE job_id = %s", (job.job_id,)).fetchone()
            return bool(row and row["cancel_requested"])

    def acknowledge_cancel(self, job: ClaimedJob, worker_id: str) -> bool:
        ok = (
            self._guarded_update(
                job,
                worker_id,
                "status = 'cancelled', finished_at = now(), lease_owner = NULL, lease_expires_at = NULL",
                {},
            )
            == 1
        )
        if ok:
            self._event(job, worker_id, "cancelled", {})
        return ok

    def progress(self, job: ClaimedJob, worker_id: str, detail: dict[str, Any]) -> None:
        self._event(job, worker_id, "progress", detail)

    def _event(self, job: ClaimedJob, worker_id: str, event: str, detail: dict[str, Any]) -> None:
        with self._connect() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO job_events (job_id, attempt, worker_id, event, detail) VALUES (%s, %s, %s, %s, %s)",
                (job.job_id, job.attempt, worker_id, event, Jsonb(detail)),
            )

    # --- API 側操作 (am_api) --------------------------------------------------

    def enqueue(
        self,
        *,
        kind: JobKind,
        session_id: uuid.UUID,
        owner_id: uuid.UUID,
        input: dict[str, Any],
        settings: dict[str, Any],
        idempotency_key: str,
        timeout_seconds: int,
        max_attempts: int = 3,
        conn: psycopg.Connection | None = None,
    ) -> uuid.UUID:
        """同じ idempotency_key の再送は既存 job_id を返す。API が業務 transaction と同じ conn で呼ぶ。"""
        job_id = uuid.uuid4()
        sql = """
            INSERT INTO jobs (job_id, kind, session_id, owner_id, input, settings, idempotency_key, status,
                              timeout_seconds, max_attempts)
            VALUES (%(job_id)s, %(kind)s, %(session_id)s, %(owner_id)s, %(input)s, %(settings)s, %(key)s, 'queued',
                    %(timeout)s, %(max_attempts)s)
            ON CONFLICT (idempotency_key) DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
            RETURNING job_id
        """
        params = {
            "job_id": job_id,
            "kind": kind.value,
            "session_id": session_id,
            "owner_id": owner_id,
            "input": Jsonb(input),
            "settings": Jsonb(settings),
            "key": idempotency_key,
            "timeout": timeout_seconds,
            "max_attempts": max_attempts,
        }
        if conn is not None:
            return conn.execute(sql, params).fetchone()["job_id"]
        with self._connect() as own, own.transaction():
            return own.execute(sql, params).fetchone()["job_id"]

    def request_cancel(self, job_id: uuid.UUID, conn: psycopg.Connection | None = None) -> None:
        sql = "UPDATE jobs SET cancel_requested = true, revision = revision + 1 WHERE job_id = %s AND status IN ('queued','leased','running')"
        if conn is not None:
            conn.execute(sql, (job_id,))
            return
        with self._connect() as own, own.transaction():
            own.execute(sql, (job_id,))

    def cancel_queued(self, job_id: uuid.UUID, conn: psycopg.Connection) -> bool:
        result = conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = now(), revision = revision + 1 WHERE job_id = %s AND status = 'queued'",
            (job_id,),
        )
        return result.rowcount == 1


def lease_deadline(job: ClaimedJob) -> datetime:
    return job.lease_expires_at


def default_timeout(kind: JobKind) -> timedelta:
    return timedelta(hours=6) if kind is JobKind.TRANSCRIPTION else timedelta(minutes=45)


__all__ = ["ClaimedJob", "JobQueue", "PostgresJobQueue", "default_timeout", "lease_deadline"]
