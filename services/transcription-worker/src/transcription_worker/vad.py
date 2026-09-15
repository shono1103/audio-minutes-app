"""外部 VAD (Silero、faster-whisper 同梱) による発話窓の生成。

言語判定より先に VAD を行う (要件 8 節)。長い発話は max_window_ms で分割し、
分割境界には overlap_ms の重なりを持たせて語の欠落を抑える。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from transcription_worker.audio import TARGET_SAMPLE_RATE, samples_to_ms
from transcription_worker.config import DecodeOptions


@dataclass(frozen=True)
class SpeechWindow:
    start_ms: int
    end_ms: int
    overlaps_previous: bool = False  # 分割で生じた重なり窓

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def speech_timestamps(audio: np.ndarray, options: DecodeOptions, sample_rate: int = TARGET_SAMPLE_RATE) -> list[tuple[int, int]]:
    """Silero VAD を実行し、サンプル単位の (start, end) を返す。"""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    vad_options = VadOptions(
        threshold=options.vad_threshold,
        min_speech_duration_ms=options.vad_min_speech_ms,
        min_silence_duration_ms=options.vad_min_silence_ms,
        speech_pad_ms=options.vad_pad_ms,
    )
    chunks = get_speech_timestamps(audio, vad_options, sampling_rate=sample_rate)
    return [(int(chunk["start"]), int(chunk["end"])) for chunk in chunks]


def windows_from_samples(
    chunks: Sequence[tuple[int, int]],
    total_samples: int,
    options: DecodeOptions,
    sample_rate: int = TARGET_SAMPLE_RATE,
) -> list[SpeechWindow]:
    """サンプル区間を ms の窓へ変換し、長い区間は重なり付きで分割する。"""
    windows: list[SpeechWindow] = []
    total_ms = samples_to_ms(total_samples, sample_rate)
    for start_sample, end_sample in chunks:
        start_ms = max(0, samples_to_ms(start_sample, sample_rate))
        end_ms = min(total_ms, samples_to_ms(end_sample, sample_rate))
        if end_ms <= start_ms:
            continue
        cursor = start_ms
        first = True
        while cursor < end_ms:
            window_end = min(end_ms, cursor + options.vad_max_window_ms)
            windows.append(SpeechWindow(cursor, window_end, overlaps_previous=not first))
            if window_end >= end_ms:
                break
            cursor = max(cursor + 1, window_end - options.vad_overlap_ms)
            first = False
    return windows


def detect_windows(audio: np.ndarray, options: DecodeOptions, sample_rate: int = TARGET_SAMPLE_RATE) -> list[SpeechWindow]:
    return windows_from_samples(speech_timestamps(audio, options, sample_rate), len(audio), options, sample_rate)


def slice_audio(audio: np.ndarray, window: SpeechWindow, sample_rate: int = TARGET_SAMPLE_RATE) -> np.ndarray:
    start = int(round(window.start_ms * sample_rate / 1000))
    end = int(round(window.end_ms * sample_rate / 1000))
    return audio[start:end]
