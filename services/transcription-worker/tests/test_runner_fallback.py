"""親プロセスの制御 (GPU→CPU fallback・取消・lease 喪失・生存通知) を GPU 実機なしで検証する。

子プロセスは偽物に差し替え、結果/失敗ファイルの書き込みと終了コードだけを再現する。
モデルも GPU も使わないので、CPU profile の CI でそのまま動く。
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from audio_minutes_contracts.models import JobFailure, JobKind, WorkerResult
from audio_minutes_contracts.queue import ClaimedJob

from transcription_worker.config import WorkerConfig
from transcription_worker.runner import Runner


class FakeQueue:
    def __init__(self) -> None:
        self.mark_running_ok = True
        self.heartbeat_ok = True
        self.cancel = False
        self.completed: list[WorkerResult] = []
        self.failures: list[JobFailure] = []
        self.progress_events: list[dict[str, Any]] = []
        self.acknowledged_cancel = 0

    def mark_running(self, job: ClaimedJob, worker_id: str) -> bool:
        return self.mark_running_ok

    def heartbeat(self, job: ClaimedJob, worker_id: str, lease_seconds: int) -> bool:
        return self.heartbeat_ok

    def cancel_requested(self, job: ClaimedJob) -> bool:
        return self.cancel

    def acknowledge_cancel(self, job: ClaimedJob, worker_id: str) -> bool:
        self.acknowledged_cancel += 1
        return True

    def complete(self, job: ClaimedJob, worker_id: str, result: WorkerResult) -> bool:
        self.completed.append(result)
        return True

    def fail(self, job: ClaimedJob, worker_id: str, failure: JobFailure, backoff_seconds: int = 0) -> bool:
        self.failures.append(failure)
        return True

    def progress(self, job: ClaimedJob, worker_id: str, detail: dict[str, Any]) -> None:
        self.progress_events.append(detail)


class FakeProcess:
    """`child_main` を実行せず、台本どおりの結果ファイルと exitcode を作る。"""

    script: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    def __init__(self, target, args, name: str = "") -> None:  # noqa: ANN001 - mp.Process 互換
        payload, overrides, fallback_reason, result_path, failure_path, _progress_path = args
        self._result_path = Path(result_path)
        self._failure_path = Path(failure_path)
        self._step = dict(FakeProcess.script[len(FakeProcess.calls)])
        FakeProcess.calls.append(
            {"overrides": overrides, "fallback_reason": fallback_reason, "attempt": payload["attempt"]}
        )
        self.exitcode: int | None = None
        self._alive = False

    def start(self) -> None:
        self._alive = True

    def join(self, timeout: float | None = None) -> None:
        if self._step.get("result") is not None:
            self._result_path.write_text(json.dumps(self._step["result"]), encoding="utf-8")
        if self._step.get("failure") is not None:
            self._failure_path.write_text(json.dumps(self._step["failure"]), encoding="utf-8")
        self.exitcode = self._step.get("exitcode", 0)
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self._alive = False

    def kill(self) -> None:
        self._alive = False


class HangingProcess(FakeProcess):
    """join しても終わらない子。取消・lease 喪失の経路を通すために使う。"""

    def join(self, timeout: float | None = None) -> None:
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive


def _result_doc() -> dict[str, Any]:
    return {
        "schema_version": "worker-result/1",
        "kind": "transcription",
        "outcome": "succeeded",
        "artifacts": [
            {"artifact_id": "art_01J7Q0AAAAAAAAAAAAAAAAAAA1", "kind": "transcript_json", "byte_size": 10,
             "sha256": "a" * 64, "content_type": "application/json"}
        ],
        "insufficient_information": [],
    }


def _failure_doc(code: str) -> dict[str, Any]:
    return {"code": code, "stage": "transcription", "retryable": True, "message": f"{code} で失敗", "retained_artifacts": []}


def _job() -> ClaimedJob:
    return ClaimedJob(
        job_id=uuid.uuid4(),
        kind=JobKind.TRANSCRIPTION,
        session_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        input={"artifacts": []},
        settings={"input_kind": "recorded_dual_track", "language_mode": "auto", "transcript_revision": 1,
                  "max_audio_ms": 14_400_000},
        attempt=1,
        max_attempts=3,
        timeout_seconds=600,
        lease_expires_at=datetime.now(UTC),
        revision=0,
    )


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Runner:
    config = WorkerConfig(
        database_url="postgresql://unused",
        artifacts_dir=tmp_path / "artifacts",
        models_dir=tmp_path / "models",
        engine="whisper.cpp",
        backend="vulkan",
        gpu_cpu_fallback=True,
        worker_id="transcription-test",
        lease_seconds=6,
    )
    runner = Runner(config, queue=FakeQueue())
    monkeypatch.setattr(runner, "upsert_heartbeat", lambda *args, **kwargs: None)
    FakeProcess.calls = []
    return runner


def _install(monkeypatch: pytest.MonkeyPatch, process_class: type[FakeProcess], script: list[dict[str, Any]]) -> None:
    FakeProcess.script = script
    FakeProcess.calls = []

    class Context:
        Process = process_class

    monkeypatch.setattr("transcription_worker.runner.mp.get_context", lambda _name: Context())


def test_gpu_failure_falls_back_to_cpu_once(runner: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        FakeProcess,
        [
            {"failure": _failure_doc("gpu_unavailable"), "exitcode": 1},
            {"result": _result_doc(), "exitcode": 0},
        ],
    )
    assert runner.process_job(_job()) == "succeeded"
    assert len(FakeProcess.calls) == 2
    # 1 回目は GPU のまま、2 回目だけ CPU。fallback 理由は WorkerConfig へ混ぜない (R14)
    assert FakeProcess.calls[0]["overrides"] == {} and FakeProcess.calls[0]["fallback_reason"] is None
    assert FakeProcess.calls[1]["overrides"] == {"backend": "cpu"}
    assert FakeProcess.calls[1]["fallback_reason"] == "gpu:gpu_unavailable"
    assert runner.queue.completed and not runner.queue.failures
    assert any(event.get("cpu_fallback") for event in runner.queue.progress_events)


def test_update_maintenance_marker_stops_claim(runner: Runner, tmp_path: Path) -> None:
    marker = tmp_path / ".update-maintenance"
    runner.config = dataclasses.replace(runner.config, update_maintenance_file=marker)
    assert runner.update_in_progress() is False
    marker.touch()
    assert runner.update_in_progress() is True


def test_cpu_fallback_failure_is_not_retried_again(runner: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        FakeProcess,
        [
            {"failure": _failure_doc("gpu_unavailable"), "exitcode": 1},
            {"failure": _failure_doc("engine_crashed"), "exitcode": 1},
        ],
    )
    assert runner.process_job(_job()) == "failed"
    assert len(FakeProcess.calls) == 2, "CPU fallback は最大 1 回"
    assert [failure.code for failure in runner.queue.failures] == ["engine_crashed"]


def test_fallback_overrides_are_valid_worker_config_fields(runner: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    """overrides が dataclasses.replace へそのまま渡せること (TypeError の再発防止)。"""
    _install(
        monkeypatch,
        FakeProcess,
        [
            {"failure": _failure_doc("gpu_unavailable"), "exitcode": 1},
            {"result": _result_doc(), "exitcode": 0},
        ],
    )
    runner.process_job(_job())
    overrides = FakeProcess.calls[1]["overrides"]
    replaced = dataclasses.replace(runner.config, **overrides)
    assert replaced.backend == "cpu"


def test_cancel_during_run_is_acknowledged(runner: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, HangingProcess, [{"result": None, "exitcode": 0}])
    runner.queue.cancel = True
    assert runner.process_job(_job()) == "cancelled"
    assert runner.queue.acknowledged_cancel == 1
    assert not runner.queue.completed


def test_lease_loss_during_run_discards_result(runner: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, HangingProcess, [{"result": None, "exitcode": 0}])
    runner.queue.heartbeat_ok = False
    assert runner.process_job(_job()) == "lease_lost"
    assert not runner.queue.completed and not runner.queue.failures


def test_worker_heartbeat_is_refreshed_while_a_job_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """180 秒を超える処理でも alive のままにする (R17)。

    実時間を使わず、lease heartbeat の周回ごとに仮想時計を進めて確認する。
    """
    config = WorkerConfig(
        database_url="postgresql://unused",
        artifacts_dir=tmp_path / "artifacts",
        models_dir=tmp_path / "models",
        worker_id="transcription-test",
        lease_seconds=6,
        heartbeat_table_seconds=15.0,
    )
    runner = Runner(config, queue=FakeQueue())
    beats: list[float] = []
    clock = {"now": 0.0}

    class SlowProcess(FakeProcess):
        def join(self, timeout: float | None = None) -> None:
            clock["now"] += 60.0  # 仮想時計で 1 分進める
            if clock["now"] >= 240.0:  # 180 秒を超えて動き続けたあとに終了する
                super().join(timeout)
            else:
                self._alive = True

    monkeypatch.setattr("transcription_worker.runner.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(runner, "upsert_heartbeat", lambda *args, **kwargs: beats.append(clock["now"]))
    _install(monkeypatch, SlowProcess, [{"result": _result_doc(), "exitcode": 0}])

    assert runner.process_job(_job()) == "succeeded"
    assert len(beats) >= 3, f"実行中に生存通知が更新されていない: {beats}"
    assert max(beats) >= 180.0
