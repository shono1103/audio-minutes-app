"""環境変数からの設定読み込み。名前と既定値は docs/integration-contract.md が正。"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Backend = Literal["cpu", "vulkan"]
EngineName = Literal["faster-whisper", "whisper.cpp"]
StrategyName = Literal["vad_turbo", "routed", "whole_retry"]


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ModelSetting:
    model_id: str
    revision: str
    compute_type: str | None = "int8"
    quantization: str | None = None
    ggml_file: str | None = None  # whisper.cpp 用のファイル名 (models_dir/ggml/ 配下)


@dataclass(frozen=True)
class DecodeOptions:
    beam_size: int = 5
    word_timestamps: bool = False
    condition_on_previous_text: bool = False
    vad_threshold: float = 0.5
    vad_min_speech_ms: int = 100
    vad_min_silence_ms: int = 500
    vad_pad_ms: int = 250
    vad_max_window_ms: int = 30_000
    vad_overlap_ms: int = 500
    specialist_confidence: float = 0.85
    specialist_minimum_seconds: float = 3.0
    sparse_chars_per_second: float = 0.5
    sparse_minimum_window_seconds: float = 2.0
    hallucination_no_speech_prob: float = 0.6
    hallucination_avg_logprob: float = -1.0
    max_review_attempts: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "beam_size": self.beam_size,
            "word_timestamps": self.word_timestamps,
            "condition_on_previous_text": self.condition_on_previous_text,
            "vad": {
                "threshold": self.vad_threshold,
                "min_speech_ms": self.vad_min_speech_ms,
                "min_silence_ms": self.vad_min_silence_ms,
                "pad_ms": self.vad_pad_ms,
                "max_window_ms": self.vad_max_window_ms,
                "overlap_ms": self.vad_overlap_ms,
            },
            "specialist": {
                "confidence": self.specialist_confidence,
                "minimum_seconds": self.specialist_minimum_seconds,
            },
            "review": {
                "sparse_chars_per_second": self.sparse_chars_per_second,
                "sparse_minimum_window_seconds": self.sparse_minimum_window_seconds,
                "hallucination_no_speech_prob": self.hallucination_no_speech_prob,
                "hallucination_avg_logprob": self.hallucination_avg_logprob,
                "max_attempts": self.max_review_attempts,
            },
        }


@dataclass(frozen=True)
class WorkerConfig:
    database_url: str
    artifacts_dir: Path
    models_dir: Path
    engine: EngineName = "faster-whisper"
    backend: Backend = "cpu"
    strategy: StrategyName = "vad_turbo"
    threads: int = 4
    model_ja: ModelSetting = field(
        default_factory=lambda: ModelSetting(
            "kotoba-tech/kotoba-whisper-v2.0-faster", "f44edd35eaeb2274e85ac7b31fb2c6f59ff1c4bc"
        )
    )
    model_multilingual: ModelSetting = field(
        default_factory=lambda: ModelSetting(
            "dropbox-dash/faster-whisper-large-v3-turbo", "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
        )
    )
    whisper_cpp_bin: str = "/opt/whisper.cpp/build/bin/whisper-cli"
    whisper_cpp_model_dir: Path = Path("/var/lib/audio-minutes/models/ggml")
    gpu_cpu_fallback: bool = False
    hf_offline: bool = True
    worker_id: str = ""
    lease_seconds: int = 120
    poll_seconds: float = 2.0
    heartbeat_table_seconds: float = 15.0
    update_maintenance_file: Path = Path("/var/log/audio-minutes/.update-maintenance")
    max_resident_models: int | None = None  # None なら cgroup メモリから決める
    log_level: str = "info"
    decode: DecodeOptions = field(default_factory=DecodeOptions)

    @staticmethod
    def from_env() -> WorkerConfig:
        engine = _env("AM_ENGINE", "faster-whisper")
        backend = _env("AM_BACKEND", "cpu")
        if engine not in ("faster-whisper", "whisper.cpp"):
            raise ValueError(f"AM_ENGINE が不正です: {engine}")
        if backend not in ("cpu", "vulkan"):
            raise ValueError(f"AM_BACKEND が不正です: {backend}")
        if backend == "vulkan" and engine != "whisper.cpp":
            raise ValueError("AM_BACKEND=vulkan は AM_ENGINE=whisper.cpp でだけ使えます (CTranslate2 に Vulkan 経路はない)")
        strategy = _env("AM_STRATEGY", "vad_turbo")
        if strategy not in ("vad_turbo", "routed", "whole_retry"):
            raise ValueError(f"AM_STRATEGY が不正です: {strategy}")
        models_dir = Path(_env("AM_MODELS_DIR", "/var/lib/audio-minutes/models"))
        worker_id = _env("AM_WORKER_ID", f"transcription-{socket.gethostname()}")
        resident = os.environ.get("AM_MAX_RESIDENT_MODELS")
        return WorkerConfig(
            database_url=_env("AM_WORKER_DATABASE_URL", ""),
            artifacts_dir=Path(_env("AM_ARTIFACTS_DIR", "/var/lib/audio-minutes/artifacts")),
            models_dir=models_dir,
            engine=engine,  # type: ignore[arg-type]
            backend=backend,  # type: ignore[arg-type]
            strategy=strategy,  # type: ignore[arg-type]
            threads=_env_int("AM_THREADS", 4),
            model_ja=ModelSetting(
                _env("AM_MODEL_JA", "kotoba-tech/kotoba-whisper-v2.0-faster"),
                _env("AM_MODEL_JA_REVISION", "f44edd35eaeb2274e85ac7b31fb2c6f59ff1c4bc"),
                ggml_file=_env("AM_MODEL_JA_GGML", "ggml-kotoba-whisper-v2.0-q5_0.bin"),
                quantization=_env("AM_MODEL_JA_QUANT", "q5_0") if engine == "whisper.cpp" else None,
            ),
            model_multilingual=ModelSetting(
                _env("AM_MODEL_MULTILINGUAL", "dropbox-dash/faster-whisper-large-v3-turbo"),
                _env("AM_MODEL_MULTILINGUAL_REVISION", "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"),
                ggml_file=_env("AM_MODEL_MULTILINGUAL_GGML", "ggml-large-v3-turbo-q5_0.bin"),
                quantization=_env("AM_MODEL_MULTILINGUAL_QUANT", "q5_0") if engine == "whisper.cpp" else None,
            ),
            whisper_cpp_bin=_env("AM_WHISPER_CPP_BIN", "/opt/whisper.cpp/build/bin/whisper-cli"),
            whisper_cpp_model_dir=Path(_env("AM_WHISPER_CPP_MODEL_DIR", str(models_dir / "ggml"))),
            gpu_cpu_fallback=_env_bool("AM_GPU_CPU_FALLBACK", False),
            hf_offline=_env_bool("HF_HUB_OFFLINE", True),
            worker_id=worker_id,
            lease_seconds=_env_int("AM_LEASE_SECONDS", 120),
            poll_seconds=_env_float("AM_POLL_SECONDS", 2.0),
            update_maintenance_file=Path(
                _env("AM_UPDATE_MAINTENANCE_FILE", "/var/log/audio-minutes/.update-maintenance")
            ),
            max_resident_models=int(resident) if resident else None,
            log_level=_env("AM_LOG_LEVEL", "info"),
            decode=DecodeOptions(
                beam_size=_env_int("AM_BEAM_SIZE", 5),
                word_timestamps=_env_bool("AM_WORD_TIMESTAMPS", False),
                specialist_confidence=_env_float("AM_SPECIALIST_CONFIDENCE", 0.85),
                specialist_minimum_seconds=_env_float("AM_SPECIALIST_MIN_SECONDS", 3.0),
            ),
        )
