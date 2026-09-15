"""推論エンジンの交換境界。共通処理は base の型だけを使い、faster-whisper / whisper.cpp の内部型を持ち込まない。"""

from transcription_worker.engines.base import (
    Capabilities,
    EngineError,
    GpuUnavailable,
    LanguageGuess,
    ModelRef,
    RawSegment,
    RawWord,
    TranscribeOptions,
    TranscriptionEngine,
    UnsupportedModel,
)

__all__ = [
    "Capabilities",
    "EngineError",
    "GpuUnavailable",
    "LanguageGuess",
    "ModelRef",
    "RawSegment",
    "RawWord",
    "TranscribeOptions",
    "TranscriptionEngine",
    "UnsupportedModel",
]
