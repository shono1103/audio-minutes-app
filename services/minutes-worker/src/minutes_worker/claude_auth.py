"""ClaudeAuthAdapter — Claude CLI の subscription ログイン・状態確認・ログアウト (FR-120〜128, ADR-0005)。

* CLI の起動と資格情報へのアクセスはこの adapter だけが行う。
* 認証 URL はプロセス内メモリだけに保持し、ログ・DB・例外メッセージへ出さない。
* 同時に実行できるログイン処理は配置全体で 1 件。
* API 課金へ切り替わり得る環境変数は値を読まず、名前の存在だけを検出して停止する。
"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from minutes_worker.compat import check_version, load_compat
from minutes_worker.redaction import install_redaction

logger = logging.getLogger(__name__)
install_redaction(logger)

_URL_RE = re.compile(r"https?://[^\s'\"<>)\]]+")

STATUS_STATES = (
    "cli_missing",
    "checking",
    "logged_out",
    "login_pending",
    "logged_in",
    "expired",
    "credential_conflict",
    "rate_limited",
    "cli_incompatible",
)
LOGIN_STATES = ("pending", "url_ready", "completed", "failed", "cancelled", "expired")


class LoginInProgress(Exception):
    """同時ログインは 1 件だけ (409)。"""


class LoginNotFound(KeyError):
    pass


@dataclass(frozen=True)
class AuthStatus:
    state: str
    cli_version: str | None
    cli_supported: bool
    checked_at: datetime
    conflict_env_vars: list[str] = field(default_factory=list)
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "cli_version": self.cli_version,
            "cli_supported": self.cli_supported,
            "checked_at": self.checked_at.isoformat(),
            "conflict_env_vars": list(self.conflict_env_vars),
            "detail": self.detail,
        }


@dataclass
class LoginSession:
    auth_session_id: str
    state: str
    expires_at: datetime
    failure_code: str | None = None
    _url: str | None = field(default=None, repr=False)
    _process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    _master_fd: int | None = field(default=None, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        """API へ返す表現。URL は url_ready の間だけ含める。"""
        body: dict[str, Any] = {
            "auth_session_id": self.auth_session_id,
            "state": self.state,
            "expires_at": self.expires_at.isoformat(),
        }
        if self.state == "url_ready" and self._url:
            body["url"] = self._url
        if self.failure_code:
            body["failure_code"] = self.failure_code
        return body

    def __repr__(self) -> str:  # URL・プロセスを repr に含めない
        return f"LoginSession(id={self.auth_session_id}, state={self.state})"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ClaudeAuthAdapter:
    def __init__(
        self,
        claude_bin: str,
        claude_home: Path,
        *,
        login_timeout_seconds: int = 300,
        status_cache_seconds: float = 15.0,
        environ: dict[str, str] | None = None,
        compat: dict[str, Any] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._bin = claude_bin
        self._home = Path(claude_home)
        self._login_timeout = login_timeout_seconds
        self._status_cache_seconds = status_cache_seconds
        self._environ = environ if environ is not None else os.environ
        self._compat = compat or load_compat()
        self._extra_env = dict(extra_env or {})
        self._lock = threading.RLock()
        self._login: LoginSession | None = None
        self._last_logged_in: bool | None = None
        self._rate_limited_until: datetime | None = None
        self._cached_status: AuthStatus | None = None
        self._cached_at: float = 0.0

    # --- 環境 -----------------------------------------------------------------

    def _child_env(self) -> dict[str, str]:
        env = {name: self._environ[name] for name in self._compat["environment_allowlist"] if name in self._environ}
        env["HOME"] = str(self._home)
        env.setdefault("LANG", "C.UTF-8")
        env["TERM"] = "dumb"
        env.update(self._compat["environment_forced"])
        env.update(self._extra_env)
        return env

    def conflict_env_vars(self) -> list[str]:
        """値は読まず、名前の存在だけを返す (FR-127)。"""
        return [name for name in self._compat["conflict_environment_variables"] if name in self._environ]

    def _run(self, args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        self._home.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [self._bin, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._child_env(),
            cwd=str(self._home),
            stdin=subprocess.DEVNULL,
            check=False,
        )

    # --- 状態 -----------------------------------------------------------------

    def cli_version(self) -> tuple[str | None, bool, bool]:
        """(version, supported, present)。"""
        try:
            completed = self._run(self._compat["version_args"], timeout=20)
        except FileNotFoundError:
            return None, False, False
        except (subprocess.TimeoutExpired, PermissionError):
            return None, False, True
        if completed.returncode != 0:
            return None, False, True
        check = check_version(completed.stdout + completed.stderr, self._compat)
        return check.version, check.supported, True

    def note_rate_limited(self, seconds: int = 300) -> None:
        with self._lock:
            self._rate_limited_until = _utcnow() + timedelta(seconds=seconds)
            self._cached_status = None

    def note_not_logged_in(self) -> None:
        """生成時に未認証エラーを受けた。次の status で expired と判定できるように記録する。"""
        with self._lock:
            self._cached_status = None

    def status(self, *, force: bool = False) -> AuthStatus:
        with self._lock:
            now = time.monotonic()
            if not force and self._cached_status and now - self._cached_at < self._status_cache_seconds:
                return self._cached_status
            result = self._compute_status()
            self._cached_status = result
            self._cached_at = now
            return result

    def _compute_status(self, *, ignore_active_login: bool = False) -> AuthStatus:
        checked_at = _utcnow()
        conflicts = self.conflict_env_vars()
        version, supported, present = self.cli_version()
        if not present:
            return AuthStatus("cli_missing", None, False, checked_at, conflicts, "Claude CLI が見つかりません")
        if not supported:
            return AuthStatus(
                "cli_incompatible", version, False, checked_at, conflicts, "対応表にない Claude CLI 版です"
            )
        if conflicts:
            return AuthStatus(
                "credential_conflict",
                version,
                True,
                checked_at,
                conflicts,
                "API 課金用の認証設定が存在するため Claude 処理を停止しています",
            )
        if not ignore_active_login and self._login is not None and self._login.state in ("pending", "url_ready"):
            self._expire_login_if_needed()
            if self._login.state in ("pending", "url_ready"):
                return AuthStatus("login_pending", version, True, checked_at, [], None)
        try:
            completed = self._run(self._compat["auth"]["status_args"], timeout=30)
        except subprocess.TimeoutExpired:
            return AuthStatus("checking", version, True, checked_at, [], "状態確認がタイムアウトしました")
        parsed = self._parse_status_json(completed.stdout)
        if parsed is None:
            return AuthStatus("cli_incompatible", version, False, checked_at, [], "status の出力形式が対応表と一致しません")
        keys = self._compat["auth"]["status_keys"]
        logged_in = bool(parsed.get(keys["logged_in"], False))
        auth_method = parsed.get(keys["auth_method"])
        api_provider = parsed.get(keys["api_provider"])
        if logged_in and (
            auth_method != self._compat["auth"]["expected_auth_method"]
            or api_provider != self._compat["auth"]["expected_api_provider"]
        ):
            return AuthStatus(
                "credential_conflict",
                version,
                True,
                checked_at,
                [],
                "subscription 以外の認証方式が有効です (authMethod / apiProvider)",
            )
        if logged_in:
            self._last_logged_in = True
            if self._rate_limited_until and self._rate_limited_until > checked_at:
                return AuthStatus("rate_limited", version, True, checked_at, [], "利用上限に到達しています")
            return AuthStatus("logged_in", version, True, checked_at, [], None)
        state = "expired" if self._last_logged_in else "logged_out"
        self._last_logged_in = False
        return AuthStatus(state, version, True, checked_at, [], None)

    @staticmethod
    def _parse_status_json(stdout: str) -> dict[str, Any] | None:
        text = stdout.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # 先頭に警告行が混ざる版に備え、最初の { 以降を試す
            start = text.find("{")
            if start < 0:
                return None
            try:
                parsed = json.loads(text[start:])
            except json.JSONDecodeError:
                return None
        return parsed if isinstance(parsed, dict) and "loggedIn" in parsed else None

    # --- ログイン ---------------------------------------------------------------

    def login(self) -> LoginSession:
        with self._lock:
            self._expire_login_if_needed()
            if self._login is not None and self._login.state in ("pending", "url_ready"):
                raise LoginInProgress("ログイン処理が既に進行中です")
            status = self.status(force=True)
            if status.state in ("cli_missing", "cli_incompatible", "credential_conflict"):
                session = LoginSession(str(uuid.uuid4()), "failed", _utcnow(), failure_code=status.state)
                self._login = session
                return session
            session = LoginSession(
                auth_session_id=str(uuid.uuid4()),
                state="pending",
                expires_at=_utcnow() + timedelta(seconds=self._login_timeout),
            )
            self._start_login_process(session)
            self._login = session
            self._cached_status = None
            logger.info("Claude ログインを開始しました auth_session_id=%s", session.auth_session_id)
            return session

    def _start_login_process(self, session: LoginSession) -> None:
        self._home.mkdir(parents=True, exist_ok=True)
        master_fd, slave_fd = os.openpty()
        try:
            process = subprocess.Popen(
                [self._bin, *self._compat["auth"]["login_args"]],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=self._child_env(),
                cwd=str(self._home),
                start_new_session=True,
                close_fds=True,
            )
        except FileNotFoundError:
            os.close(master_fd)
            os.close(slave_fd)
            session.state = "failed"
            session.failure_code = "cli_missing"
            return
        finally:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        session._process = process
        session._master_fd = master_fd
        thread = threading.Thread(target=self._pump_login_output, args=(session,), daemon=True)
        session._thread = thread
        thread.start()

    def _pump_login_output(self, session: LoginSession) -> None:
        """PTY 出力を読み、URL を抽出する。バッファは URL 判定後に破棄し、ログへ出さない。"""
        buffer = b""
        master_fd = session._master_fd
        process = session._process
        assert master_fd is not None and process is not None
        try:
            while True:
                if _utcnow() >= session.expires_at:
                    self._finish_login(session, "expired")
                    return
                ready, _, _ = select.select([master_fd], [], [], 0.5)
                if ready:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        break
                    buffer += chunk
                    if session.state == "pending":
                        for match in _URL_RE.finditer(buffer.decode("utf-8", "replace")):
                            url = match.group(0).rstrip(".,;")
                            if self._url_allowed(url):
                                with self._lock:
                                    session._url = url
                                    session.state = "url_ready"
                                logger.info("認証 URL を取得しました auth_session_id=%s", session.auth_session_id)
                            else:
                                logger.warning("allowlist 外の origin を提示されたためログインを中止します")
                                self._finish_login(session, "failed", failure_code="claude_cli_incompatible")
                                return
                            break
                    if len(buffer) > 65536:
                        buffer = buffer[-8192:]
                if process.poll() is not None and not ready:
                    break
            process.wait(timeout=5)
            if session.state in ("cancelled", "expired", "failed"):
                return
            if process.returncode == 0:
                # ログイン session が url_ready の間は public status が login_pending を返す。
                # CLI 終了後の検証だけは、その session を無視して資格情報を直接再確認する。
                status = self._compute_status(ignore_active_login=True)
                if status.state == "logged_in":
                    self._finish_login(session, "completed")
                    return
            self._finish_login(session, "failed", failure_code="claude_not_authenticated")
        except Exception:
            logger.exception("ログイン監視で予期しないエラー")
            self._finish_login(session, "failed", failure_code="internal")
        finally:
            buffer = b""
            self._close_login_fds(session)

    def _url_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc:
            return False
        origin = f"{parts.scheme}://{parts.netloc}/"
        return origin in self._compat["auth"]["url_allowlist_origins"]

    def _finish_login(self, session: LoginSession, state: str, *, failure_code: str | None = None) -> None:
        with self._lock:
            if session.state in ("completed", "failed", "cancelled", "expired"):
                return
            session.state = state
            session.failure_code = failure_code
            session._url = None
            self._cached_status = None
        self._terminate(session)

    def _terminate(self, session: LoginSession) -> None:
        process = session._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    process.kill()
                except Exception as error:  # noqa: BLE001
                    logger.debug("ログイン子プロセスの kill に失敗しました: %s", type(error).__name__)

    def _close_login_fds(self, session: LoginSession) -> None:
        if session._master_fd is not None:
            try:
                os.close(session._master_fd)
            except OSError:
                pass
            session._master_fd = None

    def _expire_login_if_needed(self) -> None:
        session = self._login
        if session is None:
            return
        if session.state in ("pending", "url_ready") and _utcnow() >= session.expires_at:
            self._finish_login(session, "expired")

    def get_login(self, auth_session_id: str) -> LoginSession:
        with self._lock:
            session = self._login
            if session is None or session.auth_session_id != auth_session_id:
                raise LoginNotFound(auth_session_id)
            self._expire_login_if_needed()
            return session

    def cancel_login(self, auth_session_id: str) -> LoginSession:
        session = self.get_login(auth_session_id)
        self._finish_login(session, "cancelled")
        return session

    def submit_login_code(self, auth_session_id: str, code: str) -> LoginSession:
        """コンテナ内に callback が届かない配置向けに、owner が受け取った認可コードを CLI へ渡す拡張。
        コードは書き込むだけで保持・ログしない。"""
        session = self.get_login(auth_session_id)
        if session.state != "url_ready" or session._master_fd is None:
            raise LoginInProgress("コードを受け付けられる状態ではありません")
        os.write(session._master_fd, (code.strip() + "\n").encode())
        return session

    def logout(self) -> AuthStatus:
        with self._lock:
            if self._login is not None and self._login.state in ("pending", "url_ready"):
                self._finish_login(self._login, "cancelled")
            try:
                self._run(self._compat["auth"]["logout_args"], timeout=30)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            self._last_logged_in = False
            self._cached_status = None
            logger.info("Claude からログアウトしました")
            return self.status(force=True)

    def close(self) -> None:
        with self._lock:
            if self._login is not None:
                self._finish_login(self._login, "cancelled")


__all__ = [
    "LOGIN_STATES",
    "STATUS_STATES",
    "AuthStatus",
    "ClaudeAuthAdapter",
    "LoginInProgress",
    "LoginNotFound",
    "LoginSession",
]
