"""録音中の先行文字起こしchunkを追加する。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006_live_transcription_chunks"
down_revision = "0005_passkey_registration_flows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001がBase.metadata.create_all()を使うため、fresh installでは現行ORMの
    # tableが先に作られる。既存DBだけここで追加して両経路を収束させる。
    if "live_audio_chunks" in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "live_audio_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("track_id", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("start_offset_ms", sa.BigInteger(), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("audio_artifact_id", sa.String(length=30), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("transcript_json_artifact_id", sa.String(length=30), nullable=True),
        sa.Column("transcript_md_artifact_id", sa.String(length=30), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="uploaded", nullable=False),
        sa.Column("failure", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("session_id", "track_id", "sequence"),
    )
    op.create_index("ix_live_audio_chunks_session_id", "live_audio_chunks", ["session_id"])
    op.create_index("ix_live_audio_chunks_job_id", "live_audio_chunks", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_live_audio_chunks_job_id", table_name="live_audio_chunks")
    op.drop_index("ix_live_audio_chunks_session_id", table_name="live_audio_chunks")
    op.drop_table("live_audio_chunks")
