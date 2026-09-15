"""API が所有する業務テーブル。jobs / job_events / worker_heartbeats は contracts/sql/jobs.sql が正で、
migration から DDL を取り込む (ORM では参照しない)。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from minutes_api.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # owner | member
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    default_format_profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @property
    def disabled(self) -> bool:
        return self.disabled_at is not None


class PasswordCredential(Base):
    __tablename__ = "credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WebAuthnCredential(Base):
    __tablename__ = "webauthn_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    credential_id: Mapped[bytes] = mapped_column(Text, unique=True, nullable=False)  # base64url
    public_key: Mapped[str] = mapped_column(Text, nullable=False)  # base64url
    sign_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    transports: Mapped[list[str] | None] = mapped_column(JSONB)
    label: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BootstrapToken(Base):
    __tablename__ = "bootstrap_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RecoveryCode(Base):
    __tablename__ = "recovery_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BrowserSession(Base):
    """認証画面用 cookie セッション。cookie にはランダムトークンだけを置き、DB で照合する。"""

    __tablename__ = "browser_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    webauthn_challenge: Mapped[str | None] = mapped_column(Text)
    webauthn_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pending_registration: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # bootstrap/invite の登録途中状態
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuthorizationCode(Base):
    __tablename__ = "authorization_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AccessToken(Base):
    __tablename__ = "access_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    family_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    scope: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ReauthGrant(Base):
    __tablename__ = "reauth_grants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    access_token_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BrowserReauthRequest(Base):
    """native が開始し、server browser で本人確認する一回限りの再認証要求。"""

    __tablename__ = "browser_reauth_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    access_token_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("access_tokens.id", ondelete="CASCADE"), nullable=False
    )
    grant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reauth_grants.id", ondelete="SET NULL")
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="reauth", server_default="reauth")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    action_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FormatProfile(Base):
    __tablename__ = "format_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    output_language: Mapped[str] = mapped_column(String(2), nullable=False)
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sections: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    additional_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    template_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MeetingSession(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    title_edited_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    title_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    language_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="auto")
    allow_external_send: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    format_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    package: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # recording-package の写し (検証済み)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="uploading")
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    transcript_revision: Mapped[int | None] = mapped_column(Integer)
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_minutes_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    transcription_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    minutes_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # 要求世代。利用者が再処理を要求するたびに進める。成功済み revision (transcript_revision) や
    # job の attempt とは役割が別で、queue 投入の冪等キーにだけ使う。
    transcription_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    minutes_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    audio_retained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    audio_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    uploads: Mapped[list[Upload]] = relationship(back_populates="session", cascade="all, delete-orphan")
    shares: Mapped[list[SessionShare]] = relationship(back_populates="session", cascade="all, delete-orphan")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="session", cascade="all, delete-orphan")


class SessionShare(Base):
    __tablename__ = "session_shares"
    __table_args__ = (UniqueConstraint("session_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    session: Mapped[MeetingSession] = relationship(back_populates="shares")


class Upload(Base):
    """tus upload。offset はこの行が正。"""

    __tablename__ = "uploads"
    __table_args__ = (UniqueConstraint("session_id", "track_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    track_id: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_length: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    offset: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending|uploading|completed|expired|cancelled
    # 追送中の排他。PATCH は短い transaction で claim を取り、ストリーム受信中は
    # 行ロックも transaction も保持しない。異常終了した claim は期限で引き継ぐ。
    write_claim_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    write_claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    session: Mapped[MeetingSession] = relationship(back_populates="uploads")


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # audio_track | transcript_json | transcript_md | minutes_md
    track_id: Mapped[str | None] = mapped_column(String(32))
    role: Mapped[str | None] = mapped_column(String(16))
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    session: Mapped[MeetingSession] = relationship(back_populates="artifacts")


class Transcript(Base):
    __tablename__ = "transcripts"
    __table_args__ = (UniqueConstraint("session_id", "revision"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    json_artifact_id: Mapped[str] = mapped_column(String(30), nullable=False)
    md_artifact_id: Mapped[str | None] = mapped_column(String(30))
    processing: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MinutesVersion(Base):
    __tablename__ = "minutes_versions"
    __table_args__ = (UniqueConstraint("session_id", "version_number"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    restored_from_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    transcript_revision: Mapped[int | None] = mapped_column(Integer)
    format_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    instructions: Mapped[str | None] = mapped_column(Text)
    insufficient_information: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    title_proposal: Mapped[str | None] = mapped_column(String(200))
    artifact_id: Mapped[str] = mapped_column(String(30), nullable=False)
    is_candidate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ClaudeConnection(Base):
    """配置単位の Claude 接続状態。URL・資格情報は持たない。"""

    __tablename__ = "claude_connection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    connected_owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="checking")
    cli_version: Mapped[str | None] = mapped_column(String(32))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClaudeAuthSession(Base):
    """ログイン処理の状態 ID。URL は保存しない。"""

    __tablename__ = "claude_auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# 参照専用: jobs テーブル (DDL は contracts/sql/jobs.sql)
JOBS_ACTIVE_STATUSES = ("queued", "leased", "running")
