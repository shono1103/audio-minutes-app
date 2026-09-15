"""テスト共通の fake engine と補助関数。実モデルを使わない。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from audio_minutes_contracts.models import Backend

from transcription_worker.config import DecodeOptions
from transcription_worker.engines.base import (
    Capabilities,
    LanguageGuess,
    ModelRef,
    RawSegment,
    TranscribeOptions,
)
from transcription_worker.engines.manager import ModelManager
from transcription_worker.vad import SpeechWindow

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "audio"
RATE = 16_000

TURBO = ModelRef("multilingual", "dropbox-dash/faster-whisper-large-v3-turbo", "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf", compute_type="int8")
KOTOBA = ModelRef("ja", "kotoba-tech/kotoba-whisper-v2.0-faster", "f44edd35eaeb2274e85ac7b31fb2c6f59ff1c4bc", compute_type="int8")


def marker_audio(duration_ms: int) -> np.ndarray:
    """サンプル値に ms 時刻を埋め込んだ音声。fake engine は audio[0] から窓の開始時刻を知る。"""
    samples = int(duration_ms * RATE / 1000)
    return (np.arange(samples, dtype=np.float64) * 1000.0 / RATE / 1e6).astype(np.float32)


def marker_start_ms(audio: np.ndarray) -> int:
    return int(round(float(audio[0]) * 1e6)) if audio.size else 0


def audio_duration_ms(audio: np.ndarray) -> int:
    return int(round(audio.size * 1000 / RATE))


ScriptFn = Callable[[int, int, str | None], list[RawSegment]]
DetectFn = Callable[[int, int], LanguageGuess | None]


class FakeEngine:
    """script(start_ms, duration_ms, language) → RawSegment (窓内の秒)。"""

    engine_name = "fake"

    def __init__(self, model: ModelRef, script: ScriptFn, detect: DetectFn | None = None) -> None:
        self.model = model
        self._script = script
        self._detect = detect
        self._loaded = False
        self.calls: list[tuple[str, int, int, str | None]] = []
        self.load_count = 0
        self.unload_count = 0
        self.load_seconds = 0.01

    def backend(self) -> Backend:
        return Backend(requested_backend="cpu", effective_backend="cpu", gpu_verified=False)

    @property
    def loaded(self) -> bool:
        return self._loaded

    def probe(self) -> Capabilities:
        return Capabilities(
            engine="faster-whisper", engine_version="fake-1.0", language_detection=True, language_probability=True,
            word_timestamps=False, segment_timestamps=True, supported_backends=("cpu",), backend=self.backend(),
        )

    def load(self) -> None:
        self._loaded = True
        self.load_count += 1

    def unload(self) -> None:
        self._loaded = False
        self.unload_count += 1

    def detect_language(self, audio: np.ndarray) -> LanguageGuess | None:
        self.calls.append(("detect", marker_start_ms(audio), audio_duration_ms(audio), None))
        return self._detect(marker_start_ms(audio), audio_duration_ms(audio)) if self._detect else None

    def transcribe(self, audio: np.ndarray, language: str | None, options: TranscribeOptions) -> list[RawSegment]:
        start = marker_start_ms(audio)
        duration = audio_duration_ms(audio)
        self.calls.append(("transcribe", start, duration, language))
        return self._script(start, duration, language)

    def cancel(self) -> None:  # pragma: no cover
        pass

    def health(self) -> bool:
        return True


def seg(start_s: float, end_s: float, text: str, language: str | None = "ja", prob: float | None = 0.95, **extra) -> RawSegment:
    return RawSegment(start=start_s, end=end_s, text=text, language=language, language_probability=prob, **extra)


def make_manager(turbo_script: ScriptFn, *, ja_script: ScriptFn | None = None, detect: DetectFn | None = None, max_resident: int = 2) -> tuple[ModelManager, dict[str, FakeEngine]]:
    engines: dict[str, FakeEngine] = {}

    def factory(ref: ModelRef) -> FakeEngine:
        if ref.profile == "ja":
            engine = FakeEngine(ref, ja_script or turbo_script, detect)
        else:
            engine = FakeEngine(ref, turbo_script, detect)
        engines[ref.profile] = engine
        return engine

    models = {"multilingual": TURBO}
    if ja_script is not None:
        models["ja"] = KOTOBA
    return ModelManager(factory, models, max_resident=max_resident), engines


def windows(*ranges: tuple[int, int]) -> list[SpeechWindow]:
    return [SpeechWindow(start, end) for start, end in ranges]


@pytest.fixture
def options() -> DecodeOptions:
    return DecodeOptions()
