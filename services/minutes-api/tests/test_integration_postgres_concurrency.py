"""PostgreSQLの行ロックを使うretention/retry交差とmigrationの統合試験。"""

from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from minutes_api import accounts, models, retention, sessions_service
from minutes_api.config import Settings, reset_settings
from minutes_api.security import after, now, token_hash

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


def _http_request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    timeout: float = 4,
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _unused_tcp_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture(scope="module")
def postgres_database(tmp_path_factory):
    if shutil.which("docker") is None:
        pytest.skip("docker がありません")
    name = f"am-api-test-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "POSTGRES_USER=am_api",
            "-e",
            "POSTGRES_PASSWORD=api-pw",
            "-e",
            "POSTGRES_DB=audio_minutes",
            "-p",
            "127.0.0.1::5432",
            "postgres:16",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        pytest.skip(f"postgres を起動できません: {run.stderr.strip()[:200]}")
    try:
        port = subprocess.run(
            ["docker", "port", name, "5432/tcp"], capture_output=True, text=True, check=True
        ).stdout.strip().rsplit(":", 1)[-1]
        dsn = f"postgresql://am_api:api-pw@127.0.0.1:{port}/audio_minutes"
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=2):
                    break
            except psycopg.Error:
                if time.monotonic() > deadline:
                    pytest.skip("postgres の起動待ちがタイムアウトしました")
                time.sleep(0.5)
        paths = tmp_path_factory.mktemp("api-postgres")
        reset_settings(
            Settings(
                database_url=dsn,
                artifacts_dir=paths / "artifacts",
                uploads_dir=paths / "uploads",
                log_dir=paths / "logs",
                background_tasks=False,
            )
        )
        alembic = Config(str(ROOT / "services" / "minutes-api" / "alembic.ini"))
        command.upgrade(alembic, "head")
        # 既存0003 DBからのupgrade経路も実際に通す。
        command.downgrade(alembic, "0003_reconciler_and_dispatch")
        command.upgrade(alembic, "head")
        engine = create_engine(dsn.replace("postgresql://", "postgresql+psycopg://", 1), future=True)
        yield sessionmaker(bind=engine, expire_on_commit=False, future=True)
        engine.dispose()
    finally:
        reset_settings()
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def test_browser_reauth_schema_and_dispatch_function_are_migrated(postgres_database) -> None:
    factory = postgres_database
    with factory() as db:
        columns = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'browser_reauth_requests'"
                )
            )
        }
        assert {
            "token_hash",
            "access_token_id",
            "grant_id",
            "purpose",
            "completed_at",
            "action_completed_at",
        } <= columns
        function_exists = db.execute(
            text("SELECT to_regprocedure('authorize_minutes_dispatch(uuid,integer,text)')")
        ).scalar_one()
        assert function_exists is not None


def test_delayed_passkey_options_body_does_not_deadlock_event_loop(
    postgres_database, tmp_path: Path
) -> None:
    """先行body待ちと後続行ロックが交差してもAPI全体を停止させない。"""
    factory = postgres_database
    invitation_token = f"delayed-invite-{uuid.uuid4().hex}"
    browser_token = f"browser-{uuid.uuid4().hex}"
    csrf_token = uuid.uuid4().hex
    invited_email = f"delayed-{uuid.uuid4().hex[:8]}@example.test"
    with factory() as setup:
        owner = models.User(email=f"owner-{uuid.uuid4().hex[:8]}@example.test", role="owner")
        setup.add(owner)
        setup.flush()
        setup.add(
            models.Invitation(
                token_hash=token_hash(invitation_token),
                email=invited_email,
                role="member",
                created_by=owner.id,
                expires_at=after(3600),
            )
        )
        setup.add(
            models.BrowserSession(
                token_hash=token_hash(browser_token),
                csrf_token=csrf_token,
                expires_at=after(3600),
            )
        )
        setup.commit()

    port = _unused_tcp_port()
    database_url = factory.kw["bind"].url.render_as_string(hide_password=False)
    server_env = {
        **os.environ,
        "AM_DATABASE_URL": database_url,
        "AM_PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
        "AM_RP_ID": "127.0.0.1",
        "AM_ALLOWED_ORIGINS": f"http://127.0.0.1:{port}",
        "AM_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
        "AM_UPLOADS_DIR": str(tmp_path / "uploads"),
        "AM_LOG_DIR": str(tmp_path / "logs"),
        "AM_UPDATE_MAINTENANCE_FILE": str(tmp_path / "update-maintenance"),
        "AM_BACKGROUND_TASKS": "0",
    }
    # conftestはSQLite試験との共用metadataをUTC TypeDecoratorへ置換するため、このfixtureが
    # 作るPostgreSQL schemaもtimestamp without time zoneになる。別process側にも同じ変換を
    # 適用し、本番schemaではなく試験fixtureの型へ読み出し規則を合わせる。
    server_command = (
        "import runpy; "
        "runpy.run_path('tests/conftest.py'); "
        "import uvicorn; "
        "uvicorn.run('minutes_api.app:create_app', factory=True, host='127.0.0.1', "
        f"port={port}, log_level='warning', access_log=False)"
    )
    server = subprocess.Popen(
        [sys.executable, "-c", server_command],
        cwd=ROOT / "services" / "minutes-api",
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    first_connection: socket.socket | None = None
    try:
        deadline = time.monotonic() + 10
        while True:
            if server.poll() is not None:
                pytest.fail(f"試験用APIが起動前に終了しました: exit={server.returncode}")
            try:
                if _http_request(port, "GET", "/v1/health", timeout=0.5)[0] == 200:
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                pytest.fail("試験用APIの起動がタイムアウトしました")
            time.sleep(0.05)

        body = json.dumps({"email": invited_email}).encode()
        split_at = body.index(b"@")
        path = f"/auth/invite/{invitation_token}/webauthn/options"
        request_headers = {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf_token,
            "Cookie": f"am_session={browser_token}",
        }
        raw_headers = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"X-CSRF-Token: {csrf_token}\r\n"
            f"Cookie: am_session={browser_token}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        first_connection = socket.create_connection(("127.0.0.1", port), timeout=4)
        first_connection.sendall(raw_headers + body[:split_at])

        # healthが一度応答すれば、先行要求はpartial bodyまで処理されevent loopへ到達済み。
        assert _http_request(port, "GET", "/v1/health")[0] == 200
        time.sleep(0.1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            second = executor.submit(
                _http_request,
                port,
                "POST",
                path,
                body=body,
                headers=request_headers,
            )
            time.sleep(0.1)
            health = executor.submit(_http_request, port, "GET", "/v1/health")
            second_status, second_body = second.result(timeout=5)
            health_status, _ = health.result(timeout=5)

        assert second_status == 200, second_body
        assert health_status == 200

        first_connection.sendall(body[split_at:])
        first_response = http.client.HTTPResponse(first_connection)
        first_response.begin()
        first_body = first_response.read()
        assert first_response.status == 200, first_body
        assert json.loads(first_body)["challenge"]
        assert json.loads(second_body)["challenge"]
    finally:
        if first_connection is not None:
            first_connection.close()
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def test_recovery_code_is_consumed_by_only_one_independent_transaction(postgres_database) -> None:
    factory = postgres_database
    with factory() as setup:
        user, codes = accounts.create_user(
            setup,
            email=f"recovery-{uuid.uuid4().hex[:8]}@example.test",
            role="member",
            password="sufficiently long recovery password 2026",
        )
        setup.commit()
        user_id = user.id
        recovery_code = codes[0]

    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            with factory() as transaction:
                user = transaction.get(models.User, user_id)
                barrier.wait(timeout=5)
                results.append(accounts.consume_recovery_code(transaction, user, recovery_code))
                transaction.commit()
        except BaseException as exc:  # noqa: BLE001 - thread内の失敗を親testへ伝える
            errors.append(exc)

    threads = [threading.Thread(target=consume, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive(), "recovery codeの同時消費が完了しません"

    assert errors == []
    assert sorted(results) == [False, True]


def test_retention_rechecks_active_job_after_waiting_for_retry_lock(postgres_database, monkeypatch) -> None:
    factory = postgres_database
    with factory() as setup:
        owner = models.User(email=f"owner-{uuid.uuid4().hex[:8]}@example.test", role="owner")
        setup.add(owner)
        setup.flush()
        session = models.MeetingSession(
            id=uuid.uuid4(),
            owner_id=owner.id,
            title="期限交差試験",
            input_kind="imported_mixed",
            language_mode="auto",
            allow_external_send=False,
            package={"tracks": [{"track_id": "imported-audio", "start_offset_ms": 0}]},
            status="failed",
            finalized_at=now(),
            audio_retained=True,
            audio_expires_at=now(),
        )
        setup.add(session)
        setup.flush()
        setup.add(
            models.Artifact(
                id=f"art_{uuid.uuid4().hex[:26]}",
                session_id=session.id,
                kind="audio_track",
                track_id="imported-audio",
                role="mixed",
                content_type="audio/wav",
                byte_size=10,
                sha256="0" * 64,
            )
        )
        setup.commit()
        session_id = session.id
        owner_id = owner.id

    lock_attempted = threading.Event()
    original_lock = retention._lock_session

    def observed_lock(db, candidate_id):
        if candidate_id == session_id:
            lock_attempted.set()
        return original_lock(db, candidate_id)

    monkeypatch.setattr(retention, "_lock_session", observed_lock)
    result: dict[str, object] = {}

    def sweep() -> None:
        with factory() as sweep_db:
            result["plan"] = retention.plan_sweep(sweep_db, at=now())
            sweep_db.commit()

    with factory() as retry_db:
        locked = retry_db.execute(
            select(models.MeetingSession).where(models.MeetingSession.id == session_id).with_for_update()
        ).scalars().one()
        thread = threading.Thread(target=sweep, daemon=True)
        thread.start()
        assert lock_attempted.wait(5), "retentionがcandidate選択後のsession lockへ到達しません"
        owner = retry_db.get(models.User, owner_id)
        job_id = sessions_service.retry(
            retry_db,
            locked,
            owner,
            stage="transcription",
            language_mode=None,
        )
        retry_db.commit()
        thread.join(5)
        assert not thread.is_alive()

    plan = result["plan"]
    assert plan.artifact_ids == ()
    with factory() as verify:
        current = verify.get(models.MeetingSession, session_id)
        assert current.audio_retained is True and current.transcription_job_id == job_id
        active = verify.execute(
            select(models.MeetingSession).where(models.MeetingSession.id == session_id)
        ).scalars().one()
        assert active.status == "queued"
