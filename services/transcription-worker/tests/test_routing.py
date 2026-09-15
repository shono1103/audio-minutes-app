from __future__ import annotations

from transcription_worker.config import DecodeOptions
from transcription_worker.engines.base import LanguageGuess
from transcription_worker.pipeline.routing import label_window, smooth_labels
from transcription_worker.vad import SpeechWindow

AVAILABLE = {"ja", "en"}


def test_high_confidence_long_ja_goes_to_specialist() -> None:
    label = label_window(SpeechWindow(0, 4000), LanguageGuess("ja", 0.9), DecodeOptions(), available=AVAILABLE)
    assert label.target == "ja"


def test_low_confidence_or_short_stays_multilingual() -> None:
    options = DecodeOptions()
    assert label_window(SpeechWindow(0, 4000), LanguageGuess("ja", 0.7), options, available=AVAILABLE).target == "multilingual"
    assert label_window(SpeechWindow(0, 2000), LanguageGuess("ja", 0.99), options, available=AVAILABLE).target == "multilingual"


def test_korean_misdetection_with_high_probability_is_not_forced() -> None:
    label = label_window(SpeechWindow(0, 5000), LanguageGuess("ko", 0.9977), DecodeOptions(), available=AVAILABLE)
    assert label.target == "multilingual" and label.reason == "ja/en 以外"


def test_missing_probability_is_not_fabricated() -> None:
    label = label_window(SpeechWindow(0, 5000), LanguageGuess("ja", None), DecodeOptions(), available=AVAILABLE)
    assert label.target == "multilingual" and label.reason == "言語確率なし"


def test_smoothing_absorbs_single_fallback_between_same_specialists() -> None:
    options = DecodeOptions()
    labels = [
        label_window(SpeechWindow(0, 4000), LanguageGuess("ja", 0.95), options, available=AVAILABLE),
        label_window(SpeechWindow(4000, 6000), LanguageGuess("ja", 0.7), options, available=AVAILABLE),
        label_window(SpeechWindow(6000, 10_000), LanguageGuess("ja", 0.95), options, available=AVAILABLE),
        label_window(SpeechWindow(10_000, 12_000), LanguageGuess("en", 0.5), options, available=AVAILABLE),
    ]
    smoothed = smooth_labels(labels)
    assert [label.target for label in smoothed] == ["ja", "ja", "ja", "multilingual"]
    assert smoothed[1].reason == "平滑化で吸収"


def test_smoothing_does_not_absorb_different_language() -> None:
    options = DecodeOptions()
    labels = [
        label_window(SpeechWindow(0, 4000), LanguageGuess("ja", 0.95), options, available=AVAILABLE),
        label_window(SpeechWindow(4000, 6000), LanguageGuess("en", 0.7), options, available=AVAILABLE),
        label_window(SpeechWindow(6000, 10_000), LanguageGuess("ja", 0.95), options, available=AVAILABLE),
    ]
    assert [label.target for label in smooth_labels(labels)] == ["ja", "multilingual", "ja"]
