"""job 結果調停と Claude 外部送信開始の永続状態を追加する。

* jobs.reconciled_at: API が worker result を業務テーブルへ一度だけ反映するための印。
* jobs.external_dispatch_started_at: 外部送信開始後の lease 切れで二重送信を防ぐ印。
* transcript / minutes の job_id unique index: API 再起動・複数 API でも同じ結果を重複確定しない。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0003_reconciler_and_dispatch"
down_revision = "0002_upload_claim_and_job_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    job_columns = {column["name"] for column in inspector.get_columns("jobs")}
    if "external_dispatch_started_at" not in job_columns:
        op.add_column("jobs", sa.Column("external_dispatch_started_at", sa.DateTime(timezone=True), nullable=True))
    if "reconciled_at" not in job_columns:
        op.add_column("jobs", sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS jobs_reconcile_idx ON jobs (finished_at, job_id) WHERE reconciled_at IS NULL")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS transcripts_job_id_uq ON transcripts (job_id) WHERE job_id IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS minutes_versions_job_id_uq ON minutes_versions (job_id) WHERE job_id IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS minutes_versions_job_id_uq")
    op.execute("DROP INDEX IF EXISTS transcripts_job_id_uq")
    op.execute("DROP INDEX IF EXISTS jobs_reconcile_idx")
    op.drop_column("jobs", "reconciled_at")
    op.drop_column("jobs", "external_dispatch_started_at")
