from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from audio_minutes_contracts.models import FormatProfile, Transcript

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "contracts" / "fixtures"
MOCK_BIN = str(Path(__file__).resolve().parents[1] / "mock_claude.py")


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    home = tmp_path / "claude-home"
    home.mkdir()
    return home


@pytest.fixture
def clean_environ() -> dict[str, str]:
    """CLI 子プロセスへ渡す最小環境。PATH だけ実環境から受け継ぐ。"""
    return {"PATH": os.environ["PATH"], "LANG": "C.UTF-8"}


@pytest.fixture
def transcript() -> Transcript:
    return Transcript.model_validate(json.loads((FIXTURES / "transcript.dual-track.json").read_text(encoding="utf-8")))


@pytest.fixture
def standard_profile() -> FormatProfile:
    return FormatProfile.model_validate(
        json.loads((FIXTURES / "format-profile.standard.json").read_text(encoding="utf-8"))
    )
