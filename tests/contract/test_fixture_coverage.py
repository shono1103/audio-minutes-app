"""contracts/schemas の全 schema に少なくとも 1 つの fixture があり、fixture が schema 名で解決できることを確認する。"""

from __future__ import annotations

import json
from pathlib import Path

from audio_minutes_contracts import schemas

ROOT = Path(__file__).resolve().parents[2] / "contracts"


def test_every_schema_has_fixture() -> None:
    schema_names = {path.name.split(".v1.schema.json")[0] for path in (ROOT / "schemas").glob("*.v1.schema.json")}
    fixture_names = {path.name.split(".")[0] for path in (ROOT / "fixtures").glob("*.json")}
    assert schema_names <= fixture_names, schema_names - fixture_names


def test_every_fixture_validates() -> None:
    for path in sorted((ROOT / "fixtures").glob("*.json")):
        name = path.name.split(".")[0]
        document = json.loads(path.read_text(encoding="utf-8"))
        assert schemas.errors(name, document) == [], path.name
