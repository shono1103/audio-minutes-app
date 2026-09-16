#!/usr/bin/env python3
"""Claude Code CLI 2.1.268 の模倣。試験専用 (AM_CLAUDE_MOCK=1)。

AM_MOCK_CLAUDE_SCENARIO で挙動を切り替える:
  logged_out | logged_in | login_url | login_bad_origin | login_hang | rate_limited | invalid_json | hang | insufficient
未指定なら $HOME/.claude/mock-logged-in の有無でログイン状態を判定する。
出力形式は実 CLI で確認した形 (claude_compat.json) に合わせる。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

VERSION = os.environ.get("AM_MOCK_CLAUDE_VERSION", "2.1.268")
SCENARIO = os.environ.get("AM_MOCK_CLAUDE_SCENARIO", "")
MARKER = Path(os.environ.get("HOME", "/tmp")) / ".claude" / "mock-logged-in"


def logged_in() -> bool:
    if SCENARIO == "logged_out":
        return False
    if SCENARIO in ("logged_in", "rate_limited", "invalid_json", "hang", "insufficient"):
        return True
    return MARKER.exists()


def status_json() -> dict:
    if logged_in():
        return {
            "loggedIn": True,
            "authMethod": os.environ.get("AM_MOCK_CLAUDE_AUTH_METHOD", "claude.ai"),
            "apiProvider": os.environ.get("AM_MOCK_CLAUDE_API_PROVIDER", "firstParty"),
            "analyticsDisabled": False,
            "projectsDirectory": str(MARKER.parent / "projects"),
            "configDirectory": str(MARKER.parent),
            "email": "mock-owner@example.com",
            "orgId": "org_mock_0000",
            "orgName": "Mock Org",
            "subscriptionType": "max",
        }
    return {
        "loggedIn": False,
        "authMethod": "none",
        "apiProvider": "firstParty",
        "analyticsDisabled": False,
        "projectsDirectory": str(MARKER.parent / "projects"),
        "configDirectory": str(MARKER.parent),
    }


def cmd_auth(args: list[str]) -> int:
    if not args:
        print("Usage: claude auth [options] [command]")
        return 1
    sub = args[0]
    if sub == "status":
        if "--text" in args:
            print("Login method: Claude account" if logged_in() else "Not logged in")
        else:
            print(json.dumps(status_json(), indent=2))
        return 0
    if sub == "login":
        if SCENARIO == "login_hang":
            time.sleep(3600)
            return 0
        if SCENARIO == "login_bad_origin":
            print("Browser didn't open? Use the url below to sign in:")
            print()
            print("https://evil.example.com/oauth/authorize?state=BAD")
            sys.stdout.flush()
            time.sleep(3600)
            return 0
        print("Browser didn't open? Use the url below to sign in:")
        print()
        print(
            "https://claude.com/oauth/authorize?code=true&client_id=mock-client&response_type=code"
            "&redirect_uri=http%3A%2F%2Flocalhost%3A54545%2Fcallback&scope=user%3Ainference"
            "&state=MOCK-STATE-SECRET-0001"
        )
        print()
        print("Paste code here if prompted > ", end="")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            return 1
        code = line.strip()
        if code == "bad-code":
            print("Login failed: invalid code")
            return 1
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text("1")
        print("Login successful. Press Enter to continue…")
        return 0
    if sub == "logout":
        MARKER.unlink(missing_ok=True)
        print("Successfully logged out from your Anthropic account.")
        return 0
    print(f"error: unknown command '{sub}'")
    return 1


def result_envelope(is_error: bool, result: str, structured: dict | None, api_error_status: int | None = None) -> dict:
    body = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "duration_ms": 1234,
        "duration_api_ms": 1000,
        "num_turns": 1,
        "result": result,
        "stop_reason": "end_turn" if not is_error else "stop_sequence",
        "session_id": "00000000-0000-4000-8000-00000000mock",
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 1000, "output_tokens": 200},
        "modelUsage": {"claude-mock": {"inputTokens": 1000, "outputTokens": 200}},
        "permission_denials": [],
        "terminal_reason": "api_error" if is_error else "completed",
        "uuid": "11111111-1111-4111-8111-11111111mock",
    }
    if structured is not None:
        body["structured_output"] = structured
    if api_error_status is not None:
        body["api_error_status"] = api_error_status
    return body


def build_structured(prompt: str, schema: dict) -> dict:
    """prompt 内の segment ID と <format> セクションを拾って、schema に合う出力を作る。"""
    # transcript 行と、分割処理後の `[refs: seg-...]` の両方から根拠を引き継ぐ。
    references = re.findall(r"\b(seg-[0-9a-zA-Z-]+)\b", prompt)
    unique_refs: list[str] = []
    for reference in references:
        if reference not in unique_refs:
            unique_refs.append(reference)
    properties = schema.get("properties", {})
    if "notes" in properties:  # 中間要約 (chunk)
        return {
            "notes": [
                {"markdown": f"モック中間要約 ({len(unique_refs)} segment)", "references": unique_refs[:20]}
            ]
        }
    section_keys = re.findall(r"^- key=([a-z_]+) title=(.+)$", prompt, flags=re.MULTILINE)
    sections = []
    for key, title in section_keys:
        if key == "transcript_references":
            continue
        markdown = f"モック本文 ({title.strip()})"
        if key == "action_items":
            markdown = "- [ ] 次回までにロードマップ案を共有する (担当: 発言なし)"
        if "<script>" in prompt or "evil" in prompt:
            markdown += " <script>alert(1)</script> ![x](https://evil.example/pixel.png)"
        sections.append(
            {"key": key, "title": title.strip(), "markdown": markdown, "references": unique_refs[:5]}
        )
    insufficient = []
    if SCENARIO == "insufficient" or "予算" in prompt:
        insufficient.append("予算に関する議論は文字起こしに含まれていません")
    return {"title": "モック議事録: ロードマップ確認", "sections": sections, "insufficient_information": insufficient}


def cmd_print(args: list[str]) -> int:
    if not logged_in():
        print(json.dumps(result_envelope(True, "Not logged in · Please run /login", None)))
        return 1
    if "--mcp-config" in args:
        config = args[args.index("--mcp-config") + 1]
        try:
            parsed = json.loads(config)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict) or "mcpServers" not in parsed:
            sys.stderr.write("Error: Invalid MCP configuration:\nmcpServers: Invalid input\n")
            return 1
    schema = {}
    if "--json-schema" in args:
        schema = json.loads(args[args.index("--json-schema") + 1])
    prompt = sys.stdin.read()
    if SCENARIO == "hang":
        time.sleep(3600)
        return 0
    if SCENARIO == "rate_limited":
        print(json.dumps(result_envelope(True, "Rate limit reached. Please try again later.", None, 429)))
        return 1
    if SCENARIO == "invalid_json":
        print("this is not json {{{")
        return 0
    structured = build_structured(prompt, schema)
    print(json.dumps(result_envelope(False, json.dumps(structured, ensure_ascii=False), structured)))
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print("Usage: claude [options] [command] [prompt]")
        return 1
    if argv[0] in ("--version", "-v"):
        print(f"{VERSION} (Claude Code)")
        return 0
    if argv[0] == "auth":
        return cmd_auth(argv[1:])
    if "-p" in argv or "--print" in argv:
        return cmd_print(argv)
    print("mock: unsupported invocation", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
