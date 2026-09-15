"""tus 追送の書き込み権と、再処理要求の世代を追加する。

* uploads.write_claim_id / write_claim_expires_at (R04):
  PATCH は短い transaction で claim を取り、ストリーム受信中は行ロックを保持しない。
* sessions.transcription_generation / minutes_generation (R07):
  成功済み revision ではなく「利用者が要求した世代」を queue 投入の冪等キーにする。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0002_upload_claim_and_job_generation"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic既定のversion_numはVARCHAR(32)で、このrevision IDは収まらない。
    # head更新がmigration本体の後に行われるため、先に列幅を拡張する。
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=32),
        type_=sa.String(length=128),
        existing_nullable=False,
    )

    # 0001 は初期骨格で Base.metadata.create_all() を使っていたため、fresh installでは
    # 新しいORM列が先に作られる。一方、既に0001を適用済みのDBには列がない。
    # 両方を0002へ収束させ、既存DBのupgrade経路を保持する。
    inspector = inspect(op.get_bind())
    upload_columns = {column["name"] for column in inspector.get_columns("uploads")}
    session_columns = {column["name"] for column in inspector.get_columns("sessions")}

    if "write_claim_id" not in upload_columns:
        op.add_column("uploads", sa.Column("write_claim_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True))
    if "write_claim_expires_at" not in upload_columns:
        op.add_column("uploads", sa.Column("write_claim_expires_at", sa.DateTime(timezone=True), nullable=True))
    if "transcription_generation" not in session_columns:
        op.add_column(
            "sessions",
            sa.Column("transcription_generation", sa.Integer(), nullable=False, server_default="0"),
        )
    if "minutes_generation" not in session_columns:
        op.add_column(
            "sessions",
            sa.Column("minutes_generation", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    op.drop_column("sessions", "minutes_generation")
    op.drop_column("sessions", "transcription_generation")
    op.drop_column("uploads", "write_claim_expires_at")
    op.drop_column("uploads", "write_claim_id")
