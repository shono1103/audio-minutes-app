"""API lifespan から動かす、短いDB transaction単位の定期処理。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from minutes_api import reconciler, retention
from minutes_api.config import Settings, get_settings
from minutes_api.db import session_scope

logger = logging.getLogger(__name__)


def run_reconcile_cycle() -> reconciler.ReconcileStats:
    with session_scope() as db:
        return reconciler.reconcile_once(db)


def run_sweep_cycle(settings: Settings | None = None) -> dict[str, int]:
    settings = settings or get_settings()
    # DB状態を確定してから実体を消す。削除失敗時は次回 plan に再登場する。
    with session_scope() as db:
        policy = retention.current(db, settings)
        plan = retention.plan_sweep(db)
    retention.delete_planned_files(plan, settings)
    with session_scope() as db:
        orphaned = retention.sweep_orphan_artifacts(db, settings=settings)
        temp = retention.sweep_temp_files(db, settings, older_than_seconds=policy.upload_hours * 3600)
    logs = retention.sweep_log_files(settings, policy)
    return {
        "expired_uploads": plan.expired_uploads,
        "expired_audio_sessions": plan.expired_audio_sessions,
        "audit_logs": plan.audit_logs,
        "orphan_artifacts": orphaned,
        "temporary_artifacts": temp,
        "log_files": logs,
    }


async def periodic(name: str, interval_seconds: float, operation: Callable[[], object]) -> None:
    """失敗を次周期へ隔離する。cancel時だけ即座に終了する。"""
    while True:
        try:
            await asyncio.to_thread(operation)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - background task を一時的なDB/FS障害で終了させない
            logger.exception("background task に失敗しました task=%s", name)
        await asyncio.sleep(max(0.1, interval_seconds))


def create_tasks(settings: Settings | None = None) -> list[asyncio.Task[None]]:
    settings = settings or get_settings()

    def unless_updating(operation: Callable[[], object]) -> object | None:
        if settings.update_maintenance_file.exists():
            return None
        return operation()

    return [
        asyncio.create_task(
            periodic("reconciler", settings.reconcile_interval_seconds, lambda: unless_updating(run_reconcile_cycle))
        ),
        asyncio.create_task(
            periodic("retention", settings.sweep_interval_seconds, lambda: unless_updating(lambda: run_sweep_cycle(settings)))
        ),
    ]


__all__ = ["create_tasks", "periodic", "run_reconcile_cycle", "run_sweep_cycle"]
