"""TranscriptionEngine の契約。

* 時刻は秒 (float) で渡された音声の先頭基準。ms 変換と共通時刻への復元は pipeline 側が行う。
* 取得できない値 (言語確率・単語時刻) は None を返し、捏造しない。
* GPU を要求して使えない場合は GpuUnavailable を送出し、CPU 実行結果を返さない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

import numpy as np
from audio_minutes_contracts.models import Backend

Profile = Literal["ja", "en", "multilingual"]


@dataclass(frozen=True)
class ModelRef:
    profile: Profile
    model_id: str
    revision: str
    compute_type: str | None = None
    quantization: str | None = None
    local_file: str | None = None  # whisper.cpp の ggml ファイル名


@dataclass(frozen=True)
class RawWord:
    start: float
    end: float
    word: str
    probability: float | None = None


@dataclass(frozen=True)
class RawSegment:
    start: float
    end: float
    text: str
    language: str | None
    language_probability: float | None
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    words: list[RawWord] | None = None


@dataclass(frozen=True)
class LanguageGuess:
    language: str
    probability: float | None
    all_probabilities: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class TranscribeOptions:
    beam_size: int = 5
    word_timestamps: bool = False
    condition_on_previous_text: bool = False
    initial_prompt: str | None = None


@dataclass(frozen=True)
class Capabilities:
    engine: Literal["faster-whisper", "whisper.cpp"]
    engine_version: str
    language_detection: bool
    language_probability: bool
    word_timestamps: bool
    segment_timestamps: bool
    supported_backends: tuple[str, ...]
    backend: Backend
    model_loaded: bool = False
    notes: tuple[str, ...] = ()

    def supports_language_modes(self) -> dict[str, bool]:
        """auto / mixed には言語判定が必要。満たさない組合せは部分対応と表示する。"""
        return {
            "ja": self.segment_timestamps,
            "en": self.segment_timestamps,
            "auto": self.segment_timestamps and self.language_detection,
            "mixed": self.segment_timestamps and self.language_detection,
        }


class EngineError(RuntimeError):
    code: str = "backend_error"
    retryable: bool = False

    def __init__(self, message: str, *, code: str | None = None, retryable: bool | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable


class GpuUnavailable(EngineError):
    code = "gpu_unavailable"
    retryable = False


class UnsupportedModel(EngineError):
    code = "unsupported_model"
    retryable = False


class EngineCrashed(EngineError):
    code = "engine_crashed"
    retryable = True


class TranscriptionEngine(Protocol):
    """1 モデルを保持する推論アダプター。"""

    model: ModelRef

    def probe(self) -> Capabilities: ...

    def load(self) -> None: ...

    def unload(self) -> None: ...

    @property
    def loaded(self) -> bool: ...

    def detect_language(self, audio: np.ndarray) -> LanguageGuess | None: ...

    def transcribe(self, audio: np.ndarray, language: str | None, options: TranscribeOptions) -> list[RawSegment]: ...

    def cancel(self) -> None: ...

    def health(self) -> bool: ...

    def backend(self) -> Backend: ...
