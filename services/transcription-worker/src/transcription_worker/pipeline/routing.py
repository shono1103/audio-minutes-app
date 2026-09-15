"""言語判定のルーティング (routed 戦略)。閾値・最小区間長・隣接窓の平滑化。

最大確率だけで専門モデルへ強制しない (日本語を韓国語へ 99.77% で誤判定した実測がある)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from transcription_worker.config import DecodeOptions
from transcription_worker.engines.base import LanguageGuess
from transcription_worker.vad import SpeechWindow

SPECIALISTS = ("ja", "en")


@dataclass(frozen=True)
class WindowLabel:
    window: SpeechWindow
    language: str | None
    probability: float | None
    target: str  # ja | en | multilingual
    reason: str


def label_window(window: SpeechWindow, guess: LanguageGuess | None, options: DecodeOptions, *, available: set[str]) -> WindowLabel:
    if guess is None:
        return WindowLabel(window, None, None, "multilingual", "言語判定なし")
    language = guess.language
    probability = guess.probability
    if language not in SPECIALISTS:
        return WindowLabel(window, language, probability, "multilingual", "ja/en 以外")
    if language not in available:
        return WindowLabel(window, language, probability, "multilingual", f"{language} 専門モデル未設定")
    if probability is None:
        return WindowLabel(window, language, None, "multilingual", "言語確率なし")
    if probability < options.specialist_confidence:
        return WindowLabel(window, language, probability, "multilingual", "低信頼")
    if window.duration_ms < options.specialist_minimum_seconds * 1000:
        return WindowLabel(window, language, probability, "multilingual", "短区間")
    return WindowLabel(window, language, probability, language, "高信頼")


def smooth_labels(labels: Sequence[WindowLabel], *, absorb_probability: float = 0.6) -> list[WindowLabel]:
    """同じ専門言語に挟まれた 1 窓の fallback を、判定言語が一致し確率が十分なら吸収する。

    頻繁なモデル切替を抑えるための最小限の平滑化。境界の窓は変えない。
    """
    result = list(labels)
    for index in range(1, len(result) - 1):
        current = result[index]
        previous = result[index - 1]
        following = result[index + 1]
        if current.target != "multilingual":
            continue
        if previous.target not in SPECIALISTS or previous.target != following.target:
            continue
        if current.language == previous.target and (current.probability or 0.0) >= absorb_probability:
            result[index] = WindowLabel(current.window, current.language, current.probability, previous.target, "平滑化で吸収")
    return result


def specialist_language(target: str) -> str | None:
    return target if target in SPECIALISTS else None
