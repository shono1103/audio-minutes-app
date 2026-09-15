"""区間の結合・置換・2 トラック merge。別トラックの同時刻は重複として削除しない。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

from audio_minutes_contracts.models import Segment, TrackRole

from transcription_worker.pipeline.types import LocalSegment


def overlap_ms(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s\W_]+", "", normalized)


def dedupe_window_overlaps(segments: Sequence[LocalSegment], overlap_ms_threshold: int = 200) -> list[LocalSegment]:
    """重なり窓の境界で同一トラック内に生じた重複を落とす。

    同じトラックの隣接する区間が時間的に重なり、正規化テキストが一致または包含関係なら後の方を削除する。
    別トラックの同時刻はここでは扱わない (削除しない)。
    """
    ordered = sorted(segments, key=lambda item: (item.start_ms, item.end_ms))
    kept: list[LocalSegment] = []
    for segment in ordered:
        if kept:
            previous = kept[-1]
            shared = overlap_ms(previous.start_ms, previous.end_ms, segment.start_ms, segment.end_ms)
            if shared >= overlap_ms_threshold:
                a = _normalize_text(previous.text)
                b = _normalize_text(segment.text)
                if a and b and (a == b or a.endswith(b) or b in a):
                    continue
                if a and b and a in b:
                    kept[-1] = segment
                    continue
        kept.append(segment)
    return kept


def replace_range(
    segments: Sequence[LocalSegment],
    start_ms: int,
    end_ms: int,
    replacement: Iterable[LocalSegment],
    review_id: str,
    *,
    minimum_overlap_ratio: float = 0.5,
) -> tuple[list[LocalSegment], list[LocalSegment]]:
    """時間範囲 [start_ms, end_ms) に属する既存区間を候補で置き換える。

    既存区間は「範囲との重なりが区間長の minimum_overlap_ratio 以上」または中点が範囲内なら対象。
    置換後の区間には replaced_by_review を付け、削除した旧区間も返す (採否理由の保存用)。
    """
    kept: list[LocalSegment] = []
    removed: list[LocalSegment] = []
    for segment in segments:
        shared = overlap_ms(segment.start_ms, segment.end_ms, start_ms, end_ms)
        inside = start_ms <= segment.midpoint_ms < end_ms
        ratio = shared / segment.duration_ms if segment.duration_ms > 0 else (1.0 if inside else 0.0)
        if inside or ratio >= minimum_overlap_ratio:
            removed.append(segment)
        else:
            kept.append(segment)
    for candidate in replacement:
        candidate.replaced_by_review = review_id
        kept.append(candidate)
    kept.sort(key=lambda item: (item.start_ms, item.end_ms))
    return kept, removed


def to_common_time(
    segments: Sequence[LocalSegment],
    *,
    track_id: str,
    role: TrackRole,
    start_offset_ms: int,
    engine: str,
    engine_version: str,
    id_prefix: str,
) -> list[Segment]:
    """トラック先頭基準の ms へ start_offset_ms を加え、契約の Segment に変換する。"""
    ordered = sorted(segments, key=lambda item: (item.start_ms, item.end_ms))
    results: list[Segment] = []
    for index, segment in enumerate(ordered, start=1):
        words = None
        if segment.words:
            words = [
                word.model_copy(update={"start_ms": word.start_ms + start_offset_ms, "end_ms": word.end_ms + start_offset_ms})
                for word in segment.words
            ]
        results.append(
            Segment(
                id=f"{id_prefix}-{index:05d}",
                track_id=track_id,
                source=role,
                start_ms=segment.start_ms + start_offset_ms,
                end_ms=max(segment.start_ms, segment.end_ms) + start_offset_ms,
                text=segment.text,
                language=segment.language,  # type: ignore[arg-type]
                language_probability=segment.language_probability,
                model_id=segment.model.model_id,
                model_revision=segment.model.revision,
                engine=engine,  # type: ignore[arg-type]
                engine_version=engine_version,
                strategy=segment.strategy,  # type: ignore[arg-type]
                avg_logprob=segment.avg_logprob,
                no_speech_prob=segment.no_speech_prob,
                words=words,
                replaced_by_review=segment.replaced_by_review,
            )
        )
    return results


def merge_tracks(per_track: Sequence[Sequence[Segment]]) -> list[Segment]:
    """時刻順に結合する。同時発話 (別トラックの同時刻) は保持する。"""
    merged = [segment for segments in per_track for segment in segments]
    merged.sort(key=lambda item: (item.start_ms, item.track_id, item.end_ms))
    return merged
