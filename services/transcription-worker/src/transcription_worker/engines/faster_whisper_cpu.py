"""faster-whisper (CTranslate2) の CPU アダプター。GPU・Metal を初期化しない。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
from audio_minutes_contracts.models import Backend

from transcription_worker.engines.base import (
    Capabilities,
    EngineError,
    LanguageGuess,
    ModelRef,
    RawSegment,
    RawWord,
    TranscribeOptions,
    UnsupportedModel,
)


def resolve_model_path(model: ModelRef, models_dir: Path, offline: bool) -> str:
    """HF cache から revision 固定でモデルディレクトリを解決する。cache になければ offline では失敗。"""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    os.environ.setdefault("HF_HOME", str(models_dir))
    try:
        return snapshot_download(
            repo_id=model.model_id,
            revision=model.revision,
            cache_dir=str(models_dir / "hub"),
            local_files_only=offline,
        )
    except LocalEntryNotFoundError as error:
        raise UnsupportedModel(
            f"モデルが事前取得されていません: {model.model_id}@{model.revision[:8]}"
        ) from error


class FasterWhisperCpuAdapter:
    engine_name = "faster-whisper"

    def __init__(self, model: ModelRef, *, models_dir: Path, threads: int, offline: bool = True) -> None:
        self.model = model
        self._models_dir = models_dir
        self._threads = threads
        self._offline = offline
        self._model = None
        self._cancelled = False
        self.load_seconds: float | None = None

    # --- 契約 ---------------------------------------------------------------

    def backend(self) -> Backend:
        return Backend(requested_backend="cpu", effective_backend="cpu", gpu_verified=False)

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def probe(self) -> Capabilities:
        import faster_whisper

        return Capabilities(
            engine="faster-whisper",
            engine_version=getattr(faster_whisper, "__version__", "unknown"),
            language_detection=True,
            language_probability=True,
            word_timestamps=True,
            segment_timestamps=True,
            supported_backends=("cpu",),
            backend=self.backend(),
            model_loaded=self.loaded,
            notes=("compute_type=" + (self.model.compute_type or "int8"),),
        )

    def load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        path = resolve_model_path(self.model, self._models_dir, self._offline)
        started = time.perf_counter()
        try:
            self._model = WhisperModel(
                path,
                device="cpu",
                compute_type=self.model.compute_type or "int8",
                cpu_threads=self._threads,
                num_workers=1,
            )
        except (ValueError, RuntimeError) as error:
            raise UnsupportedModel(f"モデルをロードできません: {self.model.model_id}") from error
        self.load_seconds = time.perf_counter() - started

    def unload(self) -> None:
        self._model = None

    def detect_language(self, audio: np.ndarray) -> LanguageGuess | None:
        self._ensure_loaded()
        if audio.size == 0:
            return None
        language, probability, all_probs = self._model.detect_language(audio=audio)  # type: ignore[union-attr]
        table = {code: float(prob) for code, prob in (all_probs or [])} if all_probs else {}
        return LanguageGuess(language=language, probability=float(probability), all_probabilities=table)

    def transcribe(self, audio: np.ndarray, language: str | None, options: TranscribeOptions) -> list[RawSegment]:
        self._ensure_loaded()
        self._cancelled = False
        if audio.size == 0:
            return []
        segments, info = self._model.transcribe(  # type: ignore[union-attr]
            audio,
            language=language,
            beam_size=options.beam_size,
            vad_filter=False,
            word_timestamps=options.word_timestamps,
            condition_on_previous_text=options.condition_on_previous_text,
            initial_prompt=options.initial_prompt,
        )
        detected = getattr(info, "language", language)
        detected_prob = getattr(info, "language_probability", None)
        results: list[RawSegment] = []
        for segment in segments:
            if self._cancelled:
                raise EngineError("cancelled", code="cancelled", retryable=False)
            words = None
            if options.word_timestamps and getattr(segment, "words", None):
                words = [
                    RawWord(float(word.start), float(word.end), word.word, getattr(word, "probability", None))
                    for word in segment.words
                ]
            results.append(
                RawSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=segment.text.strip(),
                    language=detected if language is None else language,
                    language_probability=float(detected_prob) if language is None and detected_prob is not None else None,
                    avg_logprob=float(segment.avg_logprob) if segment.avg_logprob is not None else None,
                    no_speech_prob=float(segment.no_speech_prob) if segment.no_speech_prob is not None else None,
                    words=words,
                )
            )
        return results

    def cancel(self) -> None:
        self._cancelled = True

    def health(self) -> bool:
        return True

    def _ensure_loaded(self) -> None:
        if self._model is None:
            self.load()
