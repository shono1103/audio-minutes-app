"""PostgresJobQueue と同じ操作を持つメモリ内キュー (runner の単体試験用)。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from audio_minutes_contracts.models import JobFailure, JobKind, WorkerResult
from audio_minutes_contracts.queue import ClaimedJob


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: dict[uuid.UUID, dict[str, Any]] = {}
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.cancel_flags: set[uuid.UUID] = set()
        self.lost_lease: set[uuid.UUID] = set()
        self.lose_on_dispatch: set[uuid.UUID] = set()

    def add(self, *, owner_id: uuid.UUID, input: dict[str, Any], settings: dict[str, Any], timeout_seconds: int = 600) -> ClaimedJob:
        job_id = uuid.uuid4()
        job = ClaimedJob(
            job_id=job_id,
            kind=JobKind.MINUTES_GENERATION,
            session_id=uuid.uuid4(),
            owner_id=owner_id,
            input=input,
            settings=settings,
            attempt=1,
            max_attempts=3,
            timeout_seconds=timeout_seconds,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=120),
            revision=1,
        )
        self.jobs[job_id] = {"status": "leased", "result": None, "failure": None, "dispatch_started": False}
        return job

    def claim(self, kinds, worker_id, lease_seconds):
        return None

    def mark_running(self, job, worker_id):
        if job.job_id in self.lost_lease:
            return False
        self.jobs[job.job_id]["status"] = "running"
        return True

    def heartbeat(self, job, worker_id, lease_seconds):
        return job.job_id not in self.lost_lease

    def mark_external_dispatch_started(self, job, worker_id):
        if job.job_id in self.lost_lease or job.job_id in self.lose_on_dispatch:
            return False
        self.jobs[job.job_id]["dispatch_started"] = True
        self.events.append(("external_dispatch_started", {}))
        return True

    def complete(self, job, worker_id, result: WorkerResult):
        if job.job_id in self.lost_lease:
            return False
        self.jobs[job.job_id].update(status="succeeded", result=result)
        return True

    def fail(self, job, worker_id, failure: JobFailure, backoff_seconds=0):
        self.jobs[job.job_id].update(status="queued" if failure.retryable else "failed", failure=failure)
        return True

    def cancel_requested(self, job):
        return job.job_id in self.cancel_flags

    def acknowledge_cancel(self, job, worker_id):
        self.jobs[job.job_id]["status"] = "cancelled"
        return True

    def progress(self, job, worker_id, detail):
        self.events.append(("progress", detail))
