from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from audio_minutes_contracts.artifacts import ChecksumMismatch, LocalArtifactStore
from audio_minutes_contracts.ids import is_artifact_id, new_artifact_id


def test_commit_publishes_immutable_id(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    stored = store.put_bytes(b"hello")
    assert is_artifact_id(stored.artifact_id)
    assert stored.byte_size == 5
    assert stored.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert store.exists(stored.artifact_id)
    assert not (tmp_path / "tmp" / f"{stored.artifact_id}.part").exists()
    assert list(store.iter_ids()) == [stored.artifact_id]


def test_checksum_mismatch_discards_temp(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    source = tmp_path / "in.bin"
    source.write_bytes(b"abc")
    with pytest.raises(ChecksumMismatch):
        store.put_file(source, expected_sha256="0" * 64)
    assert list(store.iter_ids()) == []
    assert not any((tmp_path / "tmp").iterdir())


def test_abort_on_exception(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    artifact_id = new_artifact_id()
    with pytest.raises(RuntimeError), store.begin(artifact_id) as pending:
        pending.write(b"x")
        raise RuntimeError("boom")
    assert not store.exists(artifact_id)


def test_invalid_id_rejected(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    with pytest.raises(ValueError):
        store.path("../etc/passwd")
