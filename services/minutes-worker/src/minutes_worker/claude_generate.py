"""ClaudeGenerationAdapter — Claude CLI の非対話モードで議事録 JSON を生成する (ADR-0005)。

* 配列引数で起動し、本文は stdin。環境変数は allowlist だけ、HOME は専用資格情報 volume。
* 専用の空作業ディレクトリで起動し、hook・MCP・プロジェクト設定・skills を読み込まない。
* 終了コード・出力を分類し、送信後に応答を失った場合は「実行結果不明」として扱う。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from minutes_worker.compat import load_compat
from minutes_worker.prompting import PromptRequest
from minutes_worker.redaction import install_redaction

logger = logging.getLogger(__name__)
install_redaction(logger)


class GenerationError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool, exit_code: int | None = None, diagnostics: dict | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.exit_code = exit_code
        self.diagnostics = diagnostics or {}


class UnknownOutcome(GenerationError):
    """送信後に応答を失った。成果物の二重確定は防げても厳密な一回実行は保証できないため自動再送しない。"""

    def __init__(self, message: str, diagnostics: dict | None = None) -> None:
        super().__init__("claude_unknown_outcome", message, retryable=False, diagnostics=diagnostics)


@dataclass
class GenerationResult:
    structured: dict[str, Any]
    raw_result_text: str
    duration_ms: int
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    envelope_keys: list[str] = field(default_factory=list)


class ClaudeGenerationAdapter:
    def __init__(
        self,
        claude_bin: str,
        claude_home: Path,
        *,
        model: str | None = None,
        environ: dict[str, str] | None = None,
        compat: dict[str, Any] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._bin = claude_bin
        self._home = Path(claude_home)
        self._model = model
        self._environ = environ if environ is not None else os.environ
        self._compat = compat or load_compat()
        self._extra_env = dict(extra_env or {})

    def _child_env(self) -> dict[str, str]:
        env = {name: self._environ[name] for name in self._compat["environment_allowlist"] if name in self._environ}
        env["HOME"] = str(self._home)
        env.setdefault("LANG", "C.UTF-8")
        env["TERM"] = "dumb"
        env.update(self._compat["environment_forced"])
        env.update(self._extra_env)
        return env

    def build_args(self, request: PromptRequest) -> list[str]:
        generate = self._compat["generate"]
        args = [self._bin, *generate["base_args"]]
        if self._model:
            args += [generate["model_flag"], self._model]
        args += [generate["system_prompt_flag"], request.system_prompt]
        args += [generate["json_schema_flag"], json.dumps(request.output_schema, ensure_ascii=False)]
        return args

    def generate(
        self,
        request: PromptRequest,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> GenerationResult:
        self._home.mkdir(parents=True, exist_ok=True)
        args = self.build_args(request)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="am-claude-") as workdir:
            try:
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self._child_env(),
                    cwd=workdir,
                    start_new_session=True,
                )
            except FileNotFoundError as error:
                raise GenerationError("claude_cli_incompatible", "Claude CLI が見つかりません", retryable=False) from error

            stdout_holder: dict[str, bytes] = {}

            def communicate() -> None:
                out, err = process.communicate(request.user_prompt.encode("utf-8"))
                stdout_holder["out"] = out
                stdout_holder["err"] = err

            thread = threading.Thread(target=communicate, daemon=True)
            thread.start()
            deadline = started + timeout_seconds
            while thread.is_alive():
                if cancel_event is not None and cancel_event.is_set():
                    self._kill(process)
                    thread.join(timeout=5)
                    raise GenerationError("cancelled", "取消要求により生成を中止しました", retryable=False)
                if time.monotonic() > deadline:
                    self._kill(process)
                    thread.join(timeout=5)
                    raise UnknownOutcome(
                        "Claude CLI が時間内に応答しませんでした。送信済みの可能性があるため自動再送しません",
                        {"timeout_seconds": timeout_seconds},
                    )
                thread.join(timeout=0.5)
            duration_ms = int((time.monotonic() - started) * 1000)
            stdout = stdout_holder.get("out", b"").decode("utf-8", "replace")
            stderr = stdout_holder.get("err", b"").decode("utf-8", "replace")
            return self._interpret(process.returncode, stdout, stderr, request, duration_ms)

    @staticmethod
    def _kill(process: subprocess.Popen[bytes]) -> None:
        try:
            process.kill()
        except Exception as error:  # noqa: BLE001
            logger.debug("Claude 子プロセスの kill に失敗しました: %s", type(error).__name__)

    def _interpret(self, returncode: int, stdout: str, stderr: str, request: PromptRequest, duration_ms: int) -> GenerationResult:
        generate = self._compat["generate"]
        keys = generate["result_keys"]
        envelope = self._parse_envelope(stdout)
        if envelope is None:
            if returncode != 0 and "Invalid MCP configuration" in stderr:
                raise GenerationError(
                    "claude_cli_incompatible", "CLI が起動オプションを受け付けません", retryable=False, exit_code=returncode
                )
            if returncode != 0 and not stdout.strip():
                raise GenerationError(
                    "claude_output_invalid",
                    "Claude CLI が結果を返さずに終了しました",
                    retryable=True,
                    exit_code=returncode,
                    diagnostics={"stderr_lines": len(stderr.splitlines())},
                )
            raise GenerationError(
                "claude_output_invalid", "Claude CLI の出力を JSON として解釈できません", retryable=True, exit_code=returncode
            )
        result_text = str(envelope.get(keys["result_text"], "") or "")
        is_error = bool(envelope.get(keys["is_error"], False)) or returncode != 0
        if is_error:
            status = envelope.get(keys["api_error_status"])
            if any(marker in result_text for marker in generate["not_logged_in_markers"]) or status in (401, 403):
                raise GenerationError(
                    "claude_not_authenticated", "Claude にログインしていません", retryable=True, exit_code=returncode
                )
            if status in generate["rate_limit_statuses"] or any(marker in result_text for marker in generate["rate_limit_markers"]):
                raise GenerationError(
                    "claude_rate_limited", "Claude の利用上限に到達しました", retryable=True, exit_code=returncode,
                    diagnostics={"api_error_status": status},
                )
            raise GenerationError(
                "internal",
                "Claude CLI がエラーを返しました",
                retryable=True,
                exit_code=returncode,
                diagnostics={"terminal_reason": envelope.get(keys["terminal_reason"]), "api_error_status": status},
            )
        structured = envelope.get(keys["structured_output"])
        if structured is None and result_text.strip():
            try:
                structured = json.loads(result_text)
            except json.JSONDecodeError:
                structured = None
        if not isinstance(structured, dict):
            raise GenerationError(
                "claude_output_invalid", "構造化出力が得られませんでした", retryable=True, exit_code=returncode
            )
        errors = sorted(Draft202012Validator(request.output_schema).iter_errors(structured), key=str)
        if errors:
            raise GenerationError(
                "claude_output_invalid",
                "構造化出力が schema に適合しません",
                retryable=True,
                exit_code=returncode,
                diagnostics={"schema_errors": [error.message for error in errors[:5]]},
            )
        usage = envelope.get(keys["usage"]) or {}
        model_usage = envelope.get(keys["model_usage"]) or {}
        model = next(iter(model_usage.keys()), None) if isinstance(model_usage, dict) else None
        return GenerationResult(
            structured=structured,
            raw_result_text=result_text,
            duration_ms=duration_ms,
            model=model,
            input_tokens=usage.get("input_tokens") if isinstance(usage, dict) else None,
            output_tokens=usage.get("output_tokens") if isinstance(usage, dict) else None,
            envelope_keys=sorted(envelope.keys()),
        )

    @staticmethod
    def _parse_envelope(stdout: str) -> dict[str, Any] | None:
        text = stdout.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # stream 形式や警告行が混ざった場合、最後の JSON 行を試す
            for line in reversed(text.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        parsed = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
            else:
                return None
        if isinstance(parsed, list):
            parsed = next((item for item in reversed(parsed) if isinstance(item, dict) and item.get("type") == "result"), None)
        return parsed if isinstance(parsed, dict) else None


__all__ = ["ClaudeGenerationAdapter", "GenerationError", "GenerationResult", "UnknownOutcome"]
