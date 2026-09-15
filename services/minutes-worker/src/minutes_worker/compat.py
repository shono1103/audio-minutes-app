"""claude_compat.json の読み込み。対応表にない CLI 版では推測したフローを実行しない (FR-132)。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


def compat_path() -> Path:
    return Path(__file__).resolve().parents[2] / "claude_compat.json"


@lru_cache(maxsize=1)
def load_compat() -> dict[str, Any]:
    with compat_path().open(encoding="utf-8") as handle:
        return json.load(handle)


@dataclass(frozen=True)
class VersionCheck:
    version: str | None
    supported: bool


def parse_version(output: str, compat: dict[str, Any] | None = None) -> str | None:
    compat = compat or load_compat()
    match = re.search(compat["version_pattern"], output.strip())
    return match.group(1) if match else None


def check_version(output: str, compat: dict[str, Any] | None = None) -> VersionCheck:
    compat = compat or load_compat()
    version = parse_version(output, compat)
    return VersionCheck(version=version, supported=version in compat["supported_versions"])
