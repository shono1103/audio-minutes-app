"""成果物ストア。artifact ID で参照し、クライアントのパス・URL を worker のアクセス先にしない。

LocalArtifactStore は同一ホストの共有 volume 用。tmp へ書き、sha256/サイズを確定してから
不変 ID のパスへ rename する。将来のオブジェクトストアは同じ Protocol で差し替える。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Protocol

from audio_minutes_contracts.ids import is_artifact_id, new_artifact_id

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class StoredArtifact:
    artifact_id: str
    byte_size: int
    sha256: str


class ArtifactStore(Protocol):
    def begin(self) -> "PendingArtifact": ...

    def open(self, artifact_id: str) -> BinaryIO: ...

    def path(self, artifact_id: str) -> Path: ...

    def exists(self, artifact_id: str) -> bool: ...

    def delete(self, artifact_id: str) -> None: ...

    def stat(self, artifact_id: str) -> StoredArtifact: ...


class PendingArtifact:
    """書き込み中の一時 artifact。commit で不変 ID として公開、abort で破棄。"""

    def __init__(self, store: "LocalArtifactStore", artifact_id: str) -> None:
        self._store = store
        self.artifact_id = artifact_id
        self.temp_path = store.root / "tmp" / f"{artifact_id}.part"
        self.temp_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: BinaryIO | None = self.temp_path.open("wb")
        self._hasher = hashlib.sha256()
        self._size = 0

    def write(self, data: bytes) -> None:
        if self._handle is None:
            raise RuntimeError("closed")
        self._handle.write(data)
        self._hasher.update(data)
        self._size += len(data)

    def write_from(self, source: BinaryIO) -> None:
        while True:
            chunk = source.read(_CHUNK)
            if not chunk:
                break
            self.write(chunk)

    def commit(self, expected_sha256: str | None = None) -> StoredArtifact:
        if self._handle is None:
            raise RuntimeError("closed")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        self._handle = None
        digest = self._hasher.hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            self.temp_path.unlink(missing_ok=True)
            raise ChecksumMismatch(self.artifact_id, expected_sha256, digest)
        final_path = self._store.path(self.artifact_id)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.temp_path, final_path)
        return StoredArtifact(self.artifact_id, self._size, digest)

    def abort(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        self.temp_path.unlink(missing_ok=True)

    def __enter__(self) -> "PendingArtifact":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.abort()


class ChecksumMismatch(ValueError):
    def __init__(self, artifact_id: str, expected: str, actual: str) -> None:
        super().__init__(f"checksum mismatch for {artifact_id}")
        self.artifact_id = artifact_id
        self.expected = expected
        self.actual = actual


class LocalArtifactStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def begin(self, artifact_id: str | None = None) -> PendingArtifact:
        return PendingArtifact(self, artifact_id or new_artifact_id())

    def path(self, artifact_id: str) -> Path:
        if not is_artifact_id(artifact_id):
            raise ValueError(f"invalid artifact id: {artifact_id!r}")
        return self.root / artifact_id[4:6] / artifact_id

    def exists(self, artifact_id: str) -> bool:
        return self.path(artifact_id).is_file()

    def open(self, artifact_id: str) -> BinaryIO:
        return self.path(artifact_id).open("rb")

    def delete(self, artifact_id: str) -> None:
        self.path(artifact_id).unlink(missing_ok=True)

    def stat(self, artifact_id: str) -> StoredArtifact:
        path = self.path(artifact_id)
        hasher = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                hasher.update(chunk)
                size += len(chunk)
        return StoredArtifact(artifact_id, size, hasher.hexdigest())

    def put_bytes(self, data: bytes, artifact_id: str | None = None) -> StoredArtifact:
        pending = self.begin(artifact_id)
        pending.write(data)
        return pending.commit()

    def put_file(self, source: Path, artifact_id: str | None = None, expected_sha256: str | None = None) -> StoredArtifact:
        pending = self.begin(artifact_id)
        with source.open("rb") as handle:
            pending.write_from(handle)
        return pending.commit(expected_sha256)

    def iter_ids(self) -> Iterator[str]:
        for shard in sorted(self.root.iterdir()):
            if shard.name == "tmp" or not shard.is_dir():
                continue
            for item in sorted(shard.iterdir()):
                if is_artifact_id(item.name):
                    yield item.name

    def sweep_temp(self, older_than_seconds: int) -> int:
        """途中終了で残った一時ファイルを回収する。"""
        import time

        removed = 0
        temp_dir = self.root / "tmp"
        if not temp_dir.is_dir():
            return 0
        cutoff = time.time() - older_than_seconds
        for item in temp_dir.iterdir():
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
                removed += 1
        return removed


def copy_into_store(store: LocalArtifactStore, source: Path, expected_sha256: str | None = None) -> StoredArtifact:
    return store.put_file(source, expected_sha256=expected_sha256)


__all__ = [
    "ArtifactStore",
    "ChecksumMismatch",
    "LocalArtifactStore",
    "PendingArtifact",
    "StoredArtifact",
    "copy_into_store",
]
