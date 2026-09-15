"""ffmpeg による入力検査・decode・16 kHz mono float32 正規化。元ファイルは変更しない。"""

from __future__ import annotations

import json
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from audio_minutes_contracts.models import Normalization

TARGET_SAMPLE_RATE = 16_000
SUPPORTED_CONTAINERS = {"wav", "m4a", "mp3", "flac"}
SUPPORTED_CODECS = {"pcm_s16le", "pcm_s24le", "pcm_f32le", "pcm_s32le", "aac", "mp3", "flac"}


class AudioInputError(ValueError):
    """非対応形式・破損・上限超過。retryable ではない。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProbeResult:
    container: str
    codec: str
    sample_rate: int
    channels: int
    duration_ms: int


def probe(path: Path) -> ProbeResult:
    """拡張子ではなく実形式を ffprobe で検査する。"""
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                "-select_streams", "a:0", str(path),
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except FileNotFoundError as error:
        raise AudioInputError("decode_failed", "ffprobe が見つかりません") from error
    if completed.returncode != 0:
        raise AudioInputError("unsupported_format", "音声として解析できません")
    info = json.loads(completed.stdout or "{}")
    streams = info.get("streams") or []
    if not streams:
        raise AudioInputError("unsupported_format", "音声ストリームがありません")
    stream = streams[0]
    fmt = info.get("format", {})
    format_name = fmt.get("format_name", "")
    container = _normalize_container(format_name)
    codec = stream.get("codec_name", "")
    if container is None or codec not in SUPPORTED_CODECS:
        raise AudioInputError("unsupported_format", f"非対応の形式です ({format_name}/{codec})")
    duration = stream.get("duration") or fmt.get("duration")
    if duration is None:
        raise AudioInputError("unsupported_format", "再生時間を取得できません")
    return ProbeResult(
        container=container,
        codec=codec,
        sample_rate=int(stream.get("sample_rate", 0) or 0),
        channels=int(stream.get("channels", 0) or 0),
        duration_ms=int(round(float(duration) * 1000)),
    )


def _normalize_container(format_name: str) -> str | None:
    if format_name == "wav":
        return "wav"
    if format_name.startswith("mov,mp4"):
        return "m4a"
    if format_name == "mp3":
        return "mp3"
    if format_name == "flac":
        return "flac"
    return None


def decode_to_float32(path: Path, sample_rate: int = TARGET_SAMPLE_RATE) -> np.ndarray:
    """ffmpeg で 16 kHz mono float32 の numpy 配列へ変換する。"""
    command = [
        "ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=3600, check=False)
    except FileNotFoundError as error:
        raise AudioInputError("decode_failed", "ffmpeg が見つかりません") from error
    if completed.returncode != 0:
        raise AudioInputError("decode_failed", "音声の decode に失敗しました")
    audio = np.frombuffer(completed.stdout, dtype=np.float32)
    if audio.size == 0:
        raise AudioInputError("decode_failed", "decode 結果が空です")
    return audio


def normalization_for(probe_result: ProbeResult, sample_rate: int = TARGET_SAMPLE_RATE) -> Normalization:
    return Normalization(
        original_container=probe_result.container,
        original_codec=probe_result.codec,
        original_sample_rate=probe_result.sample_rate,
        original_channels=probe_result.channels,
        target_sample_rate=sample_rate,
        target_channels=1,
    )


def write_wav_pcm16(path: Path, audio: np.ndarray, sample_rate: int = TARGET_SAMPLE_RATE) -> None:
    """whisper.cpp CLI 入力などのために 16-bit PCM WAV を書く。"""
    clipped = np.clip(audio, -1.0, 1.0)
    samples = (clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())


def read_wav_float32(path: Path) -> tuple[np.ndarray, int]:
    """テスト用の軽量 WAV 読み込み (ffmpeg を使わない)。"""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        width = handle.getsampwidth()
        channels = handle.getnchannels()
        frames = handle.readframes(handle.getnframes())
    if width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise AudioInputError("unsupported_format", f"サンプル幅 {width} は未対応")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def ms_to_samples(ms: int, sample_rate: int = TARGET_SAMPLE_RATE) -> int:
    return int(round(ms * sample_rate / 1000))


def samples_to_ms(samples: int, sample_rate: int = TARGET_SAMPLE_RATE) -> int:
    return int(round(samples * 1000 / sample_rate))


def seconds_to_ms(seconds: float) -> int:
    return int(round(seconds * 1000))
