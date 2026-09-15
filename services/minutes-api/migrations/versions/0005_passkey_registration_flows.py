"""パスキー後日追加用のbrowser request状態を追加する。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0005_passkey_registration_flows"
down_revision = "0004_browser_reauth_and_dispatch_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("browser_reauth_requests")}
    if "purpose" not in columns:
        op.add_column(
            "browser_reauth_requests",
            sa.Column("purpose", sa.String(length=32), server_default="reauth", nullable=False),
        )
    if "action_completed_at" not in columns:
        op.add_column(
            "browser_reauth_requests",
            sa.Column("action_completed_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("browser_reauth_requests", "action_completed_at")
    op.drop_column("browser_reauth_requests", "purpose")
