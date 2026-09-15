"""whisper.cpp の CLI 起動アダプター (ADR-0004 初期実装、GPU 計画 4〜5 節)。

* `-oj` で JSON を出力させ、stderr の system_info / ggml_vulkan 行から実効 backend を判定する。
* `requested_backend=vulkan` で実 GPU を識別できない (未検出・ソフトウェア描画) なら GpuUnavailable。
  CPU 実行結果を GPU 結果として返さない。
* 言語確率は CLI から得られないため None。単語時刻も出力しない。
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from audio_minutes_contracts.models import Backend

from transcription_worker.audio import write_wav_pcm16
from transcription_worker.engines.base import (
    Capabilities,
    EngineCrashed,
    EngineError,
    GpuUnavailable,
    LanguageGuess,
    ModelRef,
    RawSegment,
    TranscribeOptions,
    UnsupportedModel,
)

SOFTWARE_RENDERERS = ("llvmpipe", "lavapipe", "swiftshader", "softpipe")


@dataclass(frozen=True)
class BackendObservation:
    effective_backend: str  # cpu | vulkan | unknown
    gpu_verified: bool
    gpu_name: str | None
    software_renderer_detected: bool | None
    vulkan_devices: int
    use_gpu_flag: bool | None
    notes: tuple[str, ...] = ()


_VULKAN_FOUND = re.compile(r"ggml_vulkan: Found (\d+) Vulkan devices?")
_VULKAN_DEVICE = re.compile(r"ggml_vulkan: \d+ = (.+?) \|")
_USE_GPU = re.compile(r"use gpu\s*=\s*(\d)")
_NO_GPU = re.compile(r"no GPU found|failed to initialize (?:Vulkan|GPU)|falling back to CPU", re.IGNORECASE)
_VERSION = re.compile(r"whisper\.cpp\s+(?:version|build)[:\s]+([\w.\-]+)", re.IGNORECASE)


def observe_backend(stderr: str, requested: str) -> BackendObservation:
    """stderr から実効 backend を判定する。デバイスファイルの存在では GPU 成功にしない。"""
    devices = 0
    match = _VULKAN_FOUND.search(stderr)
    if match:
        devices = int(match.group(1))
    names = _VULKAN_DEVICE.findall(stderr)
    gpu_name = names[0].strip() if names else None
    software = None
    if gpu_name is not None:
        software = any(token in gpu_name.lower() for token in SOFTWARE_RENDERERS)
    use_gpu_match = _USE_GPU.search(stderr)
    use_gpu = (use_gpu_match.group(1) == "1") if use_gpu_match else None
    no_gpu = bool(_NO_GPU.search(stderr))
    notes: list[str] = []

    if requested == "cpu":
        if devices > 0 or use_gpu:
            notes.append("CPU 要求だが GPU が初期化された")
            return BackendObservation("unknown", False, gpu_name, software, devices, use_gpu, tuple(notes))
        return BackendObservation("cpu", False, None, None, 0, use_gpu, tuple(notes))

    # vulkan 要求
    if devices == 0 or no_gpu or use_gpu is False:
        notes.append("Vulkan デバイスが検出されなかった")
        return BackendObservation("cpu" if no_gpu or use_gpu is False else "unknown", False, gpu_name, software, devices, use_gpu, tuple(notes))
    if software:
        notes.append("ソフトウェア描画 (llvmpipe/lavapipe) を検出")
        return BackendObservation("cpu", False, gpu_name, True, devices, use_gpu, tuple(notes))
    return BackendObservation("vulkan", True, gpu_name, False, devices, use_gpu, tuple(notes))


def parse_cli_json(document: dict) -> tuple[list[RawSegment], str | None]:
    """whisper-cli の -oj 出力を RawSegment に変換する。offsets は ms。"""
    language = None
    result = document.get("result") or {}
    if isinstance(result, dict):
        language = result.get("language")
    segments: list[RawSegment] = []
    for item in document.get("transcription") or []:
        offsets = item.get("offsets") or {}
        text = (item.get("text") or "").strip()
        segments.append(
            RawSegment(
                start=float(offsets.get("from", 0)) / 1000.0,
                end=float(offsets.get("to", 0)) / 1000.0,
                text=text,
                language=language,
                language_probability=None,
                avg_logprob=None,
                no_speech_prob=None,
                words=None,
            )
        )
    return segments, language


class WhisperCppAdapter:
    engine_name = "whisper.cpp"

    def __init__(
        self,
        model: ModelRef,
        *,
        binary: str,
        model_dir: Path,
        threads: int,
        requested_backend: str = "cpu",
        timeout_seconds: int = 3600,
    ) -> None:
        self.model = model
        self._binary = binary
        self._model_dir = model_dir
        self._threads = threads
        self._requested = requested_backend
        self._timeout = timeout_seconds
        self._loaded = False
        self._process: subprocess.Popen | None = None
        self.last_observation: BackendObservation | None = None
        self.engine_version = "unknown"

    def backend(self) -> Backend:
        observed = self.last_observation
        return Backend(
            requested_backend=self._requested,  # type: ignore[arg-type]
            effective_backend=observed.effective_backend if observed else "unknown",  # type: ignore[arg-type]
            gpu_verified=bool(observed and observed.gpu_verified),
            gpu_name=observed.gpu_name if observed else None,
            software_renderer_detected=observed.software_renderer_detected if observed else None,
            fallback_reason=None,
        )

    @property
    def loaded(self) -> bool:
        return self._loaded

    def model_path(self) -> Path:
        if not self.model.local_file:
            raise UnsupportedModel(f"whisper.cpp 用の ggml ファイル名が未設定です: {self.model.model_id}")
        return self._model_dir / self.model.local_file

    def probe(self) -> Capabilities:
        notes: list[str] = []
        if not Path(self._binary).exists():
            notes.append(f"バイナリが見つかりません: {self._binary}")
        else:
            try:
                completed = subprocess.run([self._binary, "--help"], capture_output=True, text=True, timeout=30, check=False)
                version = _VERSION.search(completed.stdout + completed.stderr)
                if version:
                    self.engine_version = version.group(1)
            except (OSError, subprocess.TimeoutExpired) as error:
                notes.append(f"バイナリを実行できません: {error}")
        if not self.model_path().exists():
            notes.append(f"モデルファイルがありません: {self.model.local_file}")
        return Capabilities(
            engine="whisper.cpp",
            engine_version=self.engine_version,
            language_detection=True,  # -l auto。確率は返せない
            language_probability=False,
            word_timestamps=False,
            segment_timestamps=True,
            supported_backends=("cpu", "vulkan"),
            backend=self.backend(),
            model_loaded=self._loaded,
            notes=tuple(notes),
        )

    def load(self) -> None:
        if not Path(self._binary).exists():
            raise EngineError(f"whisper.cpp バイナリが見つかりません: {self._binary}")
        if not self.model_path().exists():
            raise UnsupportedModel(f"ggml モデルがありません: {self.model.local_file}")
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def detect_language(self, audio: np.ndarray) -> LanguageGuess | None:
        """`-l auto` で最初の区間の言語を得る。確率は取れないため None。"""
        _segments, language = self._run(audio, language=None, options=TranscribeOptions(), detect_only=True)
        if not language:
            return None
        return LanguageGuess(language=language, probability=None)

    def transcribe(self, audio: np.ndarray, language: str | None, options: TranscribeOptions) -> list[RawSegment]:
        segments, _ = self._run(audio, language=language, options=options)
        return segments

    def cancel(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.kill()

    def health(self) -> bool:
        return Path(self._binary).exists()

    def _run(
        self, audio: np.ndarray, *, language: str | None, options: TranscribeOptions, detect_only: bool = False
    ) -> tuple[list[RawSegment], str | None]:
        if not self._loaded:
            self.load()
        if audio.size == 0:
            return [], None
        with tempfile.TemporaryDirectory(prefix="am-cpp-") as tmp:
            wav_path = Path(tmp) / "input.wav"
            out_prefix = Path(tmp) / "out"
            write_wav_pcm16(wav_path, audio)
            command = [
                self._binary, "-m", str(self.model_path()), "-f", str(wav_path), "-oj", "-of", str(out_prefix),
                "-t", str(self._threads), "-bs", str(options.beam_size), "-l", language or "auto", "-nt",
            ]
            if self._requested == "cpu":
                command.append("-ng")  # GPU を使わない
            if detect_only:
                command.append("-dl")
            try:
                self._process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                stdout, stderr = self._process.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired as error:
                self.cancel()
                raise EngineError("whisper.cpp がタイムアウトしました", code="timeout", retryable=True) from error
            finally:
                process, self._process = self._process, None
            returncode = process.returncode if process else -1
            observation = observe_backend(stderr or "", self._requested)
            self.last_observation = observation
            if self._requested == "vulkan" and not observation.gpu_verified:
                raise GpuUnavailable("Vulkan GPU を実利用できませんでした: " + "; ".join(observation.notes))
            if returncode < 0 or returncode in (134, 139):
                raise EngineCrashed(f"whisper.cpp が異常終了しました (code={returncode})")
            if returncode != 0:
                raise EngineError(f"whisper.cpp が失敗しました (code={returncode})", retryable=False)
            json_path = out_prefix.with_suffix(".json")
            if detect_only:
                match = re.search(r"auto-detected language:\s*([a-z]{2})", (stderr or "") + (stdout or ""))
                return [], match.group(1) if match else None
            if not json_path.exists():
                raise EngineError("whisper.cpp の JSON 出力がありません", retryable=False)
            document = json.loads(json_path.read_text(encoding="utf-8"))
            return parse_cli_json(document)
