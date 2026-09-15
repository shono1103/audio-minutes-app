"""言語モードと戦略 (全体計画 2 節・M4 実装順)。

* ja / en: 専門モデル固定 (fixed_ja / fixed_en)。
* mixed: VAD 窓ごとに多言語モデルを言語未指定で使い、専門モデルへ分割しない。
* auto:
  - vad_turbo (B0): 全 VAD 窓を多言語モデルで処理。保守的な比較基準。
  - routed (C): 窓ごとに言語判定し、高信頼・十分な長さの ja/en だけ専門モデルへ。
  - whole_retry (E): 全体を一括処理し、VAD 窓と照合した要確認区間だけ 1 回再処理して時間範囲ごとに置換。
要確認区間 (FR-154) は全戦略で記録し、再処理は 1 区間あたり max_review_attempts 回まで (FR-155)。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from transcription_worker.engines.base import (
    LanguageGuess,
    ModelRef,
    RawSegment,
    TranscribeOptions,
    TranscriptionEngine,
)
from transcription_worker.pipeline.merge import dedupe_window_overlaps, replace_range
from transcription_worker.pipeline.review import (
    ReviewCandidate,
    covered_text,
    detect_review_candidates,
    is_probable_hallucination,
    outside_speech,
    text_chars,
)
from transcription_worker.pipeline.routing import label_window, smooth_labels, specialist_language
from transcription_worker.pipeline.types import (
    LocalReview,
    LocalReviewAttempt,
    LocalSegment,
    StrategyResult,
    TrackContext,
    from_raw,
)
from transcription_worker.vad import SpeechWindow, slice_audio

StrategyFn = Callable[[TrackContext], StrategyResult]


def _options(ctx: TrackContext) -> TranscribeOptions:
    return TranscribeOptions(
        beam_size=ctx.options.beam_size,
        word_timestamps=ctx.options.word_timestamps,
        condition_on_previous_text=ctx.options.condition_on_previous_text,
    )


def _transcribe_window(
    ctx: TrackContext,
    engine: TranscriptionEngine,
    window: SpeechWindow,
    *,
    language: str | None,
    strategy: str,
    language_override: str | None = None,
) -> list[LocalSegment]:
    audio = slice_audio(ctx.audio, window, ctx.sample_rate)
    raws = engine.transcribe(audio, language, _options(ctx))
    segments: list[LocalSegment] = []
    for raw in raws:
        if is_probable_hallucination(raw, ctx.options):
            continue
        segment = from_raw(raw, window.start_ms, engine.model, strategy, language_override=language_override)
        if segment.end_ms <= segment.start_ms:
            segment.end_ms = segment.start_ms + 1
        segments.append(segment)
    return segments


def _choose_retry_engine(ctx: TrackContext, guess: LanguageGuess | None) -> tuple[TranscriptionEngine, str | None]:
    """再処理に使うモデル。高信頼 ja で専門モデルがあれば Kotoba、それ以外は多言語モデル + 判定言語。"""
    if (
        guess is not None
        and guess.language == "ja"
        and ctx.manager.available("ja")
        and (guess.probability is None or guess.probability >= ctx.options.specialist_confidence)
    ):
        return ctx.manager.get("ja"), "ja"
    language = guess.language if guess is not None and guess.language in ("ja", "en") else None
    return ctx.manager.get("multilingual"), language


def _acceptance(candidate: ReviewCandidate, candidate_segments: Sequence[LocalSegment], existing_chars: int) -> tuple[bool, str]:
    chars = sum(text_chars(segment.text) for segment in candidate_segments)
    if chars == 0:
        return False, "候補が空のため未解決のまま残す"
    if candidate.reason == "speech_without_text":
        return True, "元区間が空で候補に文字がある"
    if chars >= max(existing_chars * 1.5, existing_chars + 4):
        return True, f"候補 {chars} 文字が元 {existing_chars} 文字を十分に上回る"
    return False, f"候補 {chars} 文字は元 {existing_chars} 文字を十分に上回らない"


def retry_review_candidates(
    ctx: TrackContext,
    candidates: Sequence[ReviewCandidate],
    segments: list[LocalSegment],
    *,
    strategy: str,
    allow_retry: bool,
) -> tuple[list[LocalSegment], list[LocalReview], int]:
    """要確認候補を記録し、許可されていれば上限付きで再処理して時間範囲ごとに置換する。"""
    reviews: list[LocalReview] = []
    started = time.perf_counter()
    for index, candidate in enumerate(candidates, start=1):
        review = LocalReview(
            id=f"rev-{ctx.track_id}-{index:04d}",
            start_ms=candidate.window.start_ms,
            end_ms=candidate.window.end_ms,
            reason=candidate.reason,
        )
        if allow_retry:
            for attempt in range(1, ctx.options.max_review_attempts + 1):
                audio = slice_audio(ctx.audio, candidate.window, ctx.sample_rate)
                guess = ctx.manager.get("multilingual").detect_language(audio)
                engine, language = _choose_retry_engine(ctx, guess)
                candidate_segments = _transcribe_window(
                    ctx, engine, candidate.window, language=language, strategy=strategy, language_override=language
                )
                existing_chars, _ = covered_text(candidate.window, segments)
                accepted, reason = _acceptance(candidate, candidate_segments, existing_chars)
                review.attempts.append(
                    LocalReviewAttempt(
                        attempt=attempt,
                        model=engine.model,
                        language=guess.language if guess else None,
                        language_probability=guess.probability if guess else None,
                        candidate_text=" ".join(segment.text for segment in candidate_segments),
                        accepted=accepted,
                        reason=reason,
                    )
                )
                if accepted:
                    segments, _removed = replace_range(
                        segments, candidate.window.start_ms, candidate.window.end_ms, candidate_segments, review.id
                    )
                    review.resolved = True
                    break
        reviews.append(review)
        ctx.report(stage="review", index=index, total=len(candidates))
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return segments, reviews, elapsed_ms


def _finish(ctx: TrackContext, segments: list[LocalSegment], *, strategy: str, allow_retry: bool, timings: dict[str, int]) -> StrategyResult:
    segments = dedupe_window_overlaps(segments, overlap_ms_threshold=max(100, ctx.options.vad_overlap_ms // 2))
    candidates = detect_review_candidates(ctx.windows, segments, ctx.options)
    segments, reviews, retry_ms = retry_review_candidates(ctx, candidates, segments, strategy=strategy, allow_retry=allow_retry)
    timings["review_retry_ms"] = retry_ms
    return StrategyResult(segments=segments, review=reviews, timings=timings)


# --- 戦略 -----------------------------------------------------------------------


def fixed(ctx: TrackContext, language: str) -> StrategyResult:
    strategy = f"fixed_{language}"
    profile = "ja" if language == "ja" and ctx.manager.available("ja") else "multilingual"
    engine = ctx.manager.get(profile)
    started = time.perf_counter()
    segments: list[LocalSegment] = []
    for index, window in enumerate(ctx.windows, start=1):
        segments.extend(_transcribe_window(ctx, engine, window, language=language, strategy=strategy, language_override=language))
        ctx.report(stage="transcribe", index=index, total=len(ctx.windows))
    timings = {"transcribe_ms": int((time.perf_counter() - started) * 1000)}
    return _finish(ctx, segments, strategy=strategy, allow_retry=False, timings=timings)


def mixed(ctx: TrackContext) -> StrategyResult:
    engine = ctx.manager.get("multilingual")
    started = time.perf_counter()
    segments: list[LocalSegment] = []
    for index, window in enumerate(ctx.windows, start=1):
        segments.extend(_transcribe_window(ctx, engine, window, language=None, strategy="mixed"))
        ctx.report(stage="transcribe", index=index, total=len(ctx.windows))
    timings = {"transcribe_ms": int((time.perf_counter() - started) * 1000)}
    return _finish(ctx, segments, strategy="mixed", allow_retry=True, timings=timings)


def vad_turbo(ctx: TrackContext) -> StrategyResult:
    engine = ctx.manager.get("multilingual")
    started = time.perf_counter()
    segments: list[LocalSegment] = []
    for index, window in enumerate(ctx.windows, start=1):
        segments.extend(_transcribe_window(ctx, engine, window, language=None, strategy="vad_turbo"))
        ctx.report(stage="transcribe", index=index, total=len(ctx.windows))
    timings = {"transcribe_ms": int((time.perf_counter() - started) * 1000)}
    return _finish(ctx, segments, strategy="vad_turbo", allow_retry=True, timings=timings)


def routed(ctx: TrackContext) -> StrategyResult:
    multilingual = ctx.manager.get("multilingual")
    available = {profile for profile in ("ja", "en") if ctx.manager.available(profile) or profile == "en"}
    detection_started = time.perf_counter()
    labels = []
    for window in ctx.windows:
        guess = multilingual.detect_language(slice_audio(ctx.audio, window, ctx.sample_rate))
        labels.append(label_window(window, guess, ctx.options, available=available))
    labels = smooth_labels(labels)
    detection_ms = int((time.perf_counter() - detection_started) * 1000)

    started = time.perf_counter()
    segments: list[LocalSegment] = []
    # モデル切替を減らすため target ごとにまとめて処理する (時刻順は後で復元)
    order = ("multilingual", "en", "ja")
    for target in order:
        chosen = [label for label in labels if label.target == target]
        if not chosen:
            continue
        if target == "ja":
            engine = ctx.manager.get("ja")
            language = "ja"
        else:
            engine = ctx.manager.get("multilingual")
            language = specialist_language(target)
        for label in chosen:
            segments.extend(
                _transcribe_window(ctx, engine, label.window, language=language, strategy="routed", language_override=language)
            )
    ctx.report(stage="transcribe", index=len(labels), total=len(labels))
    timings = {
        "language_detection_ms": detection_ms,
        "transcribe_ms": int((time.perf_counter() - started) * 1000),
    }
    return _finish(ctx, segments, strategy="routed", allow_retry=True, timings=timings)


def whole_retry(ctx: TrackContext) -> StrategyResult:
    engine = ctx.manager.get("multilingual")
    started = time.perf_counter()
    raws: list[RawSegment] = engine.transcribe(ctx.audio, None, _options(ctx))
    segments: list[LocalSegment] = []
    for raw in raws:
        if is_probable_hallucination(raw, ctx.options):
            continue
        segment = from_raw(raw, 0, engine.model, "whole_retry")
        if outside_speech(segment, ctx.windows) and (segment.no_speech_prob or 0.0) > 0.5:
            continue  # 無音区間に生じた出力
        segments.append(segment)
    timings = {"transcribe_ms": int((time.perf_counter() - started) * 1000)}
    ctx.report(stage="transcribe", index=1, total=1)
    return _finish(ctx, segments, strategy="whole_retry", allow_retry=True, timings=timings)


def run_strategy(ctx: TrackContext, language_mode: str) -> StrategyResult:
    if language_mode == "ja":
        return fixed(ctx, "ja")
    if language_mode == "en":
        return fixed(ctx, "en")
    if language_mode == "mixed":
        return mixed(ctx)
    table: dict[str, StrategyFn] = {"vad_turbo": vad_turbo, "routed": routed, "whole_retry": whole_retry}
    if ctx.strategy_name not in table:
        raise ValueError(f"未知の戦略: {ctx.strategy_name}")
    return table[ctx.strategy_name](ctx)


def strategy_label(language_mode: str, strategy_name: str) -> str:
    if language_mode in ("ja", "en"):
        return f"fixed_{language_mode}"
    if language_mode == "mixed":
        return "mixed"
    return strategy_name


__all__ = ["ModelRef", "retry_review_candidates", "run_strategy", "strategy_label"]
