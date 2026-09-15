"""ジョブ実行ループ (ADR-0003 / ADR-0004)。

* PostgreSQL の jobs から transcription を lease し、子プロセス (spawn) で pipeline を実行する。
* 親は heartbeat (lease/3)、timeout、cancel、lease 喪失、終了コードだけを監視する。
* 成果物は LocalArtifactStore に tmp → commit で書き、WorkerResult を complete する。
* GPU 要求の失敗は AM_GPU_CPU_FALLBACK=1 のときだけ CPU で 1 回だけ再実行し、その結果には
  requested_backend=cpu と fallback_reason を残す (CPU 結果を GPU 結果として返さない)。
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import platform
import signal
import socket
import tempfile
import time
import traceback
from dataclasses import replace
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from audio_minutes_contracts.artifacts import LocalArtifactStore
from audio_minutes_contracts.models import (
    ErrorCode,
    JobFailure,
    JobKind,
    WorkerResult,
    WorkerResultArtifact,
)
from audio_minutes_contracts.queue import ClaimedJob, PostgresJobQueue
from psycopg.types.json import Jsonb

from transcription_worker import __version__
from transcription_worker.audio import AudioInputError
from transcription_worker.config import WorkerConfig
from transcription_worker.engines.base import EngineError
from transcription_worker.models import model_status
from transcription_worker.pipeline.transcribe import (
    TrackInput,
    TranscribeRequest,
    engine_factory,
    model_refs,
    transcribe_request,
)
from transcription_worker.resources import describe_resources

log = logging.getLogger("transcription_worker")

RETRYABLE_CODES = {ErrorCode.ENGINE_CRASHED, ErrorCode.TIMEOUT, ErrorCode.INTERNAL}
NON_RETRYABLE_CODES = {
    ErrorCode.INVALID_INPUT,
    ErrorCode.UNSUPPORTED_FORMAT,
    ErrorCode.LIMIT_EXCEEDED,
    ErrorCode.UNSUPPORTED_MODEL,
    ErrorCode.DECODE_FAILED,
    ErrorCode.GPU_UNAVAILABLE,
    ErrorCode.BACKEND_ERROR,
    ErrorCode.CANCELLED,
}
GPU_FALLBACK_CODES = {ErrorCode.GPU_UNAVAILABLE, ErrorCode.BACKEND_ERROR}
CRASH_EXIT_CODES = {-11, 139, -6, 134, -9, 137}


def classify_failure(code: str, message: str, *, exit_code: int | None = None, diagnostics: dict[str, Any] | None = None) -> JobFailure:
    """失敗コードから retryable を決める。invalid_input / unsupported_model は再試行しない。"""
    try:
        error_code = ErrorCode(code)
    except ValueError:
        error_code = ErrorCode.INTERNAL
    retryable = error_code in RETRYABLE_CODES
    return JobFailure(
        code=error_code,
        stage="transcription",
        retryable=retryable,
        message=message,
        exit_code=exit_code,
        retained_artifacts=[],
        diagnostics=diagnostics,
    )


def classify_exit_code(exit_code: int) -> JobFailure:
    if exit_code in CRASH_EXIT_CODES:
        return classify_failure(
            "engine_crashed",
            f"推論プロセスが異常終了しました (exit={exit_code})",
            exit_code=exit_code,
            diagnostics={"signal": signal.Signals(-exit_code).name if exit_code < 0 else None},
        )
    return classify_failure("internal", f"推論プロセスが失敗しました (exit={exit_code})", exit_code=exit_code)


# --- 子プロセス ----------------------------------------------------------------


def request_from_job(job: ClaimedJob, store: LocalArtifactStore, config: WorkerConfig, fallback_reason: str | None) -> TranscribeRequest:
    settings = job.settings
    tracks: list[TrackInput] = []
    for artifact in job.input.get("artifacts", []):
        if artifact.get("kind") != "audio_track":
            continue
        artifact_id = artifact["artifact_id"]
        if not store.exists(artifact_id):
            raise AudioInputError("invalid_input", f"入力音声の artifact がありません: {artifact_id}")
        tracks.append(
            TrackInput(
                track_id=artifact["track_id"],
                role=artifact.get("role") or "mixed",
                path=store.path(artifact_id),
                source_artifact_id=artifact_id,
                start_offset_ms=int(artifact.get("start_offset_ms") or 0),
            )
        )
    return TranscribeRequest(
        session_id=job.session_id,
        revision=int(settings.get("transcript_revision") or job.input.get("transcript_revision") or 1),
        input_kind=settings.get("input_kind") or ("recorded_dual_track" if len(tracks) == 2 else "imported_mixed"),
        language_mode=settings.get("language_mode") or "auto",
        tracks=tracks,
        strategy=settings.get("strategy") or config.strategy,
        requested_backend=config.backend,
        fallback_reason=fallback_reason,
    )


def child_main(
    job_payload: dict[str, Any],
    overrides: dict[str, Any],
    fallback_reason: str | None,
    result_path: str,
    failure_path: str,
    progress_path: str,
) -> None:
    """子プロセス本体。成功なら result_path に WorkerResult、既知の失敗なら failure_path に JobFailure を書く。

    `overrides` は WorkerConfig の項目だけ (dataclasses.replace へ渡す)。fallback の理由は
    設定ではなく結果メタデータなので、混ぜずに別引数で受け取る (R14)。
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s child %(message)s")
    try:
        config = WorkerConfig.from_env()
        if overrides:
            config = replace(config, **overrides)
        store = LocalArtifactStore(config.artifacts_dir)
        job = ClaimedJob(
            job_id=UUID(job_payload["job_id"]),
            kind=JobKind(job_payload["kind"]),
            session_id=UUID(job_payload["session_id"]),
            owner_id=UUID(job_payload["owner_id"]),
            input=job_payload["input"],
            settings=job_payload["settings"],
            attempt=job_payload["attempt"],
            max_attempts=job_payload["max_attempts"],
            timeout_seconds=job_payload["timeout_seconds"],
            lease_expires_at=datetime.now(UTC),
            revision=job_payload["revision"],
        )
        request = request_from_job(job, store, config, fallback_reason)

        def progress(detail: dict[str, Any]) -> None:
            Path(progress_path).write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")

        transcript = transcribe_request(request, config, progress=progress)
        json_bytes = transcript.model_dump_json(indent=None).encode("utf-8")
        md_bytes = transcript.to_markdown().encode("utf-8")
        json_stored = store.put_bytes(json_bytes)
        md_stored = store.put_bytes(md_bytes)
        result = WorkerResult(
            kind=JobKind.TRANSCRIPTION,
            outcome="succeeded",
            artifacts=[
                WorkerResultArtifact(
                    artifact_id=json_stored.artifact_id,
                    kind="transcript_json",
                    byte_size=json_stored.byte_size,
                    sha256=json_stored.sha256,
                    content_type="application/json",
                ),
                WorkerResultArtifact(
                    artifact_id=md_stored.artifact_id,
                    kind="transcript_md",
                    byte_size=md_stored.byte_size,
                    sha256=md_stored.sha256,
                    content_type="text/markdown",
                ),
            ],
            processing=transcript.processing,
        )
        Path(result_path).write_text(result.model_dump_json(), encoding="utf-8")
    except AudioInputError as error:
        _write_failure(failure_path, classify_failure(error.code, str(error)))
        raise SystemExit(1) from error
    except EngineError as error:
        _write_failure(
            failure_path,
            classify_failure(error.code, str(error), diagnostics={"engine_error": type(error).__name__}).model_copy(
                update={"retryable": error.retryable}
            ),
        )
        raise SystemExit(1) from error
    except Exception as error:  # noqa: BLE001 - 想定外は internal として記録し stderr へ traceback
        log.error("想定外の失敗: %s", error)
        traceback.print_exc()
        _write_failure(failure_path, classify_failure("internal", "文字起こし処理で想定外のエラーが発生しました"))
        raise SystemExit(1) from error


def _write_failure(path: str, failure: JobFailure) -> None:
    Path(path).write_text(failure.model_dump_json(), encoding="utf-8")


# --- 親プロセス ----------------------------------------------------------------


class Runner:
    def __init__(self, config: WorkerConfig, queue: PostgresJobQueue | None = None) -> None:
        self.config = config
        self.queue = queue or PostgresJobQueue(config.database_url)
        self.worker_id = config.worker_id or f"transcription-{socket.gethostname()}"
        self.store = LocalArtifactStore(config.artifacts_dir)
        self._stop = False
        self._last_table_heartbeat = 0.0
        self.startup_backend: dict[str, Any] = {}
        self.model_readiness: list[dict[str, Any]] = []

    # --- 起動時検証 -------------------------------------------------------------

    def verify_startup(self) -> dict[str, Any]:
        """CPU arch / count / memory と engine・backend を検証する。CPU profile では GPU を初期化しない。"""
        resources = describe_resources(self.config.threads)
        readiness = model_status(self.config.models_dir, self.config, check_hashes=True)
        self.model_readiness = [
            {
                "profile": model["profile"],
                "model_id": model["model_id"],
                "model_revision": model["revision"],
                "ready": readiness["ready"],
            }
            for model in readiness["models"]
        ]
        if not readiness["ready"]:
            self.startup_backend = {
                "requested_backend": self.config.backend,
                "effective_backend": "unavailable",
                "gpu_verified": False,
                "ready": False,
                "readiness_error": "; ".join(readiness["errors"]),
            }
            raise RuntimeError(self.startup_backend["readiness_error"])
        factory = engine_factory(self.config)
        probe_engine = factory(model_refs(self.config)["multilingual"])
        capabilities = probe_engine.probe()
        if self.config.backend == "cpu" and capabilities.backend.effective_backend not in ("cpu", "unknown"):
            raise RuntimeError("CPU profile なのに GPU backend が初期化されています")
        if self.config.backend == "vulkan" and "vulkan" not in capabilities.supported_backends:
            raise RuntimeError("engine が Vulkan に対応していません")
        modes = capabilities.supports_language_modes()
        if not all(modes.values()):
            log.warning("この engine は一部の言語モードに未対応です: %s", modes)
        info = {
            "resources": json.loads(resources.model_dump_json()),
            "engine": capabilities.engine,
            "engine_version": capabilities.engine_version,
            "requested_backend": self.config.backend,
            "capabilities": {
                "language_detection": capabilities.language_detection,
                "language_probability": capabilities.language_probability,
                "word_timestamps": capabilities.word_timestamps,
                "language_modes": modes,
            },
            "notes": list(capabilities.notes),
        }
        self.startup_backend = {
            "requested_backend": self.config.backend,
            "effective_backend": capabilities.backend.effective_backend,
            "gpu_verified": capabilities.backend.gpu_verified,
            "ready": True,
            "offline": self.config.hf_offline,
            "engine": capabilities.engine,
            "engine_version": capabilities.engine_version,
        }
        log.info("起動検証: %s", json.dumps(info, ensure_ascii=False))
        return info

    # --- heartbeat テーブル -------------------------------------------------------

    def upsert_heartbeat(self, claude_state: str | None = None) -> None:
        models = self.model_readiness or [
            {
                "profile": profile,
                "model_id": ref.model_id,
                "revision": ref.revision,
                "ready": False,
            }
            for profile, ref in model_refs(self.config).items()
        ]
        try:
            with psycopg.connect(self.config.database_url, connect_timeout=5) as conn, conn.transaction():
                conn.execute(
                    """
                    INSERT INTO worker_heartbeats (worker_id, kind, version, backend, models, claude_state, heartbeat_at)
                    VALUES (%s, 'transcription', %s, %s, %s, %s, now())
                    ON CONFLICT (worker_id) DO UPDATE SET
                        version = EXCLUDED.version, backend = EXCLUDED.backend, models = EXCLUDED.models,
                        heartbeat_at = now()
                    """,
                    (self.worker_id, __version__, Jsonb(self.startup_backend), Jsonb(models), claude_state),
                )
        except psycopg.Error as error:
            log.warning("heartbeat 更新に失敗: %s", error)

    # --- ループ ---------------------------------------------------------------

    def stop(self, *_: Any) -> None:
        self._stop = True

    def update_in_progress(self) -> bool:
        return self.config.update_maintenance_file.exists()

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        try:
            self.verify_startup()
        except Exception:
            # model 未取得・破損時も heartbeat に unavailable と理由を残して終了する。
            # stale な過去 heartbeat を capabilities が available と誤認しないための境界。
            self.upsert_heartbeat()
            raise
        while not self._stop:
            now = time.monotonic()
            if now - self._last_table_heartbeat >= self.config.heartbeat_table_seconds:
                self.upsert_heartbeat()
                self._last_table_heartbeat = now
            if self.update_in_progress():
                time.sleep(self.config.poll_seconds)
                continue
            try:
                job = self.queue.claim(["transcription"], self.worker_id, self.config.lease_seconds)
            except psycopg.Error as error:
                log.warning("claim に失敗: %s", error)
                time.sleep(self.config.poll_seconds)
                continue
            if job is None:
                time.sleep(self.config.poll_seconds)
                continue
            self.process_job(job)

    def process_job(
        self,
        job: ClaimedJob,
        *,
        overrides: dict[str, Any] | None = None,
        fallback_reason: str | None = None,
        fallback_done: bool = False,
    ) -> str:
        """1 ジョブを子プロセスで実行し、結果を queue に反映する。戻り値は結果種別 (テスト用)。"""
        log.info("job %s attempt %s を開始", job.job_id, job.attempt)
        if not self.queue.mark_running(job, self.worker_id):
            log.warning("lease を失ったため job %s を開始しません", job.job_id)
            return "lease_lost"

        payload = {
            "job_id": str(job.job_id),
            "kind": job.kind.value,
            "session_id": str(job.session_id),
            "owner_id": str(job.owner_id),
            "input": job.input,
            "settings": job.settings,
            "attempt": job.attempt,
            "max_attempts": job.max_attempts,
            "timeout_seconds": job.timeout_seconds,
            "revision": job.revision,
        }
        with tempfile.TemporaryDirectory(prefix="am-job-") as tmp:
            result_path = os.path.join(tmp, "result.json")
            failure_path = os.path.join(tmp, "failure.json")
            progress_path = os.path.join(tmp, "progress.json")
            context = mp.get_context("spawn")
            child = context.Process(
                target=child_main,
                args=(payload, overrides or {}, fallback_reason, result_path, failure_path, progress_path),
                name=f"transcribe-{job.job_id}",
            )
            started = time.monotonic()
            child.start()
            heartbeat_interval = max(1.0, self.config.lease_seconds / 3)
            last_progress = None
            outcome = "unknown"
            while True:
                child.join(timeout=heartbeat_interval)
                # job の lease とは別に worker 自身の生存通知も出す。長時間の文字起こし中に
                # 更新が止まると、capabilities が稼働中の worker を alive=false と判定する (R17)
                now = time.monotonic()
                if now - self._last_table_heartbeat >= self.config.heartbeat_table_seconds:
                    self.upsert_heartbeat()
                    self._last_table_heartbeat = now
                if not child.is_alive():
                    break
                elapsed = time.monotonic() - started
                if elapsed > job.timeout_seconds:
                    self._terminate(child)
                    failure = classify_failure("timeout", f"工程 timeout ({job.timeout_seconds}s) を超えました")
                    self.queue.fail(job, self.worker_id, failure, backoff_seconds=30)
                    log.warning("job %s timeout", job.job_id)
                    return "timeout"
                if self.queue.cancel_requested(job):
                    self._terminate(child)
                    self.queue.acknowledge_cancel(job, self.worker_id)
                    log.info("job %s を取消", job.job_id)
                    return "cancelled"
                if not self.queue.heartbeat(job, self.worker_id, self.config.lease_seconds):
                    self._terminate(child)
                    log.warning("job %s の lease を失ったため中断 (結果は捨てる)", job.job_id)
                    return "lease_lost"
                progress = _read_json(progress_path)
                if progress and progress != last_progress:
                    self.queue.progress(job, self.worker_id, progress)
                    last_progress = progress

            exit_code = child.exitcode if child.exitcode is not None else -1
            result_doc = _read_json(result_path)
            failure_doc = _read_json(failure_path)
            if exit_code == 0 and result_doc:
                result = WorkerResult.model_validate(result_doc)
                if self.queue.complete(job, self.worker_id, result):
                    log.info("job %s 完了 (artifacts=%d)", job.job_id, len(result.artifacts))
                    outcome = "succeeded"
                else:
                    log.warning("job %s の完了を拒否された (古い attempt)", job.job_id)
                    for artifact in result.artifacts:
                        self.store.delete(artifact.artifact_id)
                    outcome = "lease_lost"
                return outcome

            failure = JobFailure.model_validate(failure_doc) if failure_doc else classify_exit_code(exit_code)
            if (
                failure.code in GPU_FALLBACK_CODES
                and self.config.gpu_cpu_fallback
                and self.config.backend != "cpu"
                and not fallback_done
            ):
                log.warning("GPU 実行に失敗 (%s)。設定により CPU で 1 回だけ再実行します", failure.code)
                self.queue.progress(
                    job,
                    self.worker_id,
                    {"cpu_fallback": True, "gpu_failure": json.loads(failure.model_dump_json())},
                )
                return self.process_job(
                    job,
                    overrides={"backend": "cpu"},
                    fallback_reason=f"gpu:{failure.code}",
                    fallback_done=True,
                )
            self.queue.fail(job, self.worker_id, failure, backoff_seconds=30 if failure.retryable else 0)
            log.warning("job %s 失敗: %s (%s)", job.job_id, failure.code, failure.message)
            return "failed"

    @staticmethod
    def _terminate(child: mp.process.BaseProcess) -> None:
        child.terminate()
        child.join(timeout=10)
        if child.is_alive():
            child.kill()
            child.join(timeout=5)


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def main() -> int:
    config = WorkerConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not config.database_url:
        log.error("AM_WORKER_DATABASE_URL が未設定です")
        return 2
    os.environ.setdefault("HF_HOME", str(config.models_dir))
    if config.hf_offline:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        contracts_version = _package_version("audio-minutes-contracts")
    except PackageNotFoundError:
        contracts_version = "unknown"
    log.info("transcription-worker %s (contracts %s) %s/%s", __version__, contracts_version, platform.machine(), config.engine)
    runner = Runner(config)
    runner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
