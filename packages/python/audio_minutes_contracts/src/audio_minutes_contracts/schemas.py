"""JSON Schema の読み込みと検証。schema 本体は contracts/schemas/ (パッケージ同梱の schemas/ から参照)。"""

from __future__ import annotations

import json
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

# schema ごとの現行版。版を上げたらここと contracts/schemas/ のファイル名を一緒に変える。
SCHEMA_VERSIONS = {
    "recording-package": 2,
    "job": 1,
    "worker-result": 1,
    "transcript": 1,
    "minutes-version": 1,
    "format-profile": 1,
    "session": 1,
    "error": 1,
}
SCHEMA_NAMES = tuple(SCHEMA_VERSIONS)


def current_version(name: str) -> int:
    try:
        return SCHEMA_VERSIONS[name]
    except KeyError as exc:
        raise KeyError(f"未知の schema です: {name}") from exc


def schema_dir() -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "schemas",
        Path(__file__).resolve().parents[4] / "contracts" / "schemas",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("contracts/schemas が見つかりません")


def schema_path(name: str, version: int | None = None) -> Path:
    return schema_dir() / f"{name}.v{version or current_version(name)}.schema.json"


@cache
def load_schema(name: str, version: int | None = None) -> dict[str, Any]:
    with schema_path(name, version).open(encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=1)
def _registry() -> Registry:
    resources = []
    for name, version in SCHEMA_VERSIONS.items():
        document = load_schema(name)
        resource = Resource.from_contents(document)
        resources.append((document["$id"], resource))
        # 相対参照 (例: "error.v1.schema.json#/$defs/job_failure") も解決できるようにする
        resources.append((f"{name}.v{version}.schema.json", resource))
    return Registry().with_resources(resources)


@cache
def validator(name: str, version: int | None = None) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name, version), registry=_registry())


def validate(name: str, document: Any, version: int | None = None) -> None:
    """検証に失敗したら jsonschema.ValidationError を送出する。"""
    validator(name, version).validate(document)


def errors(name: str, document: Any, version: int | None = None) -> list[str]:
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in validator(name, version).iter_errors(document)
    ]
