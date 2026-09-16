from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from minutes_worker.claude_auth import ClaudeAuthAdapter, LoginInProgress
from tests.conftest import MOCK_BIN


def make_adapter(home: Path, environ: dict[str, str], scenario: str = "", **kwargs) -> ClaudeAuthAdapter:
    extra = {"AM_MOCK_CLAUDE_SCENARIO": scenario} if scenario else {}
    extra.update(kwargs.pop("extra_env", {}))
    return ClaudeAuthAdapter(MOCK_BIN, home, environ=environ, extra_env=extra, status_cache_seconds=0, **kwargs)


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timeout waiting for condition")


def test_status_logged_in_and_logged_out(claude_home, clean_environ):
    assert make_adapter(claude_home, clean_environ, "logged_in").status().state == "logged_in"
    assert make_adapter(claude_home, clean_environ, "logged_out").status().state == "logged_out"


def test_status_never_surfaces_email_or_org(claude_home, clean_environ):
    status = make_adapter(claude_home, clean_environ, "logged_in").status()
    dumped = str(status.to_dict())
    assert "mock-owner@example.com" not in dumped
    assert "org_mock" not in dumped
    assert status.cli_version == "2.1.268" and status.cli_supported


def test_cli_missing(claude_home, clean_environ):
    adapter = ClaudeAuthAdapter("/nonexistent/claude", claude_home, environ=clean_environ, status_cache_seconds=0)
    assert adapter.status().state == "cli_missing"
    session = adapter.login()
    assert session.state == "failed" and session.failure_code == "cli_missing"


def test_cli_incompatible_version(claude_home, clean_environ):
    adapter = make_adapter(claude_home, clean_environ, "logged_in", extra_env={"AM_MOCK_CLAUDE_VERSION": "9.9.9"})
    status = adapter.status()
    assert status.state == "cli_incompatible" and status.cli_version == "9.9.9" and not status.cli_supported


def test_credential_conflict_reports_names_only(claude_home, clean_environ):
    environ = {**clean_environ, "ANTHROPIC_API_KEY": "sk-ant-SECRET-VALUE-123456789"}
    status = make_adapter(claude_home, environ, "logged_in").status()
    assert status.state == "credential_conflict"
    assert status.conflict_env_vars == ["ANTHROPIC_API_KEY"]
    assert "SECRET-VALUE" not in str(status.to_dict())


def test_credential_conflict_from_auth_method(claude_home, clean_environ):
    adapter = make_adapter(claude_home, clean_environ, "logged_in", extra_env={"AM_MOCK_CLAUDE_AUTH_METHOD": "console"})
    assert adapter.status().state == "credential_conflict"
    adapter = make_adapter(claude_home, clean_environ, "logged_in", extra_env={"AM_MOCK_CLAUDE_API_PROVIDER": "bedrock"})
    assert adapter.status().state == "credential_conflict"


def test_login_url_ready_then_completed_and_url_not_logged(claude_home, clean_environ, caplog):
    caplog.set_level(logging.DEBUG)
    adapter = make_adapter(claude_home, clean_environ, "login_url")
    assert adapter.status().state == "logged_out"
    session = adapter.login()
    assert session.state == "pending"
    assert adapter.status().state == "login_pending"
    wait_for(lambda: session.state == "url_ready")
    public = session.public()
    assert public["url"].startswith("https://claude.com/oauth/authorize")
    assert "MOCK-STATE-SECRET" in public["url"]
    with pytest.raises(LoginInProgress):
        adapter.login()
    adapter.submit_login_code(session.auth_session_id, "good-code")
    wait_for(lambda: session.state == "completed")
    assert "url" not in session.public()
    assert adapter.status(force=True).state == "logged_in"
    assert "MOCK-STATE-SECRET" not in caplog.text
    assert "https://" not in caplog.text
    assert "MOCK-STATE-SECRET" not in repr(session)


def test_login_bad_origin_fails(claude_home, clean_environ, caplog):
    caplog.set_level(logging.DEBUG)
    adapter = make_adapter(claude_home, clean_environ, "login_bad_origin")
    session = adapter.login()
    wait_for(lambda: session.state == "failed")
    assert session.failure_code == "claude_cli_incompatible"
    assert "url" not in session.public()
    assert "evil.example.com" not in caplog.text


def test_login_url_allowlist_requires_exact_official_origin(claude_home, clean_environ):
    adapter = make_adapter(claude_home, clean_environ, "logged_out")
    assert adapter._url_allowed("https://claude.com/oauth/authorize")
    assert not adapter._url_allowed("https://claude.com.evil.example/oauth/authorize")
    assert not adapter._url_allowed("https://user@claude.com/oauth/authorize")
    assert not adapter._url_allowed("https://claude.com:8443/oauth/authorize")


def test_login_cancel(claude_home, clean_environ):
    adapter = make_adapter(claude_home, clean_environ, "login_url")
    session = adapter.login()
    wait_for(lambda: session.state == "url_ready")
    cancelled = adapter.cancel_login(session.auth_session_id)
    assert cancelled.state == "cancelled"
    assert "url" not in cancelled.public()
    assert adapter.status(force=True).state == "logged_out"
    # 取消後は新しいログインを開始できる
    assert adapter.login().state == "pending"
    adapter.close()


def test_login_expires(claude_home, clean_environ):
    adapter = make_adapter(claude_home, clean_environ, "login_hang", login_timeout_seconds=1)
    session = adapter.login()
    wait_for(lambda: session.state == "expired", timeout=6)
    assert adapter.status(force=True).state == "logged_out"


def test_logout_and_expired_state(claude_home, clean_environ):
    adapter = make_adapter(claude_home, clean_environ, "login_url")
    session = adapter.login()
    wait_for(lambda: session.state == "url_ready")
    adapter.submit_login_code(session.auth_session_id, "good-code")
    wait_for(lambda: session.state == "completed")
    assert adapter.status(force=True).state == "logged_in"
    (claude_home / ".claude" / "mock-logged-in").unlink()  # 資格情報の失効を模倣
    assert adapter.status(force=True).state == "expired"
    assert adapter.logout().state == "logged_out"


def test_rate_limited_note(claude_home, clean_environ):
    adapter = make_adapter(claude_home, clean_environ, "logged_in")
    adapter.note_rate_limited(60)
    assert adapter.status(force=True).state == "rate_limited"
