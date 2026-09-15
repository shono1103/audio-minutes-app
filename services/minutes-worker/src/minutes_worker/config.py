"""環境変数からの設定 (docs/integration-contract.md)。"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else default


@dataclass(frozen=True)
class WorkerConfig:
    database_url: str = field(default_factory=lambda: os.environ.get("AM_WORKER_DATABASE_URL", ""))
    artifacts_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("AM_ARTIFACTS_DIR", "/var/lib/audio-minutes/artifacts"))
    )
    internal_bind: str = field(default_factory=lambda: os.environ.get("AM_INTERNAL_BIND", "0.0.0.0:8791"))
    internal_token: str = field(default_factory=lambda: os.environ.get("AM_INTERNAL_TOKEN", ""))
    claude_bin: str = field(default_factory=lambda: os.environ.get("AM_CLAUDE_BIN", "claude"))
    claude_home: Path = field(
        default_factory=lambda: Path(os.environ.get("AM_CLAUDE_HOME", "/var/lib/audio-minutes/claude"))
    )
    claude_model: str | None = field(default_factory=lambda: os.environ.get("AM_CLAUDE_MODEL") or None)
    claude_mock: bool = field(default_factory=lambda: os.environ.get("AM_CLAUDE_MOCK", "0") == "1")
    chunk_chars: int = field(default_factory=lambda: _env_int("AM_CLAUDE_CHUNK_CHARS", 60_000))
    generate_timeout_seconds: int = field(default_factory=lambda: _env_int("AM_CLAUDE_TIMEOUT_SECONDS", 1500))
    login_timeout_seconds: int = field(default_factory=lambda: _env_int("AM_CLAUDE_LOGIN_TIMEOUT_SECONDS", 300))
    worker_id: str = field(
        default_factory=lambda: os.environ.get("AM_WORKER_ID") or f"minutes-{socket.gethostname()}"
    )
    lease_seconds: int = field(default_factory=lambda: _env_int("AM_LEASE_SECONDS", 120))
    poll_interval_seconds: float = field(default_factory=lambda: float(os.environ.get("AM_POLL_INTERVAL", "2")))
    update_maintenance_file: Path = field(
        default_factory=lambda: Path(
            os.environ.get("AM_UPDATE_MAINTENANCE_FILE", "/var/log/audio-minutes/.update-maintenance")
        )
    )
    log_level: str = field(default_factory=lambda: os.environ.get("AM_LOG_LEVEL", "info"))

    def resolved_claude_bin(self) -> str:
        if self.claude_mock:
            return str(Path(__file__).resolve().parents[2] / "mock_claude.py")
        return self.claude_bin

    @property
    def heartbeat_seconds(self) -> int:
        return max(5, self.lease_seconds // 3)
