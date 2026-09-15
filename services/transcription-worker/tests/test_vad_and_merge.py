from __future__ import annotations

from audio_minutes_contracts.models import Segment

from transcription_worker.audio import ms_to_samples, samples_to_ms
from transcription_worker.config import DecodeOptions
from transcription_worker.pipeline.merge import (
    dedupe_window_overlaps,
    merge_tracks,
    replace_range,
    to_common_time,
)
from transcription_worker.pipeline.types import LocalSegment
from transcription_worker.vad import windows_from_samples

from .conftest import KOTOBA, TURBO


def test_sample_ms_conversion_roundtrip() -> None:
    assert ms_to_samples(1000) == 16_000
    assert samples_to_ms(16_000) == 1000
    assert samples_to_ms(ms_to_samples(1234)) == 1234


def test_windows_from_samples_converts_and_splits_with_overlap() -> None:
    options = DecodeOptions(vad_max_window_ms=10_000, vad_overlap_ms=500)
    chunks = [(0, 16_000 * 2), (16_000 * 5, 16_000 * 30)]
    result = windows_from_samples(chunks, 16_000 * 30, options)
    assert result[0].start_ms == 0 and result[0].end_ms == 2000 and not result[0].overlaps_previous
    long_windows = result[1:]
    assert long_windows[0].start_ms == 5000 and long_windows[0].end_ms == 15_000
    assert long_windows[1].start_ms == 14_500 and long_windows[1].overlaps_previous
    assert long_windows[-1].end_ms == 30_000
    for previous, current in zip(long_windows, long_windows[1:], strict=False):
        assert previous.end_ms - current.start_ms == 500


def test_windows_clamped_to_audio_length() -> None:
    options = DecodeOptions()
    result = windows_from_samples([(0, 16_000 * 99)], 16_000 * 3, options)
    assert result[0].end_ms == 3000


def _local(start: int, end: int, text: str, model=TURBO, language: str = "ja") -> LocalSegment:
    return LocalSegment(start, end, text, language, 0.9, model, "vad_turbo")


def test_dedupe_window_overlaps_drops_duplicate_text_only_when_overlapping() -> None:
    segments = [
        _local(0, 1000, "こんにちは"),
        _local(900, 1900, "こんにちは"),  # 重なり窓の境界で重複
        _local(2000, 3000, "こんにちは"),  # 重ならないので保持 (相づちの繰り返し)
    ]
    result = dedupe_window_overlaps(segments, overlap_ms_threshold=100)
    assert [item.start_ms for item in result] == [0, 2000]


def test_replace_range_replaces_only_segments_inside_range_and_marks_review() -> None:
    segments = [_local(0, 1000, "a"), _local(1000, 2000, "b"), _local(2000, 3000, "c")]
    replacement = [_local(1000, 1500, "b1", KOTOBA), _local(1500, 2000, "b2", KOTOBA)]
    kept, removed = replace_range(segments, 1000, 2000, replacement, "rev-x")
    assert [item.text for item in removed] == ["b"]
    assert [item.text for item in kept] == ["a", "b1", "b2", "c"]
    assert all(item.replaced_by_review == "rev-x" for item in kept if item.text.startswith("b"))
    assert kept[0].replaced_by_review is None


def test_replace_range_keeps_segments_with_small_overlap() -> None:
    segments = [_local(0, 1200, "a"), _local(1200, 2000, "b")]
    kept, removed = replace_range(segments, 1000, 2000, [_local(1000, 2000, "B", KOTOBA)], "rev-y")
    assert [item.text for item in removed] == ["b"]
    assert kept[0].text == "a"


def test_to_common_time_adds_offset_and_labels_source() -> None:
    segments = to_common_time(
        [_local(500, 1500, "mic")], track_id="microphone", role="microphone", start_offset_ms=12,
        engine="faster-whisper", engine_version="1.2.1", id_prefix="seg-microphone",
    )
    assert segments[0].start_ms == 512 and segments[0].end_ms == 1512
    assert segments[0].source == "microphone" and segments[0].id == "seg-microphone-00001"


def _segment(track: str, source: str, start: int, end: int, text: str) -> Segment:
    return Segment(
        id=f"{track}-{start}", track_id=track, source=source, start_ms=start, end_ms=end, text=text, language="ja",
        language_probability=0.9, model_id=TURBO.model_id, model_revision=TURBO.revision, engine="faster-whisper",
        engine_version="1.2.1", strategy="vad_turbo",
    )


def test_merge_tracks_keeps_simultaneous_speech_from_both_tracks() -> None:
    app = [_segment("app-audio", "app", 1000, 3000, "app 側"), _segment("app-audio", "app", 5000, 6000, "後半")]
    mic = [_segment("microphone", "microphone", 1000, 2500, "mic 側 (同時刻)")]
    merged = merge_tracks([app, mic])
    assert [item.text for item in merged] == ["app 側", "mic 側 (同時刻)", "後半"]
    assert len(merged) == 3, "別トラックの同時刻を重複として削除しない"
