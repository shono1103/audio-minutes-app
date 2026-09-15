"""SQLAlchemy エンジンとセッション。同期 + psycopg3。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from minutes_api.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().sqlalchemy_url, pool_pre_ping=True, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


def reset_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def db_dependency() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def commit_rejection(db: Session) -> None:
    """拒否応答を返す直前に、確定させたい副作用だけを commit する。

    `db_dependency` は例外時に rollback するため、トークン系列の失効や認可コードの
    消費、その監査記録を拒否と一緒に捨ててしまう。呼び出し側は「この時点までの変更は
    すべて確定してよい」ことを確認したうえで、raise の直前にだけ使う。
    すべてのエラー経路で commit してはいけない。
    """
    db.commit()
