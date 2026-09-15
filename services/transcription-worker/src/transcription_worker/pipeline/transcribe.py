"""ジョブ入力 → Transcript。contracts の schema で検証してから返す。"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from audio_minutes_contracts import schemas
from audio_minutes_contracts.models import (
    Backend,
    InputKind,
    LanguageMode,
    Processing,
    ProcessingModel,
    ReviewAttempt,
    ReviewItem,
    Timings,
    Transcript,
    TranscriptTrack,
)

from transcription_worker.audio import (
    TARGET_SAMPLE_RATE,
    AudioInputError,
    decode_to_float32,
    normalization_for,
    probe,
    samples_to_ms,
)
from transcription_worker.config import WorkerConfig
from transcription_worker.engines.base import ModelRef, TranscriptionEngine
from transcription_worker.engines.manager import ModelManager
from transcription_worker.pipeline.merge import merge_tracks, to_common_time
from transcription_worker.pipeline.strategies import run_strategy, strategy_label
from transcription_worker.pipeline.types import TrackContext
from transcription_worker.resources import (
    cgroup_memory_peak_bytes,
    describe_resources,
    process_peak_rss_bytes,
    recommended_resident_models,
)
from transcription_worker.vad import detect_windows

MAX_AUDIO_MS = 14_400_000


@dataclass(frozen=True)
class TrackInput:
    track_id: str
    role: str  # app | microphone | mixed
    path: Path
    source_artifact_id: str
    start_offset_ms: int = 0


@dataclass(frozen=True)
class TranscribeRequest:
    session_id: UUID
    revision: int
    input_kind: str
    language_mode: str
    tracks: list[TrackInput]
    strategy: str
    requested_backend: str = "cpu"
    fallback_reason: str | None = None


def model_refs(config: WorkerConfig) -> dict[str, ModelRef]:
    refs = {
        "multilingual": ModelRef(
            "multilingual",
            config.model_multilingual.model_id,
            config.model_multilingual.revision,
            compute_type=config.model_multilingual.compute_type if config.engine == "faster-whisper" else None,
            quantization=config.model_multilingual.quantization,
            local_file=config.model_multilingual.ggml_file,
        ),
        "ja": ModelRef(
            "ja",
            config.model_ja.model_id,
            config.model_ja.revision,
            compute_type=config.model_ja.compute_type if config.engine == "faster-whisper" else None,
            quantization=config.model_ja.quantization,
            local_file=config.model_ja.ggml_file,
        ),
    }
    return refs


def engine_factory(config: WorkerConfig) -> Callable[[ModelRef], TranscriptionEngine]:
    if config.engine == "faster-whisper":
        if config.backend != "cpu":
            raise ValueError("faster-whisper は CPU 経路だけを提供する")
        from transcription_worker.engines.faster_whisper_cpu import FasterWhisperCpuAdapter

        return lambda ref: FasterWhisperCpuAdapter(
            ref, models_dir=config.models_dir, threads=config.threads, offline=config.hf_offline
        )
    from transcription_worker.engines.whisper_cpp import WhisperCppAdapter

    return lambda ref: WhisperCppAdapter(
        ref,
        binary=config.whisper_cpp_bin,
        model_dir=config.whisper_cpp_model_dir,
        threads=config.threads,
        requested_backend=config.backend,
    )


def build_manager(config: WorkerConfig, resources_limit: int | None = None) -> ModelManager:
    limit = resources_limit if resources_limit is not None else describe_resources(config.threads).memory_limit_bytes
    resident = config.max_resident_models or recommended_resident_models(limit)
    return ModelManager(engine_factory(config), model_refs(config), max_resident=resident)  # type: ignore[arg-type]


def _expected_tracks(input_kind: str) -> set[str]:
    return {"app-audio", "microphone"} if input_kind == "recorded_dual_track" else {"imported-audio"}


def transcribe_request(
    request: TranscribeRequest,
    config: WorkerConfig,
    manager: ModelManager | None = None,
    progress: Callable[[dict], None] | None = None,
) -> Transcript:
    total_started = time.perf_counter()
    manager = manager or build_manager(config)
    expected = _expected_tracks(request.input_kind)
    provided = {track.track_id for track in request.tracks}
    if provided != expected:
        raise AudioInputError("invalid_input", f"入力種別 {request.input_kind} に必要なトラックが揃っていません")

    # probe はモデルをロードせずに行う (engine 版・能力の記録用)
    factory = engine_factory(config)
    probe_engine = factory(model_refs(config)["multilingual"])
    capabilities = probe_engine.probe()
    engine_version = capabilities.engine_version

    per_track_segments = []
    reviews: list[ReviewItem] = []
    transcript_tracks: list[TranscriptTrack] = []
    timings = {"decode_audio_ms": 0, "vad_ms": 0, "transcribe_ms": 0, "language_detection_ms": 0, "review_retry_ms": 0, "merge_ms": 0}
    audio_duration_ms = 0

    for track in request.tracks:
        info = probe(track.path)
        if info.duration_ms > MAX_AUDIO_MS:
            raise AudioInputError("limit_exceeded", "音声が 4 時間の上限を超えています")
        decode_started = time.perf_counter()
        audio = decode_to_float32(track.path, TARGET_SAMPLE_RATE)
        timings["decode_audio_ms"] += int((time.perf_counter() - decode_started) * 1000)
        duration_ms = samples_to_ms(len(audio), TARGET_SAMPLE_RATE)
        audio_duration_ms = max(audio_duration_ms, track.start_offset_ms + duration_ms)

        vad_started = time.perf_counter()
        windows = detect_windows(audio, config.decode, TARGET_SAMPLE_RATE)
        timings["vad_ms"] += int((time.perf_counter() - vad_started) * 1000)

        ctx = TrackContext(
            track_id=track.track_id,
            audio=audio,
            windows=windows,
            options=config.decode,
            manager=manager,
            engine_name=config.engine,
            engine_version=engine_version,
            strategy_name=request.strategy,
            sample_rate=TARGET_SAMPLE_RATE,
            progress=(lambda detail, _track=track.track_id: progress({"track_id": _track, **detail})) if progress else None,
        )
        result = run_strategy(ctx, request.language_mode)
        for key, value in result.timings.items():
            timings[key] = timings.get(key, 0) + value

        merge_started = time.perf_counter()
        segments = to_common_time(
            result.segments,
            track_id=track.track_id,
            role=track.role,  # type: ignore[arg-type]
            start_offset_ms=track.start_offset_ms,
            engine=config.engine,
            engine_version=engine_version,
            id_prefix=f"seg-{track.track_id}",
        )
        per_track_segments.append(segments)
        for review in result.review:
            reviews.append(
                ReviewItem(
                    id=review.id,
                    track_id=track.track_id,
                    start_ms=review.start_ms + track.start_offset_ms,
                    end_ms=review.end_ms + track.start_offset_ms,
                    reason=review.reason,  # type: ignore[arg-type]
                    attempts=[
                        ReviewAttempt(
                            attempt=attempt.attempt,
                            model_id=attempt.model.model_id,
                            model_revision=attempt.model.revision,
                            language=attempt.language if attempt.language in ("ja", "en") else ("und" if attempt.language else None),  # type: ignore[arg-type]
                            language_probability=attempt.language_probability,
                            candidate_text=attempt.candidate_text,
                            accepted=attempt.accepted,
                            reason=attempt.reason,
                        )
                        for attempt in review.attempts
                    ],
                    resolved=review.resolved,
                )
            )
        timings["merge_ms"] += int((time.perf_counter() - merge_started) * 1000)
        transcript_tracks.append(
            TranscriptTrack(
                track_id=track.track_id,
                role=track.role,  # type: ignore[arg-type]
                source_artifact_id=track.source_artifact_id,
                start_offset_ms=track.start_offset_ms,
                duration_ms=duration_ms,
                normalization=normalization_for(info, TARGET_SAMPLE_RATE),
            )
        )

    merge_started = time.perf_counter()
    segments = merge_tracks(per_track_segments)
    timings["merge_ms"] += int((time.perf_counter() - merge_started) * 1000)

    used_models = _used_models(manager, config)
    backend = _backend(manager, config, request)
    total_ms = int((time.perf_counter() - total_started) * 1000)
    transcribe_ms = timings["transcribe_ms"] + timings["language_detection_ms"] + timings["review_retry_ms"]
    resources = describe_resources(config.threads).model_copy(
        update={"peak_memory_bytes": cgroup_memory_peak_bytes() or process_peak_rss_bytes()}
    )
    processing = Processing(
        engine=config.engine,
        engine_version=engine_version,
        strategy=strategy_label(request.language_mode, request.strategy),  # type: ignore[arg-type]
        models=used_models,
        decode={**config.decode.as_dict(), "measurement": "resident" if config.engine == "faster-whisper" else "cli_per_window"},
        backend=backend,
        resources=resources,
        timings=Timings(
            total_ms=total_ms,
            decode_audio_ms=timings["decode_audio_ms"],
            vad_ms=timings["vad_ms"],
            language_detection_ms=timings["language_detection_ms"],
            model_load_ms=_model_load_ms(manager),
            transcribe_ms=timings["transcribe_ms"],
            review_retry_ms=timings["review_retry_ms"],
            merge_ms=timings["merge_ms"],
            audio_duration_ms=audio_duration_ms,
            rtf=(transcribe_ms / audio_duration_ms) if audio_duration_ms > 0 else None,
        ),
    )
    transcript = Transcript(
        session_id=request.session_id,
        revision=request.revision,
        input_kind=InputKind(request.input_kind),
        language_mode=LanguageMode(request.language_mode),
        tracks=transcript_tracks,
        segments=segments,
        review=reviews,
        processing=processing,
    )
    schemas.validate("transcript", json.loads(transcript.model_dump_json()))
    return transcript


def _used_models(manager: ModelManager, config: WorkerConfig) -> list[ProcessingModel]:
    models: list[ProcessingModel] = []
    for profile, engine in manager.engines().items():
        ref = engine.model
        models.append(
            ProcessingModel(
                profile=profile,  # type: ignore[arg-type]
                model_id=ref.model_id,
                model_revision=ref.revision,
                compute_type=ref.compute_type,
                quantization=ref.quantization,
                file_sha256=None,
            )
        )
    if not models:
        ref = model_refs(config)["multilingual"]
        models.append(ProcessingModel(profile="multilingual", model_id=ref.model_id, model_revision=ref.revision, compute_type=ref.compute_type, quantization=ref.quantization))
    return models


def _backend(manager: ModelManager, config: WorkerConfig, request: TranscribeRequest) -> Backend:
    engines = manager.engines()
    base = engines.get("multilingual") or next(iter(engines.values()), None)
    if base is not None:
        backend = base.backend()
    else:
        backend = Backend(requested_backend=config.backend, effective_backend="cpu" if config.backend == "cpu" else "unknown", gpu_verified=False)
    return backend.model_copy(update={"vm": os.environ.get("AM_VM_LABEL"), "fallback_reason": request.fallback_reason})


def _model_load_ms(manager: ModelManager) -> int | None:
    total = 0.0
    seen = False
    for engine in manager.engines().values():
        seconds = getattr(engine, "load_seconds", None)
        if seconds is not None:
            total += seconds
            seen = True
    return int(total * 1000) if seen else None
