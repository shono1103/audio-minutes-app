#!/usr/bin/env python3
"""テスト用の合成音声を生成する。実会議音声は使わない。

  python3 scripts/make-test-audio.py [出力ディレクトリ (既定: tests/fixtures/audio)]

生成物 (16 kHz mono PCM16 WAV、数秒、数十〜百数十 KB):
  tone-3s.wav        0.5 秒無音 + 440 Hz 正弦波 2 秒 + 0.5 秒無音
  silence-2s.wav     2 秒無音 (VAD で音声なし、幻覚抑制の検査用)
  noise-1s.wav       1 秒の一様ノイズ (非音声)
  dual-app.wav       4 秒。0.5〜2.0 秒に 330 Hz (recorded_dual_track の app 側)
  dual-mic.wav       3.988 秒。2.488〜3.488 秒に 660 Hz (microphone 側、開始 offset 12 ms を想定)
  dual-track.json    2 トラックの offset・長さ・sha256 (recording-package の tracks 相当)
標準ライブラリだけで動く。
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import sys
import wave
from pathlib import Path

RATE = 16_000


def render(duration_s: float, parts: list[tuple[float, float, float, float]]) -> bytes:
    """parts: (start_s, end_s, freq_hz, amplitude)。区間外は無音。"""
    total = int(round(duration_s * RATE))
    samples = [0.0] * total
    for start_s, end_s, freq, amplitude in parts:
        start = int(round(start_s * RATE))
        end = min(total, int(round(end_s * RATE)))
        for index in range(start, end):
            t = (index - start) / RATE
            envelope = min(1.0, t / 0.02, (end - index) / RATE / 0.02)  # 20 ms のフェードでクリックを避ける
            samples[index] += amplitude * envelope * math.sin(2 * math.pi * freq * t)
    return struct.pack("<%dh" % total, *[int(max(-1.0, min(1.0, s)) * 32767) for s in samples])


def render_noise(duration_s: float, amplitude: float = 0.2, seed: int = 20260912) -> bytes:
    import random

    rng = random.Random(seed)
    total = int(round(duration_s * RATE))
    return struct.pack("<%dh" % total, *[int(rng.uniform(-amplitude, amplitude) * 32767) for _ in range(total)])


def write_wav(path: Path, frames: bytes) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(frames)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "audio"
    out.mkdir(parents=True, exist_ok=True)
    write_wav(out / "tone-3s.wav", render(3.0, [(0.5, 2.5, 440.0, 0.5)]))
    write_wav(out / "silence-2s.wav", render(2.0, []))
    write_wav(out / "noise-1s.wav", render_noise(1.0))
    write_wav(out / "dual-app.wav", render(4.0, [(0.5, 2.0, 330.0, 0.5)]))
    write_wav(out / "dual-mic.wav", render(3.988, [(2.488, 3.488, 660.0, 0.5)]))
    manifest = {
        "input_kind": "recorded_dual_track",
        "tracks": [
            {"track_id": "app-audio", "role": "app", "file": "dual-app.wav", "start_offset_ms": 0, "duration_ms": 4000,
             "sample_rate": RATE, "channels": 1, "codec": "pcm_s16le", "container": "wav",
             "byte_size": (out / "dual-app.wav").stat().st_size, "sha256": sha256(out / "dual-app.wav")},
            {"track_id": "microphone", "role": "microphone", "file": "dual-mic.wav", "start_offset_ms": 12, "duration_ms": 3988,
             "sample_rate": RATE, "channels": 1, "codec": "pcm_s16le", "container": "wav",
             "byte_size": (out / "dual-mic.wav").stat().st_size, "sha256": sha256(out / "dual-mic.wav")},
        ],
        "note": "合成音声。app は 500-2000 ms に 330 Hz、mic は共通時刻 2500-3500 ms に 660 Hz",
    }
    (out / "dual-track.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "README.md").write_text(
        "# tests/fixtures/audio\n\n`scripts/make-test-audio.py` が生成する合成音声。実会議音声・公開データセットは置かない。\n"
        "内容と用途はスクリプト先頭の docstring と `dual-track.json` を参照する。\n",
        encoding="utf-8",
    )
    for path in sorted(out.iterdir()):
        print(f"{path.name}\t{path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
