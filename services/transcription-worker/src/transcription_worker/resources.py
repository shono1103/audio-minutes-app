"""CPU / メモリ資源の検査。cgroup 値を Apple GPU 込みの総消費量とは呼ばない。"""

from __future__ import annotations

import os
import platform
import resource
import sys
from pathlib import Path

from audio_minutes_contracts.models import Resources


def cgroup_memory_limit_bytes() -> int | None:
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        candidate = Path(path)
        if candidate.is_file():
            value = candidate.read_text().strip()
            if value in ("max", "") or not value.isdigit():
                return None
            number = int(value)
            return None if number >= 2**62 else number
    return None


def cgroup_memory_peak_bytes() -> int | None:
    for path in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):
        candidate = Path(path)
        if candidate.is_file():
            value = candidate.read_text().strip()
            if value.isdigit():
                return int(value)
    return None


def process_peak_rss_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS はバイト、Linux は KiB
    return usage if sys.platform == "darwin" else usage * 1024


def cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except AttributeError:
        return os.cpu_count() or 1


def describe_resources(threads: int) -> Resources:
    return Resources(
        cpu_arch=platform.machine(),
        cpu_count=cpu_count(),
        memory_limit_bytes=cgroup_memory_limit_bytes(),
        peak_memory_bytes=None,
        threads=threads,
    )


def recommended_resident_models(limit_bytes: int | None, model_bytes_estimate: int = 2_500_000_000) -> int:
    """順次ロードか 2 モデル常駐かを決める。上限不明なら安全側で 1。"""
    if limit_bytes is None:
        return 1
    return 2 if limit_bytes >= model_bytes_estimate * 3 else 1
