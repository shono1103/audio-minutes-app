"""要確認区間の検出 (FR-154) と幻覚抑制。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from transcription_worker.config import DecodeOptions
from transcription_worker.engines.base import RawSegment
from transcription_worker.pipeline.merge import overlap_ms
from transcription_worker.pipeline.types import LocalSegment
from transcription_worker.vad import SpeechWindow


@dataclass(frozen=True)
class ReviewCandidate:
    window: SpeechWindow
    reason: str  # speech_without_text | sparse_text
    covered_chars: int


def text_chars(text: str) -> int:
    return len("".join(text.split()))


def covered_text(window: SpeechWindow, segments: Sequence[LocalSegment]) -> tuple[int, list[LocalSegment]]:
    """窓と重なる区間のうち、重なり比率に応じた文字数を数える。"""
    chars = 0
    matched: list[LocalSegment] = []
    for segment in segments:
        shared = overlap_ms(window.start_ms, window.end_ms, segment.start_ms, segment.end_ms)
        if shared <= 0:
            continue
        matched.append(segment)
        ratio = shared / segment.duration_ms if segment.duration_ms > 0 else 1.0
        chars += int(round(text_chars(segment.text) * min(1.0, ratio)))
    return chars, matched


def detect_review_candidates(
    windows: Sequence[SpeechWindow], segments: Sequence[LocalSegment], options: DecodeOptions
) -> list[ReviewCandidate]:
    """VAD 上の音声窓と文字起こしを照合し、出力なし・極端に疎な窓を返す。"""
    candidates: list[ReviewCandidate] = []
    for window in windows:
        if window.overlaps_previous and window.duration_ms < options.vad_overlap_ms * 2:
            continue  # 分割の重なり片だけの窓は単独では判定しない
        chars, _ = covered_text(window, segments)
        seconds = window.duration_ms / 1000.0
        if chars == 0:
            candidates.append(ReviewCandidate(window, "speech_without_text", 0))
        elif seconds >= options.sparse_minimum_window_seconds and chars / seconds < options.sparse_chars_per_second:
            candidates.append(ReviewCandidate(window, "sparse_text", chars))
    return candidates


def is_probable_hallucination(raw: RawSegment, options: DecodeOptions) -> bool:
    """無音幻覚の疑い。no_speech_prob が高く、かつ平均対数確率が低い区間。"""
    if not raw.text.strip():
        return True
    if raw.no_speech_prob is None or raw.avg_logprob is None:
        return False
    return raw.no_speech_prob > options.hallucination_no_speech_prob and raw.avg_logprob < options.hallucination_avg_logprob


def outside_speech(segment: LocalSegment, windows: Sequence[SpeechWindow]) -> bool:
    """VAD 窓とまったく重ならない区間 (whole 処理で無音に生じた出力)。"""
    return all(overlap_ms(segment.start_ms, segment.end_ms, window.start_ms, window.end_ms) == 0 for window in windows)
