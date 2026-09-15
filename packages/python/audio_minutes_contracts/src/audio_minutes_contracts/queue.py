"""PostgreSQL の jobs テーブルを使う永続ジョブキュー (ADR-0003)。

worker は am_worker ロールで接続し、この module の操作だけを使う。
lease は短い transaction で取得し、推論中に DB ロックを保持しない。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

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

    def mark_external_dispatch_started(self, job: ClaimedJob, worker_id: str) -> bool: ...

    def complete(self, job: ClaimedJob, worker_id: str, result: WorkerResult) -> bool: ...

    def fail(self, job: ClaimedJob, worker_id: str, failure: JobFailure, backoff_seconds: int = 0) -> bool: ...

    def cancel_requested(self, job: ClaimedJob) -> bool: ...

    def progress(self, job: ClaimedJob, worker_id: str, detail: dict[str, Any]) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
            cancelled = conn.execute(
                """
                SELECT job_id, kind, attempt, revision, external_dispatch_started_at
                  FROM jobs
                 WHERE kind = ANY(%(kinds)s)
                   AND cancel_requested = true
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
            if cancelled is not None:
                # worker が取消を観測する前に落ちても、lease 切れ後は行ロックと
                # attempt/revision の fence を取った別 worker が terminal 化する。
                # Claude 送信開始済みだけは取消と断定せず outcome=unknown にする。
                dispatched = (
                    cancelled["kind"] == JobKind.MINUTES_GENERATION.value
                    and cancelled["external_dispatch_started_at"] is not None
                )
                result = (
                    Jsonb(
                        {
                            "schema_version": "worker-result/1",
                            "kind": JobKind.MINUTES_GENERATION.value,
                            "outcome": "unknown",
                            "artifacts": [],
                            "insufficient_information": [],
                        }
                    )
                    if dispatched
                    else None
                )
                status = "succeeded" if dispatched else "cancelled"
                updated = conn.execute(
                    """
                    UPDATE jobs
                       SET status = %(status)s, result = %(result)s, finished_at = now(),
                           lease_owner = NULL, lease_expires_at = NULL, revision = revision + 1
                     WHERE job_id = %(job_id)s
                       AND attempt = %(attempt)s
                       AND revision = %(revision)s
                       AND cancel_requested = true
                       AND (
                            status = 'queued'
                            OR (status IN ('leased', 'running') AND lease_expires_at < now())
                       )
                    """,
                    {
                        "job_id": cancelled["job_id"],
                        "attempt": cancelled["attempt"],
                        "revision": cancelled["revision"],
                        "status": status,
                        "result": result,
                    },
                )
                if updated.rowcount == 1:
                    event = "external_outcome_unknown" if dispatched else "cancelled"
                    reason = "cancel_requested_lease_expired_after_dispatch" if dispatched else "cancel_requested_lease_expired"
                    conn.execute(
                        "INSERT INTO job_events (job_id, attempt, worker_id, event, detail) VALUES (%s, %s, %s, %s, %s)",
                        (
                            cancelled["job_id"],
                            cancelled["attempt"],
                            worker_id,
                            event,
                            Jsonb({"reason": reason}),
                        ),
                    )
                return None
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
            current = conn.execute(
                "SELECT attempt, max_attempts, status, kind, external_dispatch_started_at FROM jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()
            if (
                current["kind"] == JobKind.MINUTES_GENERATION.value
                and current["status"] in ("queued", "leased", "running")
                and current["external_dispatch_started_at"] is not None
            ):
                # Claude は idempotency key を受け取らない。送信開始後に worker が落ちた
                # job を再取得すると、同じ文字起こしを無条件に二重送信してしまう。
                # 成果物の有無を断定せず unknown として terminal にし、利用者の明示操作を待つ。
                conn.execute(
                    """
                    UPDATE jobs SET status = 'succeeded', revision = revision + 1, finished_at = now(),
                        result = %(result)s, lease_owner = NULL, lease_expires_at = NULL
                    WHERE job_id = %(job_id)s
                    """,
                    {
                        "job_id": job_id,
                        "result": Jsonb(
                            {
                                "schema_version": "worker-result/1",
                                "kind": JobKind.MINUTES_GENERATION.value,
                                "outcome": "unknown",
                                "artifacts": [],
                                "insufficient_information": [],
                            }
                        ),
                    },
                )
                conn.execute(
                    "INSERT INTO job_events (job_id, attempt, worker_id, event, detail) VALUES (%s, %s, %s, 'external_outcome_unknown', %s)",
                    (job_id, current["attempt"], worker_id, Jsonb({"reason": "lease_expired_after_dispatch"})),
                )
                return None
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
                                "stage": (
                                    "transcription"
                                    if current["kind"] == JobKind.TRANSCRIPTION.value
                                    else "minutes"
                                ),
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

    def _guarded_update(
        self,
        job: ClaimedJob,
        worker_id: str,
        set_sql: str,
        params: dict[str, Any],
        *,
        reject_cancel_requested: bool = False,
    ) -> int:
        """attempt・lease_owner（必要なら未取消）が一致する行だけ更新する。"""
        params = {**params, "job_id": job.job_id, "attempt": job.attempt, "worker": worker_id}
        cancel_guard = "AND cancel_requested = false" if reject_cancel_requested else ""
        with self._connect() as conn, conn.transaction():
            result = conn.execute(
                f"""
                UPDATE jobs SET {set_sql}, revision = revision + 1
                WHERE job_id = %(job_id)s AND attempt = %(attempt)s AND lease_owner = %(worker)s
                  AND status IN ('leased', 'running')
                  {cancel_guard}
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
        return (
            self._guarded_update(
                job,
                worker_id,
                "status = 'running', heartbeat_at = now()",
                {},
                reject_cancel_requested=True,
            )
            == 1
        )

    def mark_external_dispatch_started(self, job: ClaimedJob, worker_id: str) -> bool:
        """現在の認可状態と lease を DB 内で原子的に検査して送信開始を記録する。

        SECURITY DEFINER 関数だけに業務テーブルの参照を閉じ込める。false の場合は
        lease 喪失か、現在状態の無効化によって job が cancelled になっている。
        """
        with self._connect() as conn, conn.transaction():
            return bool(
                conn.execute(
                    "SELECT authorize_minutes_dispatch(%s, %s, %s)",
                    (job.job_id, job.attempt, worker_id),
                ).fetchone()["authorize_minutes_dispatch"]
            )

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
                reject_cancel_requested=True,
            )
            == 1
        )
        if ok:
            self._event(job, worker_id, "completed", {"outcome": result.outcome, "artifacts": len(result.artifacts)})
        return ok

    def fail(self, job: ClaimedJob, worker_id: str, failure: JobFailure, backoff_seconds: int = 0) -> bool:
        retry = failure.retryable and job.attempt < job.max_attempts
        set_sql = (
            # minutes は送信開始後なら retryable な局所エラーでも自動再送しない。
            # preflight 失敗時は marker が NULL なので従来どおり backoff 再試行できる。
            "status = CASE WHEN kind = 'minutes_generation' AND external_dispatch_started_at IS NOT NULL "
            "              THEN 'failed' ELSE 'queued' END, "
            "failure = %(failure)s, lease_owner = NULL, lease_expires_at = NULL, "
            "finished_at = CASE WHEN kind = 'minutes_generation' AND external_dispatch_started_at IS NOT NULL "
            "                   THEN now() ELSE finished_at END, "
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
                reject_cancel_requested=True,
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
