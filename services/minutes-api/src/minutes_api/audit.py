"""監査ログ。会議内容・秘密・URL を含めず、操作の種別と対象 ID だけを記録する。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from minutes_api.models import AuditLog


def record(
    db: Session,
    action: str,
    *,
    actor_id: uuid.UUID | None,
    target_type: str | None = None,
    target_id: str | uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            metadata_=metadata,
        )
    )
