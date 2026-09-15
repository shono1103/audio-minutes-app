"""pipeline 内部の型。時刻はトラック先頭基準の ms。共通時刻への復元は transcribe.py が行う。"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from audio_minutes_contracts.models import Word

from transcription_worker.config import DecodeOptions
from transcription_worker.engines.base import ModelRef, RawSegment
from transcription_worker.engines.manager import ModelManager
from transcription_worker.vad import SpeechWindow


@dataclass
class LocalSegment:
    start_ms: int
    end_ms: int
    text: str
    language: str  # ja | en | und
    language_probability: float | None
    model: ModelRef
    strategy: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    words: list[Word] | None = None
    replaced_by_review: str | None = None

    @property
    def midpoint_ms(self) -> int:
        return (self.start_ms + self.end_ms) // 2

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass
class LocalReviewAttempt:
    attempt: int
    model: ModelRef
    language: str | None
    language_probability: float | None
    candidate_text: str
    accepted: bool
    reason: str


@dataclass
class LocalReview:
    id: str
    start_ms: int
    end_ms: int
    reason: str
    attempts: list[LocalReviewAttempt] = field(default_factory=list)
    resolved: bool = False


@dataclass
class TrackContext:
    track_id: str
    audio: np.ndarray
    windows: list[SpeechWindow]
    options: DecodeOptions
    manager: ModelManager
    engine_name: str
    engine_version: str
    strategy_name: str
    sample_rate: int = 16_000
    progress: object | None = None  # callable(detail: dict) | None

    def report(self, **detail: object) -> None:
        if callable(self.progress):
            self.progress(detail)


@dataclass
class StrategyResult:
    segments: list[LocalSegment]
    review: list[LocalReview]
    timings: dict[str, int] = field(default_factory=dict)


def normalize_language(value: str | None) -> str:
    if value in ("ja", "en"):
        return value
    return "und"


def from_raw(raw: RawSegment, base_ms: int, model: ModelRef, strategy: str, *, language_override: str | None = None) -> LocalSegment:
    """engine の秒単位区間を窓の先頭 (base_ms) 基準の ms へ変換する。"""
    words = None
    if raw.words:
        words = [
            Word(
                start_ms=base_ms + int(round(word.start * 1000)),
                end_ms=base_ms + int(round(word.end * 1000)),
                word=word.word,
                probability=word.probability,
            )
            for word in raw.words
        ]
    return LocalSegment(
        start_ms=base_ms + int(round(raw.start * 1000)),
        end_ms=base_ms + int(round(raw.end * 1000)),
        text=raw.text.strip(),
        language=normalize_language(language_override or raw.language),
        language_probability=raw.language_probability,
        model=model,
        strategy=strategy,
        avg_logprob=raw.avg_logprob,
        no_speech_prob=raw.no_speech_prob,
        words=words,
    )
