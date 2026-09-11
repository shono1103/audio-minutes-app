"""fixture が JSON Schema と pydantic の両方に適合し、双方の写しが一致することを確認する。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audio_minutes_contracts import models, schemas

FIXTURES = Path(__file__).resolve().parents[4] / "contracts" / "fixtures"

CASES = [
    ("recording-package", "recording-package.dual-track.json", models.RecordingPackage),
    ("recording-package", "recording-package.imported.json", models.RecordingPackage),
    ("job", "job.transcription.json", models.Job),
    ("worker-result", "worker-result.transcription.json", models.WorkerResult),
    ("transcript", "transcript.dual-track.json", models.Transcript),
    ("format-profile", "format-profile.standard.json", models.FormatProfile),
    ("minutes-version", "minutes-version.claude.json", models.MinutesVersion),
    ("error", "error.claude-not-authenticated.json", models.ApiError),
    ("session", "session.completed.json", models.Session),
]


@pytest.mark.parametrize("schema_name, fixture, model", CASES, ids=[case[1] for case in CASES])
def test_fixture_matches_schema_and_model(schema_name: str, fixture: str, model: type) -> None:
    document = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    assert schemas.errors(schema_name, document) == []
    parsed = model.model_validate(document)
    roundtrip = json.loads(parsed.model_dump_json(exclude_none=False))
    assert schemas.errors(schema_name, roundtrip) == [], "pydantic の出力も schema に適合する"


def test_required_track_ids() -> None:
    package = models.RecordingPackage.model_validate(
        json.loads((FIXTURES / "recording-package.dual-track.json").read_text())
    )
    assert package.required_track_ids() == ["app-audio", "microphone"]


def test_schema_rejects_pid_or_path_fields() -> None:
    document = json.loads((FIXTURES / "recording-package.dual-track.json").read_text())
    document["source"]["pid"] = 1234
    assert schemas.errors("recording-package", document)
    with pytest.raises(Exception):
        models.RecordingPackage.model_validate(document)


def test_transcript_markdown_keeps_order_and_sources() -> None:
    transcript = models.Transcript.model_validate(json.loads((FIXTURES / "transcript.dual-track.json").read_text()))
    markdown = transcript.to_markdown()
    assert markdown.index("(app/ja)") < markdown.index("(microphone/en)")
    assert "⚠" in markdown


def test_job_failure_rejects_unknown_code() -> None:
    document = json.loads((FIXTURES / "error.claude-not-authenticated.json").read_text())
    document["error"]["code"] = "something_else"
    assert schemas.errors("error", document)
