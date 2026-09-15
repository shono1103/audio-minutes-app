from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from audio_minutes_contracts.artifacts import LocalArtifactStore
from audio_minutes_contracts.models import FormatProfile, MinutesJobSettings, Transcript

from minutes_worker.claude_auth import ClaudeAuthAdapter
from minutes_worker.claude_generate import ClaudeGenerationAdapter
from minutes_worker.config import WorkerConfig
from minutes_worker.runner import MinutesRunner
from tests.conftest import FIXTURES, MOCK_BIN
from tests.fake_queue import FakeQueue

OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


def make_runner(claude_home, clean_environ, store, scenario: str, **config_overrides) -> tuple[MinutesRunner, FakeQueue]:
    extra = {"AM_MOCK_CLAUDE_SCENARIO": scenario}
    config_values = {
        "database_url": "postgresql://unused",
        "artifacts_dir": store.root,
        "claude_bin": MOCK_BIN,
        "claude_home": claude_home,
        "worker_id": "minutes-test",
        "lease_seconds": 30,
    }
    config_values.update(config_overrides)
    config = WorkerConfig(**config_values)
    auth = ClaudeAuthAdapter(MOCK_BIN, claude_home, environ=clean_environ, extra_env=extra, status_cache_seconds=0)
    generator = ClaudeGenerationAdapter(MOCK_BIN, claude_home, environ=clean_environ, extra_env=extra)
    queue = FakeQueue()
    return MinutesRunner(config, queue, store, auth, generator), queue


def seed_inputs(store: LocalArtifactStore, *, with_profile_artifact: bool = False, parent: str | None = None) -> list[dict]:
    transcript_bytes = (FIXTURES / "transcript.dual-track.json").read_bytes()
    artifacts = [{"artifact_id": store.put_bytes(transcript_bytes).artifact_id, "kind": "transcript_json"}]
    if with_profile_artifact:
        artifacts.append(
            {"artifact_id": store.put_bytes((FIXTURES / "format-profile.standard.json").read_bytes()).artifact_id, "kind": "format_snapshot"}
        )
    if parent is not None:
        artifacts.append({"artifact_id": store.put_bytes(parent.encode()).artifact_id, "kind": "minutes_md"})
    return artifacts


def base_settings(**overrides) -> dict:
    """API が実際に作る payload と同じ契約 (MinutesJobSettings) で組み立てる。

    ここで直接 dict を書かないことで、producer 側が契約を変えたら consumer 側の
    試験も同時に壊れる (R05 / R09)。
    """
    fields = {
        "kind": "claude_generated",
        "allow_external_send": True,
        "connected_owner_id": str(OWNER),
        "title": "週次定例 (仮)",
        "title_edited_by_user": False,
        "input_kind": "recorded_dual_track",
        "started_at": datetime(2026, 9, 12, 10, 0, tzinfo=UTC).isoformat(),
        "duration_ms": 1_800_000,
        "transcript_revision": 1,
    }
    fields.update(overrides)
    return json.loads(MinutesJobSettings.model_validate(fields).model_dump_json())


def test_success_path_creates_minutes_artifact(claude_home, clean_environ, store):
    runner, queue = make_runner(claude_home, clean_environ, store, "logged_in")
    job = queue.add(owner_id=OWNER, input={"artifacts": seed_inputs(store, with_profile_artifact=True)}, settings=base_settings())
    outcome = runner.process(job)
    assert outcome.status == "succeeded"
    result = queue.jobs[job.job_id]["result"]
    assert result.outcome == "succeeded" and result.kind == "minutes_generation"
    assert result.title_proposal == "モック議事録: ロードマップ確認"
    assert result.claude.cli_version == "2.1.268" and result.claude.chunks == 1
    artifact = result.artifacts[0]
    assert artifact.kind == "minutes_md"
    markdown = store.open(artifact.artifact_id).read().decode()
    assert markdown.startswith("# モック議事録: ロードマップ確認")
    assert "## 要約" in markdown and "## 決定事項" in markdown and "`seg-0001`" in markdown
    assert store.stat(artifact.artifact_id).sha256 == artifact.sha256
    assert not any((store.root / "tmp").iterdir())
    assert queue.jobs[job.job_id]["dispatch_started"] is True


def test_update_maintenance_does_not_claim_job(claude_home, clean_environ, store, tmp_path):
    marker = tmp_path / ".update-maintenance"
    marker.touch()
    runner, queue = make_runner(
        claude_home,
        clean_environ,
        store,
        "logged_in",
        update_maintenance_file=marker,
    )
    called = False

    def claim(*_args, **_kwargs):
        nonlocal called
        called = True

    queue.claim = claim
    assert runner.run_once() is None
    assert called is False


def test_title_edited_by_user_is_not_overwritten_in_body(claude_home, clean_environ, store):
    runner, queue = make_runner(claude_home, clean_environ, store, "logged_in")
    job = queue.add(owner_id=OWNER, input={"artifacts": seed_inputs(store)}, settings=base_settings(title_edited_by_user=True))
    assert runner.process(job).status == "succeeded"
    result = queue.jobs[job.job_id]["result"]
    markdown = store.open(result.artifacts[0].artifact_id).read().decode()
    assert markdown.startswith("# 週次定例 (仮)")
    assert result.title_proposal == "モック議事録: ロードマップ確認"  # API 側が未編集時だけ採用する


def test_insufficient_information_propagates(claude_home, clean_environ, store):
    runner, queue = make_runner(claude_home, clean_environ, store, "insufficient")
    job = queue.add(owner_id=OWNER, input={"artifacts": seed_inputs(store, parent="# 旧版")}, settings=base_settings(instructions="予算の議論を詳しく"))
    assert runner.process(job).status == "succeeded"
    result = queue.jobs[job.job_id]["result"]
    assert result.insufficient_information == ["予算に関する議論は文字起こしに含まれていません"]
    markdown = store.open(result.artifacts[0].artifact_id).read().decode()
    assert "## 情報不足" in markdown


@pytest.mark.parametrize(
    "settings_override, scenario, code, retryable",
    [
        ({"allow_external_send": False}, "logged_in", "external_send_forbidden", False),
        ({"connected_owner_id": str(uuid.uuid4())}, "logged_in", "not_connected_owner", False),
        ({}, "logged_out", "claude_not_authenticated", True),
    ],
)
def test_preflight_rejections(claude_home, clean_environ, store, settings_override, scenario, code, retryable):
    runner, queue = make_runner(claude_home, clean_environ, store, scenario)
    job = queue.add(owner_id=OWNER, input={"artifacts": seed_inputs(store)}, settings=base_settings(**settings_override))
    outcome = runner.process(job)
    assert outcome.status == "failed" and outcome.code == code
    failure = queue.jobs[job.job_id]["failure"]
    assert failure.code == code and failure.retryable is retryable and failure.stage == "minutes"
    assert queue.jobs[job.job_id]["status"] == ("queued" if retryable else "failed")
    assert list(store.iter_ids()) == [artifact["artifact_id"] for artifact in job.input["artifacts"]]


def test_credential_conflict_blocks_and_hides_value(claude_home, clean_environ, store):
    environ = {**clean_environ, "ANTHROPIC_AUTH_TOKEN": "super-secret-token-value"}
    runner, queue = make_runner(claude_home, environ, store, "logged_in")
    job = queue.add(owner_id=OWNER, input={"artifacts": seed_inputs(store)}, settings=base_settings())
    outcome = runner.process(job)
    assert outcome.code == "claude_credential_conflict"
    failure = queue.jobs[job.job_id]["failure"]
    assert "ANTHROPIC_AUTH_TOKEN" in failure.message and "super-secret" not in failure.model_dump_json()


def test_hang_becomes_unknown_outcome_without_artifact(claude_home, clean_environ, store):
    runner, queue = make_runner(claude_home, clean_environ, store, "hang", generate_timeout_seconds=1)
    job = queue.add(owner_id=OWNER, input={"artifacts": seed_inputs(store)}, settings=base_settings(), timeout_seconds=1)
    outcome = runner.process(job)
    assert outcome.status == "unknown"
    result = queue.jobs[job.job_id]["result"]
    assert result.outcome == "unknown" and result.artifacts == []
    assert queue.jobs[job.job_id]["dispatch_started"] is True


def test_lease_lost_while_persisting_dispatch_does_not_call_claude(claude_home, clean_environ, store, monkeypatch):
    runner, queue = make_runner(claude_home, clean_environ, store, "logged_in")
    job = queue.add(owner_id=OWNER, input={"artifacts": seed_inputs(store)}, settings=base_settings())
    queue.lose_on_dispatch.add(job.job_id)
    calls: list[object] = []
    monkeypatch.setattr(runner.generator, "generate", lambda *args, **kwargs: calls.append(args))

    assert runner.process(job).status == "lease_lost"
    assert calls == []
    assert queue.jobs[job.job_id]["dispatch_started"] is False


@pytest.mark.parametrize("scenario, code", [("rate_limited", "claude_rate_limited"), ("invalid_json", "claude_output_invalid")])
def test_generation_errors_recorded(claude_home, clean_environ, store, scenario, code):
    runner, queue = make_runner(claude_home, clean_environ, store, scenario)
    job = queue.add(owner_id=OWNER, input={"artifacts": seed_inputs(store)}, settings=base_settings())
    outcome = runner.process(job)
    assert outcome.status == "failed" and outcome.code == code
    if scenario == "rate_limited":
        assert runner.auth.status(force=True).state == "rate_limited"


def test_long_transcript_is_chunked_and_references_kept(claude_home, clean_environ, store):
    runner, queue = make_runner(claude_home, clean_environ, store, "logged_in")
    job = queue.add(owner_id=OWNER, input={"artifacts": seed_inputs(store)}, settings=base_settings(chunk_chars=120))
    assert runner.process(job).status == "succeeded"
    result = queue.jobs[job.job_id]["result"]
    assert result.claude.chunks >= 2
    stages = [detail["stage"] for event, detail in queue.events if event == "progress"]
    assert stages.count("chunk") == result.claude.chunks and stages[-1] == "final"
    markdown = store.open(result.artifacts[0].artifact_id).read().decode()
    assert "`seg-" in markdown  # 中間メモ経由でも根拠 segment ID が最終版に残る


def test_cancel_during_generation(claude_home, clean_environ, store):
    runner, queue = make_runner(claude_home, clean_environ, store, "hang", lease_seconds=3)
    job = queue.add(owner_id=OWNER, input={"artifacts": seed_inputs(store)}, settings=base_settings(), timeout_seconds=60)
    queue.cancel_flags.add(job.job_id)
    outcome = runner.process(job)
    assert outcome.status == "cancelled" and queue.jobs[job.job_id]["status"] == "cancelled"


def test_lease_lost_discards_result(claude_home, clean_environ, store):
    runner, queue = make_runner(claude_home, clean_environ, store, "logged_in")
    job = queue.add(owner_id=OWNER, input={"artifacts": seed_inputs(store)}, settings=base_settings())
    queue.lost_lease.add(job.job_id)
    assert runner.process(job).status == "lease_lost"


def test_missing_transcript_input(claude_home, clean_environ, store):
    runner, queue = make_runner(claude_home, clean_environ, store, "logged_in")
    job = queue.add(owner_id=OWNER, input={"artifacts": []}, settings=base_settings())
    outcome = runner.process(job)
    assert outcome.status == "failed" and outcome.code == "invalid_input"


def test_standard_profile_fallback_matches_fixture(claude_home, clean_environ, store):
    runner, _ = make_runner(claude_home, clean_environ, store, "logged_in")
    transcript_id = store.put_bytes((FIXTURES / "transcript.dual-track.json").read_bytes()).artifact_id
    from tests.fake_queue import FakeQueue

    job = FakeQueue().add(owner_id=OWNER, input={"artifacts": [{"artifact_id": transcript_id, "kind": "transcript_json"}]}, settings=base_settings())
    transcript, profile, parent = runner._load_inputs(job, runner._job_settings(job))
    assert isinstance(transcript, Transcript) and isinstance(profile, FormatProfile) and parent is None
    assert profile.builtin and profile.name == "標準"
    inline = json.loads((FIXTURES / "format-profile.standard.json").read_text())
    inline["name"] = "settings 優先"
    job2 = FakeQueue().add(owner_id=OWNER, input=job.input, settings=base_settings(format_snapshot=inline))
    assert runner._load_inputs(job2, runner._job_settings(job2))[1].name == "settings 優先"


def test_worker_heartbeat_is_refreshed_while_a_job_runs(claude_home, clean_environ, store, monkeypatch) -> None:
    """長時間の生成中も worker_heartbeats を更新する (R17)。

    job の lease heartbeat とは別テーブルで、止まると capabilities が稼働中の worker を
    alive=false と判定してしまう。生成を遅らせて、実行中に複数回更新されることを見る。
    """
    runner, queue = make_runner(claude_home, clean_environ, store, "logged_in")
    # heartbeat_seconds は lease_seconds から導く property (下限 5 秒) なので、試験では縮める
    monkeypatch.setattr(WorkerConfig, "heartbeat_seconds", property(lambda self: 0.05))
    beats: list[float] = []
    monkeypatch.setattr(runner, "report_heartbeat", lambda: beats.append(time.monotonic()))

    original = runner.generator.generate

    def slow_generate(*args, **kwargs):
        time.sleep(0.3)  # heartbeat 間隔 (0.05s) より十分長くする
        return original(*args, **kwargs)

    monkeypatch.setattr(runner.generator, "generate", slow_generate)

    job = queue.add(owner_id=OWNER, input={"artifacts": seed_inputs(store)}, settings=base_settings())
    assert runner.process(job).status == "succeeded"
    assert len(beats) >= 2, f"実行中に生存通知が更新されていない: {beats}"
