"""JSON Schema の読み込みと検証。schema 本体は contracts/schemas/ (パッケージ同梱の schemas/ から参照)。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA_NAMES = (
    "recording-package",
    "job",
    "worker-result",
    "transcript",
    "minutes-version",
    "format-profile",
    "session",
    "error",
)


def schema_dir() -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "schemas",
        Path(__file__).resolve().parents[4] / "contracts" / "schemas",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("contracts/schemas が見つかりません")


def schema_path(name: str, version: int = 1) -> Path:
    return schema_dir() / f"{name}.v{version}.schema.json"


@lru_cache(maxsize=None)
def load_schema(name: str, version: int = 1) -> dict[str, Any]:
    with schema_path(name, version).open(encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=1)
def _registry() -> Registry:
    resources = []
    for name in SCHEMA_NAMES:
        document = load_schema(name)
        resource = Resource.from_contents(document)
        resources.append((document["$id"], resource))
        # 相対参照 (例: "error.v1.schema.json#/$defs/job_failure") も解決できるようにする
        resources.append((f"{name}.v1.schema.json", resource))
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def validator(name: str, version: int = 1) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name, version), registry=_registry())


def validate(name: str, document: Any, version: int = 1) -> None:
    """検証に失敗したら jsonschema.ValidationError を送出する。"""
    validator(name, version).validate(document)


def errors(name: str, document: Any, version: int = 1) -> list[str]:
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in validator(name, version).iter_errors(document)
    ]
