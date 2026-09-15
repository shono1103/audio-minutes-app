"""Docker の postgres:16 を一時起動し、contracts/sql/jobs.sql を適用して runner を 1 周させる。"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from audio_minutes_contracts.artifacts import LocalArtifactStore
from audio_minutes_contracts.models import JobKind
from audio_minutes_contracts.queue import PostgresJobQueue

from minutes_worker.claude_auth import ClaudeAuthAdapter
from minutes_worker.claude_generate import ClaudeGenerationAdapter
from minutes_worker.config import WorkerConfig
from minutes_worker.runner import MinutesRunner
from tests.conftest import FIXTURES, MOCK_BIN, ROOT

pytestmark = pytest.mark.integration
OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")


@pytest.fixture(scope="module")
def postgres_dsns():
    if shutil.which("docker") is None:
        pytest.skip("docker がありません")
    name = f"am-minutes-test-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_USER=am_api", "-e", "POSTGRES_PASSWORD=api-pw",
         "-e", "POSTGRES_DB=audio_minutes", "-p", "127.0.0.1::5432", "postgres:16"],
        capture_output=True, text=True, check=False,
    )
    if run.returncode != 0:
        pytest.skip(f"postgres を起動できません: {run.stderr.strip()[:200]}")
    try:
        port = subprocess.run(["docker", "port", name, "5432/tcp"], capture_output=True, text=True, check=True).stdout.strip().rsplit(":", 1)[-1]
        api_dsn = f"postgresql://am_api:api-pw@127.0.0.1:{port}/audio_minutes"
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(api_dsn, connect_timeout=2):
                    break
            except psycopg.Error:
                if time.monotonic() > deadline:
                    pytest.skip("postgres の起動待ちがタイムアウトしました")
                time.sleep(0.5)
        with psycopg.connect(api_dsn, autocommit=True) as conn:
            # jobs.sql のdispatch guardが参照するAPI所有表の最小形。
            conn.execute("CREATE TABLE users (id uuid PRIMARY KEY, disabled_at timestamptz)")
            conn.execute(
                "CREATE TABLE sessions (id uuid PRIMARY KEY, owner_id uuid NOT NULL, "
                "allow_external_send boolean NOT NULL DEFAULT true, deleted_at timestamptz, minutes_job_id uuid)"
            )
            conn.execute(
                "CREATE TABLE claude_connection (id integer PRIMARY KEY, connected_owner_id uuid, state text NOT NULL)"
            )
            conn.execute((ROOT / "contracts" / "sql" / "jobs.sql").read_text(encoding="utf-8"))
            conn.execute("ALTER ROLE am_worker PASSWORD 'worker-pw'")
        yield api_dsn, f"postgresql://am_worker:worker-pw@127.0.0.1:{port}/audio_minutes"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def test_runner_round_trip_with_worker_role(postgres_dsns, claude_home, clean_environ, tmp_path: Path):
    api_dsn, worker_dsn = postgres_dsns
    store = LocalArtifactStore(tmp_path / "artifacts")
    transcript_id = store.put_bytes((FIXTURES / "transcript.dual-track.json").read_bytes()).artifact_id
    api_queue = PostgresJobQueue(api_dsn)
    session_id = uuid.uuid4()
    settings = {
        "kind": "claude_generated", "allow_external_send": True, "connected_owner_id": str(OWNER),
        "title": "統合試験", "title_edited_by_user": False, "input_kind": "recorded_dual_track",
        "started_at": "2026-09-12T10:00:00+09:00", "duration_ms": 1800000, "transcript_revision": 1,
    }
    job_id = api_queue.enqueue(
        kind=JobKind.MINUTES_GENERATION, session_id=session_id, owner_id=OWNER,
        input={"artifacts": [{"artifact_id": transcript_id, "kind": "transcript_json"}]},
        settings=settings, idempotency_key=f"minutes:{session_id}:1", timeout_seconds=600,
    )
    with psycopg.connect(api_dsn) as conn, conn.transaction():
        conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT DO NOTHING", (OWNER,))
        conn.execute(
            "INSERT INTO sessions (id, owner_id, allow_external_send, minutes_job_id) VALUES (%s, %s, true, %s)",
            (session_id, OWNER, job_id),
        )
        conn.execute(
            "INSERT INTO claude_connection (id, connected_owner_id, state) VALUES (1, %s, 'logged_in') "
            "ON CONFLICT (id) DO UPDATE SET connected_owner_id = EXCLUDED.connected_owner_id, state = EXCLUDED.state",
            (OWNER,),
        )
    assert api_queue.enqueue(
        kind=JobKind.MINUTES_GENERATION, session_id=session_id, owner_id=OWNER, input={"artifacts": []},
        settings=settings, idempotency_key=f"minutes:{session_id}:1", timeout_seconds=600,
    ) == job_id, "同じ冪等キーの再送は同じ job"

    extra = {"AM_MOCK_CLAUDE_SCENARIO": "logged_in"}
    config = WorkerConfig(database_url=worker_dsn, artifacts_dir=store.root, claude_bin=MOCK_BIN, claude_home=claude_home,
                          worker_id="minutes-it", lease_seconds=30)
    runner = MinutesRunner(
        config, PostgresJobQueue(worker_dsn), store,
        ClaudeAuthAdapter(MOCK_BIN, claude_home, environ=clean_environ, extra_env=extra, status_cache_seconds=0),
        ClaudeGenerationAdapter(MOCK_BIN, claude_home, environ=clean_environ, extra_env=extra),
    )
    runner.report_heartbeat()
    outcome = runner.run_once()
    assert outcome is not None and outcome.status == "succeeded" and outcome.job_id == job_id
    assert runner.run_once() is None

    with psycopg.connect(api_dsn) as conn:
        row = conn.execute(
            "SELECT status, attempt, result, lease_owner, external_dispatch_started_at FROM jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        assert row[0] == "succeeded" and row[1] == 1 and row[3] is None
        assert row[2]["kind"] == "minutes_generation" and row[2]["artifacts"][0]["kind"] == "minutes_md"
        assert row[4] is not None
        events = [event for (event,) in conn.execute("SELECT event FROM job_events WHERE job_id = %s ORDER BY event_id", (job_id,))]
        assert events[0] == "claimed" and events[-1] == "completed"
        heartbeat = conn.execute("SELECT kind, claude_state FROM worker_heartbeats WHERE worker_id = 'minutes-it'").fetchone()
        assert heartbeat == ("minutes", "logged_in")

    # am_worker は業務テーブルを持たず、jobs 以外を作成・参照できない
    with psycopg.connect(worker_dsn) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("CREATE TABLE should_fail (id int)")


def test_expired_lease_after_external_dispatch_becomes_unknown_without_reclaim(postgres_dsns) -> None:
    api_dsn, worker_dsn = postgres_dsns
    api_queue = PostgresJobQueue(api_dsn)
    worker_queue = PostgresJobQueue(worker_dsn)
    session_id = uuid.uuid4()
    job_id = api_queue.enqueue(
        kind=JobKind.MINUTES_GENERATION,
        session_id=session_id,
        owner_id=OWNER,
        input={"artifacts": []},
        settings={"allow_external_send": True, "connected_owner_id": str(OWNER)},
        idempotency_key=f"minutes:{session_id}:lease-loss",
        timeout_seconds=600,
    )
    with psycopg.connect(api_dsn) as conn, conn.transaction():
        conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT DO NOTHING", (OWNER,))
        conn.execute(
            "INSERT INTO sessions (id, owner_id, allow_external_send, minutes_job_id) VALUES (%s, %s, true, %s)",
            (session_id, OWNER, job_id),
        )
        conn.execute(
            "INSERT INTO claude_connection (id, connected_owner_id, state) VALUES (1, %s, 'logged_in') "
            "ON CONFLICT (id) DO UPDATE SET connected_owner_id = EXCLUDED.connected_owner_id, state = EXCLUDED.state",
            (OWNER,),
        )
    claimed = worker_queue.claim([JobKind.MINUTES_GENERATION.value], "worker-a", 30)
    assert claimed is not None and claimed.job_id == job_id
    assert worker_queue.mark_running(claimed, "worker-a")
    assert worker_queue.mark_external_dispatch_started(claimed, "worker-a")
    with psycopg.connect(api_dsn) as conn, conn.transaction():
        conn.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id = %s", (job_id,))

    assert worker_queue.claim([JobKind.MINUTES_GENERATION.value], "worker-b", 30) is None
    with psycopg.connect(api_dsn) as conn:
        row = conn.execute("SELECT status, attempt, result FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
        assert row[0] == "succeeded" and row[1] == 1
        assert row[2]["outcome"] == "unknown" and row[2]["artifacts"] == []


@pytest.mark.parametrize(
    "invalidate_sql",
    [
        "UPDATE jobs SET cancel_requested = true WHERE job_id = %(job_id)s",
        "UPDATE sessions SET deleted_at = now() WHERE id = %(session_id)s",
        "UPDATE sessions SET allow_external_send = false WHERE id = %(session_id)s",
        "UPDATE users SET disabled_at = now() WHERE id = %(owner_id)s",
        "UPDATE claude_connection SET connected_owner_id = gen_random_uuid() WHERE id = 1",
        "UPDATE claude_connection SET state = 'not_logged_in' WHERE id = 1",
    ],
)
def test_dispatch_guard_cancels_job_when_current_state_is_invalid(postgres_dsns, invalidate_sql: str) -> None:
    api_dsn, worker_dsn = postgres_dsns
    api_queue = PostgresJobQueue(api_dsn)
    worker_queue = PostgresJobQueue(worker_dsn)
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    settings = {"allow_external_send": True, "connected_owner_id": str(owner_id)}
    job_id = api_queue.enqueue(
        kind=JobKind.MINUTES_GENERATION,
        session_id=session_id,
        owner_id=owner_id,
        input={"artifacts": []},
        settings=settings,
        idempotency_key=f"minutes:{session_id}:guard",
        timeout_seconds=600,
    )
    with psycopg.connect(api_dsn) as conn, conn.transaction():
        conn.execute("INSERT INTO users (id) VALUES (%s)", (owner_id,))
        conn.execute(
            "INSERT INTO sessions (id, owner_id, allow_external_send, minutes_job_id) VALUES (%s, %s, true, %s)",
            (session_id, owner_id, job_id),
        )
        conn.execute(
            "INSERT INTO claude_connection (id, connected_owner_id, state) VALUES (1, %s, 'logged_in') "
            "ON CONFLICT (id) DO UPDATE SET connected_owner_id = EXCLUDED.connected_owner_id, state = EXCLUDED.state",
            (owner_id,),
        )
    claimed = worker_queue.claim([JobKind.MINUTES_GENERATION.value], "guard-worker", 30)
    assert claimed is not None and worker_queue.mark_running(claimed, "guard-worker")
    with psycopg.connect(api_dsn) as conn, conn.transaction():
        conn.execute(invalidate_sql, {"job_id": job_id, "session_id": session_id, "owner_id": owner_id})

    assert worker_queue.mark_external_dispatch_started(claimed, "guard-worker") is False
    with psycopg.connect(api_dsn) as conn:
        row = conn.execute(
            "SELECT status, cancel_requested, lease_owner, external_dispatch_started_at FROM jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        assert row == ("cancelled", True, None, None)


@pytest.mark.parametrize("dispatch_started", [False, True])
def test_cancel_requested_expired_lease_is_fenced_terminal(postgres_dsns, dispatch_started: bool) -> None:
    api_dsn, worker_dsn = postgres_dsns
    api_queue = PostgresJobQueue(api_dsn)
    worker_queue = PostgresJobQueue(worker_dsn)
    session_id = uuid.uuid4()
    job_id = api_queue.enqueue(
        kind=JobKind.MINUTES_GENERATION,
        session_id=session_id,
        owner_id=OWNER,
        input={"artifacts": []},
        settings={"allow_external_send": True, "connected_owner_id": str(OWNER)},
        idempotency_key=f"minutes:{session_id}:cancel-expired",
        timeout_seconds=600,
    )
    with psycopg.connect(api_dsn) as conn, conn.transaction():
        conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT DO NOTHING", (OWNER,))
        conn.execute(
            "INSERT INTO sessions (id, owner_id, allow_external_send, minutes_job_id) VALUES (%s, %s, true, %s)",
            (session_id, OWNER, job_id),
        )
        conn.execute(
            "INSERT INTO claude_connection (id, connected_owner_id, state) VALUES (1, %s, 'logged_in') "
            "ON CONFLICT (id) DO UPDATE SET connected_owner_id = EXCLUDED.connected_owner_id, state = EXCLUDED.state",
            (OWNER,),
        )
    claimed = worker_queue.claim([JobKind.MINUTES_GENERATION.value], "old-worker", 30)
    assert claimed is not None and worker_queue.mark_running(claimed, "old-worker")
    if dispatch_started:
        assert worker_queue.mark_external_dispatch_started(claimed, "old-worker")
    with psycopg.connect(api_dsn) as conn, conn.transaction():
        conn.execute(
            "UPDATE jobs SET cancel_requested = true, lease_expires_at = now() - interval '1 second', "
            "revision = revision + 1 WHERE job_id = %s",
            (job_id,),
        )

    assert worker_queue.mark_running(claimed, "old-worker") is False
    assert worker_queue.claim([JobKind.MINUTES_GENERATION.value], "fence-worker", 30) is None
    assert worker_queue.mark_running(claimed, "old-worker") is False
    with psycopg.connect(api_dsn) as conn:
        status, result, lease_owner = conn.execute(
            "SELECT status, result, lease_owner FROM jobs WHERE job_id = %s", (job_id,)
        ).fetchone()
        assert status == ("succeeded" if dispatch_started else "cancelled")
        assert (result["outcome"] if result else None) == ("unknown" if dispatch_started else None)
        assert lease_owner is None
