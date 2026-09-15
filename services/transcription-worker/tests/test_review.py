from __future__ import annotations

from transcription_worker.config import DecodeOptions
from transcription_worker.engines.base import RawSegment
from transcription_worker.pipeline.review import (
    detect_review_candidates,
    is_probable_hallucination,
    outside_speech,
)
from transcription_worker.pipeline.types import LocalSegment
from transcription_worker.vad import SpeechWindow

from .conftest import TURBO


def _local(start: int, end: int, text: str) -> LocalSegment:
    return LocalSegment(start, end, text, "ja", 0.9, TURBO, "vad_turbo")


def test_speech_without_text_detected() -> None:
    windows = [SpeechWindow(0, 3000), SpeechWindow(4000, 7000)]
    segments = [_local(0, 3000, "はい、まずロードマップから。")]
    candidates = detect_review_candidates(windows, segments, DecodeOptions())
    assert [(c.window.start_ms, c.reason) for c in candidates] == [(4000, "speech_without_text")]


def test_sparse_text_detected_for_long_window() -> None:
    windows = [SpeechWindow(0, 5000)]
    segments = [_local(0, 5000, "Mr.")]  # 5 秒に 3 文字
    candidates = detect_review_candidates(windows, segments, DecodeOptions(sparse_chars_per_second=0.7))
    assert candidates[0].reason == "sparse_text" and candidates[0].covered_chars == 3


def test_short_window_with_little_text_is_not_sparse() -> None:
    windows = [SpeechWindow(0, 1000)]
    segments = [_local(0, 1000, "はい")]
    assert detect_review_candidates(windows, segments, DecodeOptions()) == []


def test_overlap_fragment_windows_are_skipped() -> None:
    windows = [SpeechWindow(0, 3000), SpeechWindow(2500, 3200, overlaps_previous=True)]
    segments = [_local(0, 3000, "本文")]
    assert detect_review_candidates(windows, segments, DecodeOptions()) == []


def test_hallucination_rule() -> None:
    options = DecodeOptions()
    assert is_probable_hallucination(RawSegment(0, 1, "", "ja", 0.9), options)
    assert is_probable_hallucination(RawSegment(0, 1, "ご視聴ありがとうございました", "ja", 0.9, avg_logprob=-1.5, no_speech_prob=0.9), options)
    assert not is_probable_hallucination(RawSegment(0, 1, "本文", "ja", 0.9, avg_logprob=-0.2, no_speech_prob=0.9), options)
    assert not is_probable_hallucination(RawSegment(0, 1, "本文", "ja", 0.9), options), "確率が無いなら捏造せず残す"


def test_outside_speech() -> None:
    windows = [SpeechWindow(1000, 2000)]
    assert outside_speech(_local(3000, 4000, "x"), windows)
    assert not outside_speech(_local(1500, 2500, "x"), windows)
