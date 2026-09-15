"""SQLite (ファイル) で業務テーブルだけを立てる HTTP 試験の土台。

jobs テーブルは contracts/sql/jobs.sql (PostgreSQL 専用: jsonb / xmax / now()) なので、
ここでは jobs.enqueue などを差し替えて API 側のロジックだけを検証する。
lock 待ちを伴う本当の並行性 (SELECT FOR UPDATE) は PostgreSQL 付きの試験でだけ確認できる。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, DateTime, TypeDecorator, create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from minutes_api import accounts, models
from minutes_api.app import create_app
from minutes_api.config import Settings, get_settings, reset_settings
from minutes_api.db import Base, db_dependency
from minutes_api.security import after, token_hash


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw) -> str:  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_on_sqlite(type_, compiler, **kw) -> str:  # noqa: ANN001
    # SQLite の autoincrement は INTEGER PRIMARY KEY だけに効く (audit_log.id)
    return "INTEGER"


@compiles(UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw) -> str:  # noqa: ANN001
    # UUID という型名は SQLite で NUMERIC affinity になり、全零に近い固定 ID を
    # 浮動小数へ変換して壊す。PostgreSQL UUID の文字列表現を保持する。
    return "CHAR(36)"


class _UtcDateTime(TypeDecorator):
    """SQLite でも PostgreSQL と同じく tz 付きで出し入れする (試験用)。

    SQLite には timestamptz が無く naive な値が返るため、実装側の tz 付き比較と
    食い違う。保存時に UTC へ寄せ、読み出しで UTC を付け直して差を埋める。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is not None and value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


def _use_utc_datetimes() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, DateTime) and column.type.timezone:
                column.type = _UtcDateTime()


_use_utc_datetimes()


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    value = reset_settings(
        Settings(
            artifacts_dir=tmp_path / "artifacts",
            uploads_dir=tmp_path / "uploads",
            log_dir=tmp_path / "logs",
            public_base_url="http://localhost:8787",
            background_tasks=False,
        )
    )
    yield value
    reset_settings()


@pytest.fixture
def session_factory(tmp_path: Path, settings: Settings) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture
def db(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    app = create_app()

    def override() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[db_dependency] = override
    with TestClient(app) as test_client:
        yield test_client


def make_user(db: Session, email: str = "owner@example.test", role: str = "owner") -> models.User:
    user = models.User(email=email, role=role)
    db.add(user)
    db.flush()
    return user


def issue_access_token(db: Session, user: models.User) -> str:
    issued = accounts.issue_tokens(db, user)
    db.commit()
    return str(issued["access_token"])


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_refresh_pair(db: Session, user: models.User) -> tuple[str, uuid.UUID]:
    issued = accounts.issue_tokens(db, user)
    db.commit()
    return str(issued["refresh_token"]), issued["_family_id"]


def make_upload(db: Session, owner: models.User, *, length: int) -> tuple[models.MeetingSession, models.Upload]:
    import datetime

    session = models.MeetingSession(
        id=uuid.uuid4(),
        owner_id=owner.id,
        title="試験セッション",
        input_kind="imported_mixed",
        language_mode="auto",
        package={"tracks": [{"track_id": "imported-audio", "start_offset_ms": 0}]},
        status="uploading",
        started_at=datetime.datetime.now(datetime.UTC),
    )
    db.add(session)
    db.flush()
    upload = models.Upload(
        session_id=session.id,
        track_id="imported-audio",
        role="mixed",
        expected_length=length,
        expected_sha256="0" * 64,
        state="pending",
        expires_at=after(3600),
    )
    db.add(upload)
    db.commit()
    return session, upload


__all__ = [
    "auth_headers",
    "get_settings",
    "issue_access_token",
    "make_refresh_pair",
    "make_upload",
    "make_user",
    "token_hash",
]
