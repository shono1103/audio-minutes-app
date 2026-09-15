from __future__ import annotations

import threading

import pytest

from minutes_worker.claude_generate import ClaudeGenerationAdapter, GenerationError, UnknownOutcome
from minutes_worker.prompting import SessionMeta, build_final_request
from tests.conftest import MOCK_BIN


def adapter(home, environ, scenario: str) -> ClaudeGenerationAdapter:
    return ClaudeGenerationAdapter(MOCK_BIN, home, environ=environ, extra_env={"AM_MOCK_CLAUDE_SCENARIO": scenario})


def request(transcript, profile):
    return build_final_request(transcript, profile, SessionMeta("t", None, None, "recorded_dual_track"))


def test_build_args_follow_compat(claude_home, clean_environ, transcript, standard_profile):
    args = adapter(claude_home, clean_environ, "logged_in").build_args(request(transcript, standard_profile))
    assert args[0] == MOCK_BIN and "-p" in args and args[args.index("--output-format") + 1] == "json"
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--system-prompt" in args and "--json-schema" in args and "--no-session-persistence" in args
    assert "--bare" not in args


def test_success_returns_schema_valid_structured_output(claude_home, clean_environ, transcript, standard_profile):
    result = adapter(claude_home, clean_environ, "logged_in").generate(request(transcript, standard_profile), timeout_seconds=30)
    assert result.structured["title"].startswith("モック議事録")
    keys = [section["key"] for section in result.structured["sections"]]
    assert "summary" in keys and "transcript_references" not in keys
    assert result.input_tokens == 1000 and result.model == "claude-mock"


def test_insufficient_information_propagates(claude_home, clean_environ, transcript, standard_profile):
    result = adapter(claude_home, clean_environ, "insufficient").generate(request(transcript, standard_profile), timeout_seconds=30)
    assert result.structured["insufficient_information"]


@pytest.mark.parametrize(
    "scenario, code",
    [("logged_out", "claude_not_authenticated"), ("rate_limited", "claude_rate_limited"), ("invalid_json", "claude_output_invalid")],
)
def test_error_classification(claude_home, clean_environ, transcript, standard_profile, scenario, code):
    with pytest.raises(GenerationError) as error:
        adapter(claude_home, clean_environ, scenario).generate(request(transcript, standard_profile), timeout_seconds=30)
    assert error.value.code == code


def test_hang_becomes_unknown_outcome(claude_home, clean_environ, transcript, standard_profile):
    with pytest.raises(UnknownOutcome):
        adapter(claude_home, clean_environ, "hang").generate(request(transcript, standard_profile), timeout_seconds=1)


def test_cancel_event_stops_process(claude_home, clean_environ, transcript, standard_profile):
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    with pytest.raises(GenerationError) as error:
        adapter(claude_home, clean_environ, "hang").generate(request(transcript, standard_profile), timeout_seconds=30, cancel_event=cancel)
    assert error.value.code == "cancelled"


def test_missing_binary(claude_home, clean_environ, transcript, standard_profile):
    generator = ClaudeGenerationAdapter("/nonexistent/claude", claude_home, environ=clean_environ)
    with pytest.raises(GenerationError) as error:
        generator.generate(request(transcript, standard_profile), timeout_seconds=5)
    assert error.value.code == "claude_cli_incompatible"


def test_child_env_is_allowlisted(claude_home, clean_environ):
    environ = {**clean_environ, "ANTHROPIC_API_KEY": "x", "SOME_RANDOM": "y"}
    env = ClaudeGenerationAdapter(MOCK_BIN, claude_home, environ=environ)._child_env()
    assert "ANTHROPIC_API_KEY" not in env and "SOME_RANDOM" not in env
    assert env["HOME"] == str(claude_home) and env["DISABLE_AUTOUPDATER"] == "1"
