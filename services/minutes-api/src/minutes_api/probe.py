"""ffprobe による実形式検査。拡張子・申告 MIME を信用せず、timeout 付き subprocess で隔離する。"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from minutes_api.config import get_settings

CONTAINER_BY_FORMAT = {
    "wav": "wav",
    "mov,mp4,m4a,3gp,3g2,mj2": "m4a",
    "mp3": "mp3",
    "flac": "flac",
}
CODEC_MAP = {
    "pcm_s16le": "pcm_s16le",
    "pcm_s24le": "pcm_s24le",
    "pcm_f32le": "pcm_f32le",
    "aac": "aac",
    "mp3": "mp3",
    "mp3float": "mp3",
    "flac": "flac",
}


@dataclass(frozen=True)
class ProbeResult:
    container: str
    codec: str
    sample_rate: int
    channels: int
    duration_ms: int


class ProbeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def probe_audio(path: Path, timeout_seconds: int = 60) -> ProbeResult:
    command = [
        get_settings().ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout_seconds, check=False)
    except FileNotFoundError as exc:
        raise ProbeError("internal", "ffprobe が見つかりません") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("decode_failed", "音声の検査がタイムアウトしました") from exc
    if completed.returncode != 0:
        raise ProbeError("unsupported_format", "音声ファイルとして読み取れません")
    try:
        info = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ProbeError("decode_failed", "音声情報を解釈できません") from exc
    audio_streams = [stream for stream in info.get("streams", []) if stream.get("codec_type") == "audio"]
    if not audio_streams:
        raise ProbeError("unsupported_format", "音声ストリームがありません")
    stream = audio_streams[0]
    format_name = info.get("format", {}).get("format_name", "")
    container = CONTAINER_BY_FORMAT.get(format_name)
    if container is None:
        raise ProbeError("unsupported_format", "対応していないコンテナ形式です")
    codec = CODEC_MAP.get(stream.get("codec_name", ""))
    if codec is None:
        raise ProbeError("unsupported_format", "対応していない音声 codec です")
    duration_seconds = stream.get("duration") or info.get("format", {}).get("duration")
    if duration_seconds is None:
        raise ProbeError("decode_failed", "再生時間を取得できません")
    return ProbeResult(
        container=container,
        codec=codec,
        sample_rate=int(stream.get("sample_rate", 0) or 0),
        channels=int(stream.get("channels", 0) or 0),
        duration_ms=int(round(float(duration_seconds) * 1000)),
    )
