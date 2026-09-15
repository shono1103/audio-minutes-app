"""Uvicorn の request target が stdout archive へ流れないことを確認する。"""

from __future__ import annotations

import http.client
import importlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = API_ROOT.parents[1]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _wait_until_listening(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"Uvicorn が起動前に終了しました: returncode={process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    pytest.fail("Uvicorn の listen 開始を待機中に timeout しました")


def _request(port: int, method: str, target: str) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(method, target, body=b"{}" if method == "POST" else None)
        response = connection.getresponse()
        response.read()
    finally:
        connection.close()


def _websocket_upgrade(port: int, target: str) -> None:
    request = (
        f"GET {target} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Connection: Upgrade\r\n"
        "Upgrade: websocket\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Key: c3ludGhldGljLXdzLWtleQ==\r\n"
        "\r\n"
    ).encode("ascii")
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(request)
        response = connection.recv(4096)
    assert response.startswith(b"HTTP/1.1 403")


def test_direct_uvicorn_module_does_not_log_sensitive_request_target(tmp_path: Path) -> None:
    port = _free_port()
    secret = "synthetic-bootstrap-invite-reauth-passkey-secret"
    target = f"/not-a-route/{secret}?token={secret}&next=%2Fprivate"
    websocket_secret = "SYNTHETIC-WS-SECRET"
    websocket_target = f"/auth/bootstrap/{websocket_secret}?token={websocket_secret}"
    environment = os.environ.copy()
    environment.update(
        {
            "AM_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
            "AM_UPLOADS_DIR": str(tmp_path / "uploads"),
            "AM_LOG_DIR": str(tmp_path / "logs"),
            "AM_SHARED_GID": str(os.getgid()),
            "AM_BACKGROUND_TASKS": "0",
            "PYTHONUNBUFFERED": "1",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "deploy/container-log-runner.py"),
            "--service",
            "minutes-api",
            "--",
            sys.executable,
            "-m",
            "uvicorn",
            "minutes_api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "info",
        ],
        cwd=API_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_until_listening(process, port)
        _request(port, "GET", target)
        _request(port, "POST", target)
        _websocket_upgrade(port, websocket_target)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate(timeout=5)

    assert "Started server process" in output
    assert "Application startup complete" in output
    assert "Shutting down" in output
    assert secret not in output
    assert "/not-a-route/" not in output
    assert websocket_secret not in output
    assert "/auth/bootstrap/" not in output
    archives = list((tmp_path / "logs/stdout/minutes-api").glob("*.log"))
    assert len(archives) == 1
    archive = archives[0].read_text(encoding="utf-8")
    assert "Started server process" in archive
    assert "Application startup complete" in archive
    assert secret not in archive
    assert "/not-a-route/" not in archive
    assert websocket_secret not in archive
    assert "/auth/bootstrap/" not in archive


def test_console_entrypoint_disables_access_log(monkeypatch) -> None:
    app_module = importlib.import_module("minutes_api.app")
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(app_module.uvicorn, "run", fake_run)
    app_module.main()

    assert captured["kwargs"]["access_log"] is False
    assert captured["kwargs"]["ws"] == "none"


def test_docker_command_disables_access_log_after_log_runner_boundary() -> None:
    dockerfile = REPOSITORY_ROOT / "services/minutes-api/Dockerfile"
    cmd_line = next(line for line in dockerfile.read_text(encoding="utf-8").splitlines() if line.startswith("CMD "))
    command = json.loads(cmd_line.removeprefix("CMD "))

    assert command[:6] == [
        "python",
        "/usr/local/bin/audio-minutes-log-runner.py",
        "--service",
        "minutes-api",
        "--",
        "sh",
    ]
    assert command[-1].endswith(
        "uvicorn minutes_api.app:app --host 0.0.0.0 --port 8000 --no-access-log --ws none"
    )
