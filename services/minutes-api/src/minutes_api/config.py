"""環境変数からの設定 (docs/integration-contract.md)。秘密の値はログへ出さない。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("AM_DATABASE_URL", "postgresql://am_api:am_api@127.0.0.1:5432/audio_minutes"))
    secret_key: str = field(default_factory=lambda: _env("AM_SECRET_KEY", "dev-only-secret-change-me"))
    internal_token: str = field(default_factory=lambda: _env("AM_INTERNAL_TOKEN", ""))
    # ブラウザー認証の canonical origin。認証画面 URL・upload URL・WebAuthn の origin 検査が
    # すべてここから決まる。rp_id はこの host と一致していなければパスキーが登録・認証できない。
    public_base_url: str = field(default_factory=lambda: _env("AM_PUBLIC_BASE_URL", "http://localhost:8787").rstrip("/"))
    rp_id: str = field(default_factory=lambda: _env("AM_RP_ID", "localhost"))
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            item.strip()
            for item in _env("AM_ALLOWED_ORIGINS", "http://localhost:8787").split(",")
            if item.strip()
        ]
    )
    artifacts_dir: Path = field(default_factory=lambda: Path(_env("AM_ARTIFACTS_DIR", "/var/lib/audio-minutes/artifacts")))
    uploads_dir: Path = field(default_factory=lambda: Path(_env("AM_UPLOADS_DIR", "/var/lib/audio-minutes/uploads")))
    log_dir: Path = field(default_factory=lambda: Path(_env("AM_LOG_DIR", "/var/log/audio-minutes")))
    update_maintenance_file: Path = field(
        default_factory=lambda: Path(_env("AM_UPDATE_MAINTENANCE_FILE", "/var/log/audio-minutes/.update-maintenance"))
    )
    minutes_worker_url: str = field(default_factory=lambda: _env("AM_MINUTES_WORKER_URL", "http://minutes-worker:8791").rstrip("/"))
    retention_upload_hours: int = field(default_factory=lambda: _int("AM_RETENTION_UPLOAD_HOURS", 24))
    retention_audio_days: int = field(default_factory=lambda: _int("AM_RETENTION_AUDIO_DAYS", 30))
    retention_log_days: int = field(default_factory=lambda: _int("AM_RETENTION_LOG_DAYS", 14))
    max_audio_ms: int = field(default_factory=lambda: _int("AM_MAX_AUDIO_MS", 14_400_000))
    max_upload_bytes: int = field(default_factory=lambda: _int("AM_MAX_UPLOAD_BYTES", 2_147_483_648))
    access_token_seconds: int = field(default_factory=lambda: _int("AM_ACCESS_TOKEN_SECONDS", 15 * 60))
    refresh_token_seconds: int = field(default_factory=lambda: _int("AM_REFRESH_TOKEN_SECONDS", 30 * 24 * 3600))
    reauth_seconds: int = field(default_factory=lambda: _int("AM_REAUTH_SECONDS", 5 * 60))
    browser_reauth_seconds: int = field(default_factory=lambda: _int("AM_BROWSER_REAUTH_SECONDS", 10 * 60))
    bootstrap_seconds: int = 10 * 60
    invitation_hours: int = 24
    authorization_code_seconds: int = 60
    transcription_timeout_seconds: int = field(default_factory=lambda: _int("AM_TRANSCRIPTION_TIMEOUT_SECONDS", 6 * 3600))
    minutes_timeout_seconds: int = field(default_factory=lambda: _int("AM_MINUTES_TIMEOUT_SECONDS", 45 * 60))
    background_tasks: bool = field(default_factory=lambda: _env("AM_BACKGROUND_TASKS", "1") == "1")
    reconcile_interval_seconds: float = field(default_factory=lambda: float(_env("AM_RECONCILE_INTERVAL_SECONDS", "2")))
    sweep_interval_seconds: float = field(default_factory=lambda: float(_env("AM_SWEEP_INTERVAL_SECONDS", "600")))
    ffprobe_bin: str = field(default_factory=lambda: _env("AM_FFPROBE_BIN", "ffprobe"))
    poll_interval_ms: int = 3000
    oauth_client_id: str = "audio-minutes-native"

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url[len("postgresql://") :]
        return url


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings(settings: Settings | None = None) -> Settings:
    global _settings
    _settings = settings or Settings()
    return _settings
