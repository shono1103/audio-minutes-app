from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from transcription_worker.config import ModelSetting, WorkerConfig
from transcription_worker.engines.base import ModelRef
from transcription_worker.models import (
    IN_PROGRESS_FILE,
    SMOKE_FILE,
    model_status,
    prefetch,
    smoke,
    verify_manifest,
    write_manifest,
)


def _config(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(
        database_url="postgresql://unused",
        artifacts_dir=tmp_path / "artifacts",
        models_dir=tmp_path / "models",
        model_multilingual=ModelSetting("example/multi", "1" * 40),
        model_ja=ModelSetting("example/ja", "2" * 40),
    )


def _entry(root: Path, model_id: str, revision: str, content: bytes = b"weight") -> dict[str, object]:
    model_root = root / "hub" / model_id.replace("/", "--") / revision
    model_root.mkdir(parents=True, exist_ok=True)
    path = model_root / "model.bin"
    path.write_bytes(content)
    import hashlib

    return {
        "engine": "faster-whisper",
        "model_id": model_id,
        "revision": revision,
        "resolved_path": str(model_root.relative_to(root)),
        "files": [{"path": "model.bin", "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}],
    }


def _ready_manifest(config: WorkerConfig) -> None:
    config.models_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(
        config.models_dir,
        [
            _entry(config.models_dir, config.model_multilingual.model_id, config.model_multilingual.revision),
            _entry(config.models_dir, config.model_ja.model_id, config.model_ja.revision),
        ],
    )


def test_verify_detects_interruption_missing_revision_and_corruption(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _ready_manifest(config)
    (config.models_dir / IN_PROGRESS_FILE).write_text("{}", encoding="utf-8")
    problems = verify_manifest(config.models_dir, config)
    assert any("中断" in problem for problem in problems)

    (config.models_dir / IN_PROGRESS_FILE).unlink()
    target = config.models_dir / "hub" / "example--ja" / config.model_ja.revision / "model.bin"
    target.write_bytes(b"broken")
    problems = verify_manifest(config.models_dir, config)
    assert any("size 不一致" in problem or "hash 不一致" in problem for problem in problems)


def test_status_requires_successful_offline_smoke(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _ready_manifest(config)
    assert model_status(config.models_dir, config)["ready"] is False
    (config.models_dir / SMOKE_FILE).write_text(
        '{"ready":true,"models":['
        f'{{"model_id":"{config.model_multilingual.model_id}","revision":"{config.model_multilingual.revision}"}},'
        f'{{"model_id":"{config.model_ja.model_id}","revision":"{config.model_ja.revision}"}}]}}',
        encoding="utf-8",
    )
    assert model_status(config.models_dir, config)["ready"] is True


def test_prefetch_is_resumable_and_preserves_incomplete_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    config.models_dir.mkdir(parents=True)
    calls = 0

    def fake_prefetch(
        setting: ModelSetting, models_dir: Path, *, offline: bool, force_download: bool = False
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConnectionError("interrupted")
        return _entry(models_dir, setting.model_id, setting.revision)

    monkeypatch.setattr("transcription_worker.models.prefetch_faster_whisper", fake_prefetch)
    monkeypatch.setattr("transcription_worker.models.shutil.disk_usage", lambda _path: SimpleNamespace(free=10_000))
    with pytest.raises(ConnectionError):
        prefetch(config, config.models_dir, online=True, min_free_bytes=1)
    assert (config.models_dir / IN_PROGRESS_FILE).is_file()
    assert not (config.models_dir / "manifest.json").exists()

    path = prefetch(config, config.models_dir, online=True, min_free_bytes=1)
    assert path.is_file()
    assert not (config.models_dir / IN_PROGRESS_FILE).exists()
    assert not verify_manifest(config.models_dir, config)


def test_prefetch_rejects_insufficient_space_before_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    config.models_dir.mkdir(parents=True)
    monkeypatch.setattr("transcription_worker.models.shutil.disk_usage", lambda _path: SimpleNamespace(free=9))
    with pytest.raises(OSError, match="空き容量"):
        prefetch(config, config.models_dir, online=True, min_free_bytes=10)
    assert not (config.models_dir / IN_PROGRESS_FILE).exists()


def test_smoke_loads_both_models_with_synthetic_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    _ready_manifest(config)
    loaded: list[str] = []

    class FakeEngine:
        def __init__(self, ref: ModelRef) -> None:
            self.model = ref

        def load(self) -> None:
            loaded.append(self.model.profile)

        def transcribe(self, audio: np.ndarray, language: str, options) -> list:  # noqa: ANN001
            assert audio.size == 8_000
            assert language in ("ja", "en")
            return []

        def unload(self) -> None:
            pass

    refs = {
        "multilingual": ModelRef("multilingual", config.model_multilingual.model_id, config.model_multilingual.revision),
        "ja": ModelRef("ja", config.model_ja.model_id, config.model_ja.revision),
    }
    monkeypatch.setattr("transcription_worker.pipeline.transcribe.engine_factory", lambda _config: FakeEngine)
    monkeypatch.setattr("transcription_worker.pipeline.transcribe.model_refs", lambda _config: refs)
    result = smoke(config)
    assert result["ready"] is True
    assert set(loaded) == {"ja", "multilingual"}
    assert "language_modes" not in result
    assert "tracks" not in result
    assert result["manifest"] == {"layout": True, "size": True, "sha256": True}
    assert all(model["checks"] == {"loaded": True, "synthetic_transcription": True} for model in result["models"])
    assert (config.models_dir / SMOKE_FILE).is_file()


def test_smoke_refuses_online_mode(tmp_path: Path) -> None:
    from dataclasses import replace

    config = replace(_config(tmp_path), hf_offline=False)
    _ready_manifest(config)
    with pytest.raises(RuntimeError, match="HF_HUB_OFFLINE=1"):
        smoke(config)
