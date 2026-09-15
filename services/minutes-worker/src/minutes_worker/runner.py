"""minutes_generation ジョブの実行ループ (ADR-0003 / M5 安全条件 / FR-129 / FR-152)。

送信直前に外部送信可否・接続 owner・ログイン状態を再検査し、成果物は一時ファイル → 検証 → 不変 ID で公開する。
"""

from __future__ import annotations

import logging
import platform
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from audio_minutes_contracts.artifacts import LocalArtifactStore
from audio_minutes_contracts.models import (
    ClaudeUsage,
    FormatProfile,
    JobFailure,
    JobKind,
    MinutesJobSettings,
    Transcript,
    WorkerResult,
    WorkerResultArtifact,
)
from audio_minutes_contracts.queue import ClaimedJob, PostgresJobQueue
from psycopg.rows import dict_row

from minutes_worker import __version__
from minutes_worker.claude_auth import ClaudeAuthAdapter
from minutes_worker.claude_generate import ClaudeGenerationAdapter, GenerationError, UnknownOutcome
from minutes_worker.config import WorkerConfig
from minutes_worker.prompting import (
    SessionMeta,
    build_final_request,
    ordered_segments,
    plan_requests,
    validate_final_output,
)
from minutes_worker.redaction import install_redaction
from minutes_worker.render import render_minutes

logger = logging.getLogger(__name__)
install_redaction(logger)

_STANDARD_PROFILE_PATH = Path(__file__).resolve().parents[4] / "contracts" / "fixtures" / "format-profile.standard.json"


class PreflightRejected(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class LeaseLostBeforeDispatch(Exception):
    """外部送信開始の永続化前に lease を失った。Claude は呼び出していない。"""


@dataclass
class JobOutcome:
    job_id: uuid.UUID
    status: str  # succeeded | unknown | failed | cancelled | lease_lost
    code: str | None = None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class MinutesRunner:
    def __init__(
        self,
        config: WorkerConfig,
        queue: PostgresJobQueue,
        store: LocalArtifactStore,
        auth: ClaudeAuthAdapter,
        generator: ClaudeGenerationAdapter,
    ) -> None:
        self.config = config
        self.queue = queue
        self.store = store
        self.auth = auth
        self.generator = generator
        self._stop = threading.Event()

    # --- ループ -----------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        logger.info("minutes-worker %s 起動 worker_id=%s", __version__, self.config.worker_id)
        last_heartbeat = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_heartbeat >= self.config.heartbeat_seconds:
                self.report_heartbeat()
                last_heartbeat = now
            if self.config.update_maintenance_file.exists():
                self._stop.wait(self.config.poll_interval_seconds)
                continue
            outcome = self.run_once()
            if outcome is None:
                self._stop.wait(self.config.poll_interval_seconds)

    def run_once(self) -> JobOutcome | None:
        if self.config.update_maintenance_file.exists():
            return None
        try:
            job = self.queue.claim([JobKind.MINUTES_GENERATION.value], self.config.worker_id, self.config.lease_seconds)
        except psycopg.Error:
            logger.exception("ジョブ取得で DB エラー")
            return None
        if job is None:
            return None
        return self.process(job)

    # --- heartbeat --------------------------------------------------------------

    def report_heartbeat(self) -> None:
        try:
            state = self.auth.status().state
        except Exception:  # noqa: BLE001
            state = "checking"
        try:
            with psycopg.connect(self.config.database_url, row_factory=dict_row, connect_timeout=5) as conn, conn.transaction():
                conn.execute(
                    """
                    INSERT INTO worker_heartbeats (worker_id, kind, version, backend, models, claude_state, heartbeat_at)
                    VALUES (%(id)s, 'minutes', %(version)s, NULL, NULL, %(state)s, now())
                    ON CONFLICT (worker_id) DO UPDATE SET version = EXCLUDED.version, claude_state = EXCLUDED.claude_state,
                        heartbeat_at = now()
                    """,
                    {"id": self.config.worker_id, "version": __version__, "state": state},
                )
        except psycopg.Error:
            logger.warning("heartbeat の記録に失敗しました")

    # --- 1 ジョブ -----------------------------------------------------------------

    def process(self, job: ClaimedJob) -> JobOutcome:
        worker_id = self.config.worker_id
        if not self.queue.mark_running(job, worker_id):
            return JobOutcome(job.job_id, "lease_lost")
        cancel_event = threading.Event()
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, args=(job, cancel_event, heartbeat_stop), daemon=True
        )
        heartbeat_thread.start()
        started = time.monotonic()
        try:
            settings = self._preflight(job)
            result = self._generate(job, settings, cancel_event, deadline=started + job.timeout_seconds)
        except LeaseLostBeforeDispatch:
            heartbeat_stop.set()
            logger.warning("送信開始前に lease を失ったため Claude を呼び出しません job=%s", job.job_id)
            return JobOutcome(job.job_id, "lease_lost")
        except PreflightRejected as rejected:
            heartbeat_stop.set()
            failure = JobFailure(code=rejected.code, stage="minutes", retryable=rejected.retryable, message=str(rejected))
            self.queue.fail(job, worker_id, failure, backoff_seconds=60 if rejected.retryable else 0)
            logger.info("送信前検査で拒否 job=%s code=%s", job.job_id, rejected.code)
            return JobOutcome(job.job_id, "failed", rejected.code)
        except UnknownOutcome as unknown:
            heartbeat_stop.set()
            result = WorkerResult(kind=JobKind.MINUTES_GENERATION, outcome="unknown", artifacts=[])
            self.queue.complete(job, worker_id, result)
            logger.warning("実行結果不明 job=%s: %s", job.job_id, unknown)
            return JobOutcome(job.job_id, "unknown", unknown.code)
        except GenerationError as error:
            heartbeat_stop.set()
            if error.code == "cancelled":
                self.queue.acknowledge_cancel(job, worker_id)
                return JobOutcome(job.job_id, "cancelled", "cancelled")
            if error.code == "claude_rate_limited":
                self.auth.note_rate_limited()
            if error.code == "claude_not_authenticated":
                self.auth.note_not_logged_in()
            failure = JobFailure(
                code=error.code,
                stage="minutes",
                retryable=error.retryable,
                message=str(error),
                exit_code=error.exit_code,
                diagnostics=error.diagnostics or None,
            )
            self.queue.fail(job, worker_id, failure, backoff_seconds=300 if error.code == "claude_rate_limited" else 30)
            logger.info("生成失敗 job=%s code=%s", job.job_id, error.code)
            return JobOutcome(job.job_id, "failed", error.code)
        except Exception as error:
            heartbeat_stop.set()
            logger.exception("予期しないエラー job=%s", job.job_id)
            failure = JobFailure(code="internal", stage="minutes", retryable=True, message=type(error).__name__)
            self.queue.fail(job, worker_id, failure, backoff_seconds=60)
            return JobOutcome(job.job_id, "failed", "internal")
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
        if not self.queue.complete(job, worker_id, result):
            logger.warning("lease を失ったため結果を破棄 job=%s", job.job_id)
            for artifact in result.artifacts:
                self.store.delete(artifact.artifact_id)
            return JobOutcome(job.job_id, "lease_lost")
        logger.info("生成完了 job=%s artifacts=%d", job.job_id, len(result.artifacts))
        return JobOutcome(job.job_id, "succeeded")

    def _heartbeat_loop(self, job: ClaimedJob, cancel_event: threading.Event, stop: threading.Event) -> None:
        """job の lease 更新と、worker 自身の生存通知を並行して出す (R17)。

        worker_heartbeats は jobs の lease とは別テーブルで、capabilities の
        alive 判定に使われる。長時間の生成中に更新が止まると、稼働中の worker が
        利用不可と判定されてしまう。
        """
        interval = self.config.heartbeat_seconds
        while not stop.wait(interval):
            try:
                if self.queue.cancel_requested(job):
                    cancel_event.set()
                if not self.queue.heartbeat(job, self.config.worker_id, self.config.lease_seconds):
                    cancel_event.set()
            except psycopg.Error:
                logger.warning("heartbeat に失敗しました job=%s", job.job_id)
            self.report_heartbeat()

    # --- 送信前検査 ---------------------------------------------------------------

    def _job_settings(self, job: ClaimedJob) -> MinutesJobSettings:
        """producer/consumer 共有の契約として settings を解釈する。

        契約に合わない payload は外部送信の可否を判断できないため、そのまま拒否する。
        """
        try:
            return MinutesJobSettings.model_validate(job.settings or {})
        except Exception as error:
            raise PreflightRejected(
                "invalid_input", f"ジョブ設定が minutes ジョブの契約と一致しません: {type(error).__name__}", retryable=False
            ) from error

    def _preflight(self, job: ClaimedJob) -> MinutesJobSettings:
        settings = self._job_settings(job)
        if not settings.allow_external_send:
            raise PreflightRejected("external_send_forbidden", "このセッションは外部送信禁止です", retryable=False)
        if settings.connected_owner_id is None or str(settings.connected_owner_id) != str(job.owner_id):
            raise PreflightRejected(
                "not_connected_owner", "Claude へ接続した owner 本人のセッションだけ生成できます", retryable=False
            )
        status = self.auth.status(force=True)
        if status.state == "credential_conflict":
            raise PreflightRejected(
                "claude_credential_conflict",
                "API 課金用の認証設定が存在するため Claude 処理を停止しました: " + ", ".join(status.conflict_env_vars),
                retryable=False,
            )
        if status.state in ("cli_missing", "cli_incompatible"):
            raise PreflightRejected("claude_cli_incompatible", status.detail or "Claude CLI を利用できません", retryable=False)
        if status.state == "rate_limited":
            raise PreflightRejected("claude_rate_limited", "Claude の利用上限に到達しています", retryable=True)
        if status.state != "logged_in":
            raise PreflightRejected("claude_not_authenticated", "Claude にログインしていません", retryable=True)
        return settings

    # --- 入力の読み込み --------------------------------------------------------------

    def _load_inputs(self, job: ClaimedJob, settings: MinutesJobSettings) -> tuple[Transcript, FormatProfile, str | None]:
        transcript: Transcript | None = None
        profile: FormatProfile | None = None
        parent_markdown: str | None = None
        for artifact in job.input.get("artifacts", []):
            kind = artifact.get("kind")
            artifact_id = artifact.get("artifact_id")
            if kind == "transcript_json":
                with self.store.open(artifact_id) as handle:
                    transcript = Transcript.model_validate_json(handle.read())
            elif kind == "format_snapshot":
                with self.store.open(artifact_id) as handle:
                    profile = FormatProfile.model_validate_json(handle.read())
            elif kind == "minutes_md":
                with self.store.open(artifact_id) as handle:
                    parent_markdown = handle.read().decode("utf-8")
        if transcript is None:
            raise PreflightRejected("invalid_input", "transcript_json の入力がありません", retryable=False)
        if profile is None and settings.format_snapshot is not None:
            profile = settings.format_snapshot
        if profile is None:
            profile = FormatProfile.model_validate_json(_STANDARD_PROFILE_PATH.read_text(encoding="utf-8"))
        return transcript, profile, parent_markdown

    # --- 生成 -----------------------------------------------------------------------

    def _generate(
        self, job: ClaimedJob, settings: MinutesJobSettings, cancel_event: threading.Event, *, deadline: float
    ) -> WorkerResult:
        transcript, profile, parent_markdown = self._load_inputs(job, settings)
        meta = SessionMeta(
            title=settings.title or "無題の会議",
            started_at=settings.started_at,
            duration_ms=settings.duration_ms,
            input_kind=transcript.input_kind.value,
        )
        instructions = settings.instructions or None
        remaining = lambda: max(5.0, min(self.config.generate_timeout_seconds, deadline - time.monotonic()))

        chunk_requests, final_request = plan_requests(
            transcript,
            profile,
            meta,
            chunk_chars=int(settings.chunk_chars or self.config.chunk_chars),
            base_minutes_markdown=parent_markdown,
            instructions=instructions,
        )
        usage_input = 0
        usage_ms = 0
        model: str | None = None
        chunks = 1
        if chunk_requests:
            notes: list[dict[str, Any]] = []
            chunks = len(chunk_requests)
            for index, request in enumerate(chunk_requests):
                self.queue.progress(job, self.config.worker_id, {"stage": "chunk", "index": index + 1, "total": chunks})
                self._mark_dispatch_started(job)
                generated = self.generator.generate(request, timeout_seconds=remaining(), cancel_event=cancel_event)
                usage_input += generated.input_tokens or 0
                usage_ms += generated.duration_ms
                model = model or generated.model
                notes.extend(generated.structured.get("notes", []))
            final_request = build_final_request(
                transcript,
                profile,
                meta,
                chunk_notes=notes,
                base_minutes_markdown=parent_markdown,
                instructions=instructions,
            )
        assert final_request is not None
        self.queue.progress(job, self.config.worker_id, {"stage": "final", "chunks": chunks})
        self._mark_dispatch_started(job)
        generated = self.generator.generate(final_request, timeout_seconds=remaining(), cancel_event=cancel_event)
        usage_input += generated.input_tokens or 0
        usage_ms += generated.duration_ms
        model = model or generated.model

        known_ids = {segment.id for segment in transcript.segments}
        problems = validate_final_output(generated.structured, profile, known_ids)
        if problems:
            raise GenerationError(
                "claude_output_invalid",
                "生成結果の構造検査に失敗しました",
                retryable=True,
                diagnostics={"problems": problems[:10]},
            )
        title_proposal = str(generated.structured["title"]).strip()[:200]
        # 利用者が編集したタイトルは保護し、未編集のときだけ提案タイトルを採用する (FR-107)
        title_for_render = meta.title if settings.title_edited_by_user else title_proposal or meta.title
        markdown = render_minutes(
            profile,
            generated.structured,
            title=title_for_render,
            started_at=meta.started_at,
            duration_ms=meta.duration_ms,
            segments=ordered_segments(transcript),
        )
        if cancel_event.is_set():
            raise GenerationError("cancelled", "取消要求により確定を中止しました", retryable=False)
        stored = self.store.put_bytes(markdown.encode("utf-8"))
        return WorkerResult(
            kind=JobKind.MINUTES_GENERATION,
            outcome="succeeded",
            artifacts=[
                WorkerResultArtifact(
                    artifact_id=stored.artifact_id,
                    kind="minutes_md",
                    byte_size=stored.byte_size,
                    sha256=stored.sha256,
                    content_type="text/markdown",
                )
            ],
            title_proposal=title_proposal or None,
            insufficient_information=list(generated.structured.get("insufficient_information", [])),
            claude=ClaudeUsage(
                cli_version=self.auth.status().cli_version or "unknown",
                model=model,
                chunks=chunks,
                input_tokens_estimate=usage_input or None,
                duration_ms=usage_ms,
            ),
        )

    def _mark_dispatch_started(self, job: ClaimedJob) -> None:
        """各 CLI 呼出しの直前に確認する。DBへ記録できなければ外部送信しない。"""
        if not self.queue.mark_external_dispatch_started(job, self.config.worker_id):
            raise LeaseLostBeforeDispatch


def build_runner(config: WorkerConfig, auth: ClaudeAuthAdapter | None = None) -> MinutesRunner:
    queue = PostgresJobQueue(config.database_url)
    store = LocalArtifactStore(config.artifacts_dir)
    extra_env = {"AM_MOCK_CLAUDE_SCENARIO": __import__("os").environ.get("AM_MOCK_CLAUDE_SCENARIO", "")} if config.claude_mock else {}
    auth = auth or ClaudeAuthAdapter(
        config.resolved_claude_bin(), config.claude_home, login_timeout_seconds=config.login_timeout_seconds, extra_env=extra_env
    )
    generator = ClaudeGenerationAdapter(
        config.resolved_claude_bin(), config.claude_home, model=config.claude_model, extra_env=extra_env
    )
    return MinutesRunner(config, queue, store, auth, generator)


def environment_summary() -> dict[str, Any]:
    return {"python": platform.python_version(), "machine": platform.machine(), "version": __version__}


__all__ = [
    "JobOutcome",
    "LeaseLostBeforeDispatch",
    "MinutesRunner",
    "PreflightRejected",
    "build_runner",
    "environment_summary",
]
