"""業務テーブルと永続ジョブ契約を作成する。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from alembic import op
from minutes_api import models  # noqa: F401
from minutes_api.db import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    Base.metadata.create_all(connection)
    jobs_sql = Path(__file__).resolve().parents[4] / "contracts" / "sql" / "jobs.sql"
    connection.execute(text(jobs_sql.read_text(encoding="utf-8")))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("DROP TABLE IF EXISTS worker_heartbeats, job_events, jobs CASCADE"))
    Base.metadata.drop_all(connection)
