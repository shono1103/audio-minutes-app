"""browser-bound再認証とminutes外部送信の原子的guardを追加する。"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision = "0004_browser_reauth_and_dispatch_guard"
down_revision = "0003_reconciler_and_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    browser_columns = {column["name"] for column in inspector.get_columns("browser_sessions")}
    if "webauthn_context" not in browser_columns:
        op.add_column("browser_sessions", sa.Column("webauthn_context", sa.dialects.postgresql.JSONB(), nullable=True))

    if "browser_reauth_requests" not in inspector.get_table_names():
        op.create_table(
            "browser_reauth_requests",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
            sa.Column(
                "user_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "access_token_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("access_tokens.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "grant_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("reauth_grants.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    # jobs.sql は fresh install と upgrade の双方で同じ関数定義・権限へ収束させる正本。
    jobs_sql = Path(__file__).resolve().parents[4] / "contracts" / "sql" / "jobs.sql"
    op.get_bind().execute(text(jobs_sql.read_text(encoding="utf-8")))


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS authorize_minutes_dispatch(uuid, integer, text)")
    op.drop_table("browser_reauth_requests")
    op.drop_column("browser_sessions", "webauthn_context")
