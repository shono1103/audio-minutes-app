"""contracts/schemas/*.v1.schema.json の pydantic 表現。

JSON Schema が正本であり、ここは実装言語向けの写し。差異は tests/contract で検出する。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class InputKind(StrEnum):
    RECORDED_DUAL_TRACK = "recorded_dual_track"
    IMPORTED_MIXED = "imported_mixed"


class LanguageMode(StrEnum):
    AUTO = "auto"
    JA = "ja"
    EN = "en"
    MIXED = "mixed"


class TrackRole(StrEnum):
    APP = "app"
    MICROPHONE = "microphone"
    MIXED = "mixed"


class Strategy(StrEnum):
    VAD_TURBO = "vad_turbo"
    ROUTED = "routed"
    WHOLE_RETRY = "whole_retry"
    FIXED_JA = "fixed_ja"
    FIXED_EN = "fixed_en"
    MIXED = "mixed"


class JobKind(StrEnum):
    TRANSCRIPTION = "transcription"
    MINUTES_GENERATION = "minutes_generation"


class JobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionStatus(StrEnum):
    UPLOADING = "uploading"
    VALIDATING = "validating"
    QUEUED = "queued"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    QUEUED_MINUTES = "queued_minutes"
    GENERATING_MINUTES = "generating_minutes"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETING = "deleting"


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_FORMAT = "unsupported_format"
    LIMIT_EXCEEDED = "limit_exceeded"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    UPLOAD_INCOMPLETE = "upload_incomplete"
    UPLOAD_EXPIRED = "upload_expired"
    DECODE_FAILED = "decode_failed"
    ENGINE_CRASHED = "engine_crashed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    GPU_UNAVAILABLE = "gpu_unavailable"
    UNSUPPORTED_MODEL = "unsupported_model"
    BACKEND_ERROR = "backend_error"
    CLAUDE_NOT_AUTHENTICATED = "claude_not_authenticated"
    CLAUDE_CREDENTIAL_CONFLICT = "claude_credential_conflict"
    CLAUDE_RATE_LIMITED = "claude_rate_limited"
    CLAUDE_CLI_INCOMPATIBLE = "claude_cli_incompatible"
    CLAUDE_OUTPUT_INVALID = "claude_output_invalid"
    CLAUDE_UNKNOWN_OUTCOME = "claude_unknown_outcome"
    EXTERNAL_SEND_FORBIDDEN = "external_send_forbidden"
    NOT_CONNECTED_OWNER = "not_connected_owner"
    AUDIO_DELETED = "audio_deleted"
    INTERNAL = "internal"


# --- recording package -------------------------------------------------------


class TimeBase(_Strict):
    unit: Literal["ms"] = "ms"
    origin: Literal["recording_start"] = "recording_start"


class RecordingTrack(_Strict):
    track_id: Literal["app-audio", "microphone", "imported-audio"]
    role: TrackRole
    start_offset_ms: int = Field(ge=0)
    container: Literal["wav", "m4a", "mp3", "flac"]
    codec: Literal["pcm_s16le", "pcm_s24le", "pcm_f32le", "aac", "mp3", "flac"]
    sample_rate: int = Field(ge=8000, le=192000)
    channels: int = Field(ge=1, le=2)
    duration_ms: int = Field(ge=0, le=14_400_000)
    byte_size: int = Field(ge=1, le=2_147_483_648)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_container: str | None = None


class RecordingSource(_Strict):
    kind: Literal["app", "chrome_tab", "file"]
    app_name: str | None = None
    bundle_id: str | None = None
    tab_host: str | None = None
    client: str | None = None


class RecordingPackage(_Strict):
    schema_version: Literal["recording-package/2"] = "recording-package/2"
    session_id: UUID
    input_kind: InputKind
    time_base: TimeBase = Field(default_factory=TimeBase)
    started_at: datetime
    # 表示用タイトル。未入力ならクライアントが録音元・ファイル名と日時から仮生成する
    title: str | None = Field(default=None, max_length=200)
    # 仮タイトルと利用者入力の区別 (FR-107 / FR-141)。false のときだけ Claude の提案を採用してよい
    title_edited_by_user: bool = False
    language_mode: LanguageMode = LanguageMode.AUTO
    allow_external_send: bool = True
    format_profile_id: UUID | None = None
    tracks: list[RecordingTrack] = Field(min_length=1, max_length=2)
    source: RecordingSource

    def required_track_ids(self) -> list[str]:
        if self.input_kind is InputKind.RECORDED_DUAL_TRACK:
            return ["app-audio", "microphone"]
        return ["imported-audio"]


# --- error -------------------------------------------------------------------


class ApiErrorBody(_Strict):
    code: ErrorCode
    message: str
    stage: Literal["auth", "upload", "validation", "transcription", "minutes", "storage", "admin"] | None = None
    retryable: bool | None = None
    request_id: str
    retained_artifacts: list[str] = Field(default_factory=list)
    details: dict[str, Any] | None = None


class ApiError(_Strict):
    error: ApiErrorBody


class JobFailure(_Strict):
    code: ErrorCode
    stage: Literal["transcription", "minutes", "storage"]
    retryable: bool
    message: str
    exit_code: int | None = None
    retained_artifacts: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] | None = None


# --- transcript --------------------------------------------------------------


class Word(_Strict):
    start_ms: int
    end_ms: int
    word: str
    probability: float | None = None


class Segment(_Strict):
    id: str
    track_id: str
    source: TrackRole
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str
    language: Literal["ja", "en", "und"]
    language_probability: float | None = Field(default=None, ge=0, le=1)
    model_id: str
    model_revision: str
    engine: Literal["faster-whisper", "whisper.cpp"]
    engine_version: str
    strategy: Strategy
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    words: list[Word] | None = None
    replaced_by_review: str | None = None


class ReviewAttempt(_Strict):
    attempt: int = Field(ge=1)
    model_id: str
    model_revision: str
    language: Literal["ja", "en", "und"] | None
    language_probability: float | None
    candidate_text: str | None = None
    accepted: bool
    reason: str


class ReviewItem(_Strict):
    id: str
    track_id: str
    start_ms: int
    end_ms: int
    reason: Literal["speech_without_text", "sparse_text", "low_language_confidence", "language_boundary"]
    attempts: list[ReviewAttempt] = Field(default_factory=list)
    resolved: bool


class Normalization(_Strict):
    original_container: str | None = None
    original_codec: str | None = None
    original_sample_rate: int | None = None
    original_channels: int | None = None
    target_sample_rate: int = 16000
    target_channels: int = 1


class TranscriptTrack(_Strict):
    track_id: str
    role: TrackRole
    source_artifact_id: str
    start_offset_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    normalization: Normalization | None = None


class ProcessingModel(_Strict):
    profile: Literal["ja", "en", "multilingual"]
    model_id: str
    model_revision: str
    compute_type: str | None = None
    quantization: str | None = None
    file_sha256: str | None = None


class Backend(_Strict):
    requested_backend: Literal["cpu", "vulkan"]
    effective_backend: Literal["cpu", "vulkan", "unknown"]
    gpu_verified: bool
    gpu_name: str | None = None
    driver: str | None = None
    vm: str | None = None
    fallback_reason: str | None = None
    software_renderer_detected: bool | None = None


class Resources(_Strict):
    cpu_arch: str
    cpu_count: int
    memory_limit_bytes: int | None = None
    peak_memory_bytes: int | None = None
    threads: int | None = None


class Timings(_Strict):
    total_ms: int = Field(ge=0)
    decode_audio_ms: int | None = None
    vad_ms: int | None = None
    language_detection_ms: int | None = None
    model_load_ms: int | None = None
    transcribe_ms: int | None = None
    review_retry_ms: int | None = None
    merge_ms: int | None = None
    audio_duration_ms: int | None = None
    rtf: float | None = None


class Processing(_Strict):
    engine: Literal["faster-whisper", "whisper.cpp"]
    engine_version: str
    strategy: Strategy
    models: list[ProcessingModel]
    decode: dict[str, Any] | None = None
    backend: Backend
    resources: Resources
    timings: Timings


class Transcript(_Strict):
    schema_version: Literal["transcript/1"] = "transcript/1"
    session_id: UUID
    revision: int = Field(ge=1)
    input_kind: InputKind
    language_mode: LanguageMode
    tracks: list[TranscriptTrack] = Field(min_length=1)
    segments: list[Segment]
    review: list[ReviewItem] = Field(default_factory=list)
    processing: Processing

    def to_markdown(self) -> str:
        """人が確認しやすい transcript.md。時刻順、音源ラベル付き。"""
        lines = [f"# 文字起こし (revision {self.revision})", ""]
        for segment in sorted(self.segments, key=lambda item: (item.start_ms, item.track_id)):
            start = _format_ms(segment.start_ms)
            end = _format_ms(segment.end_ms)
            flag = " ⚠" if segment.replaced_by_review else ""
            lines.append(f"- [{start}–{end}] ({segment.source}/{segment.language}){flag} {segment.text}")
        unresolved = [item for item in self.review if not item.resolved]
        if unresolved:
            lines += ["", "## 要確認区間 (未解決)", ""]
            for item in unresolved:
                lines.append(
                    f"- [{_format_ms(item.start_ms)}–{_format_ms(item.end_ms)}] {item.track_id}: {item.reason}"
                )
        return "\n".join(lines) + "\n"


def _format_ms(value: int) -> str:
    seconds, milliseconds = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


# --- job / worker result -----------------------------------------------------


class JobInputArtifact(_Strict):
    artifact_id: str
    kind: Literal["audio_track", "transcript_json", "minutes_md", "format_snapshot"]
    track_id: str | None = None
    role: TrackRole | None = None
    start_offset_ms: int | None = None
    revision: int | None = None


class JobInput(_Strict):
    artifacts: list[JobInputArtifact]
    transcript_revision: int | None = None
    parent_minutes_version_id: str | None = None


class WorkerResultArtifact(_Strict):
    artifact_id: str
    kind: Literal["transcript_json", "transcript_md", "minutes_md"]
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str | None = None


class ClaudeUsage(_Strict):
    cli_version: str
    model: str | None = None
    chunks: int = Field(default=1, ge=1)
    input_tokens_estimate: int | None = None
    duration_ms: int = Field(default=0, ge=0)


class WorkerResult(_Strict):
    schema_version: Literal["worker-result/1"] = "worker-result/1"
    kind: JobKind
    outcome: Literal["succeeded", "unknown"] = "succeeded"
    artifacts: list[WorkerResultArtifact]
    processing: Processing | None = None
    title_proposal: str | None = Field(default=None, max_length=200)
    insufficient_information: list[str] = Field(default_factory=list)
    claude: ClaudeUsage | None = None


class TranscriptionJobSettings(_Strict):
    """transcription ジョブの設定スナップショット。API が producer、transcription-worker が consumer。

    `transcript_revision` は「この実行が作ろうとしている transcript の版」で、投入の冪等キー
    (要求世代) とは別物。冪等キーは jobs.idempotency_key が持つ。
    """

    input_kind: InputKind
    language_mode: LanguageMode
    transcript_revision: int = Field(ge=1)
    max_audio_ms: int = Field(ge=1)
    requested_backend: Literal["cpu", "vulkan"] | None = None
    strategy: Strategy | None = None


class MinutesJobSettings(_Strict):
    """minutes_generation ジョブの設定スナップショット。API が producer、minutes-worker が consumer。

    外部送信の可否 (`allow_external_send`) と接続 owner (`connected_owner_id`) は API が
    投入時に確定させ、worker は送信直前にこの 2 つと実際のログイン状態を再検査する。
    どちらかが欠けている payload は worker が拒否する (FR-129 / M5 安全条件)。
    """

    kind: Literal["claude_generated", "claude_regenerated"]
    allow_external_send: bool
    connected_owner_id: UUID | None
    # 利用者が編集したタイトル。未編集のときだけ Claude の提案タイトルを採用してよい
    title: str = Field(max_length=200)
    title_edited_by_user: bool
    input_kind: InputKind
    format_snapshot: FormatProfile | None = None
    instructions: str | None = Field(default=None, max_length=8000)
    started_at: datetime | None = None
    duration_ms: int | None = None
    transcript_revision: int | None = None
    expected_current_version_id: UUID | None = None
    chunk_chars: int | None = None


class Job(_Strict):
    schema_version: Literal["job/1"] = "job/1"
    job_id: UUID
    kind: JobKind
    session_id: UUID
    owner_id: UUID
    input: JobInput
    settings: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=200)
    status: JobStatus
    attempt: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1)
    cancel_requested: bool
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    available_at: datetime | None = None
    external_dispatch_started_at: datetime | None = None
    result: WorkerResult | None = None
    failure: JobFailure | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


# --- format / minutes / session ---------------------------------------------


class FormatSection(_Strict):
    key: Literal[
        "summary", "decisions", "action_items", "open_questions", "topics", "transcript_references", "custom"
    ]
    title: str = Field(min_length=1, max_length=100)
    enabled: bool
    instructions: str | None = Field(default=None, max_length=2000)


class FormatProfile(_Strict):
    schema_version: Literal["format-profile/1"] = "format-profile/1"
    profile_id: UUID
    owner_id: UUID | None = None
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=100)
    output_language: Literal["ja", "en"]
    builtin: bool = False
    is_default: bool = False
    sections: list[FormatSection] = Field(min_length=1)
    additional_instructions: str = Field(default="", max_length=4000)
    template_markdown: str = Field(max_length=20000)
    updated_at: datetime | None = None


class MinutesVersion(_Strict):
    schema_version: Literal["minutes-version/1"] = "minutes-version/1"
    version_id: UUID
    session_id: UUID
    version_number: int = Field(ge=1)
    kind: Literal["claude_generated", "manual_edit", "claude_regenerated", "restored"]
    created_by: str
    created_at: datetime
    parent_version_id: UUID | None
    restored_from_version_id: UUID | None = None
    transcript_revision: int | None
    format_snapshot: FormatProfile | None
    instructions: str | None = None
    insufficient_information: list[str] = Field(default_factory=list)
    title_proposal: str | None = None
    artifact_id: str
    is_candidate: bool = False


class SessionTrack(_Strict):
    track_id: str
    role: TrackRole
    upload_state: Literal["pending", "uploading", "completed", "expired", "cancelled"]
    upload_id: str | None = None
    upload_url: str | None = None
    upload_expires_at: datetime | None = None
    artifact_id: str | None = None


class Session(_Strict):
    schema_version: Literal["session/1"] = "session/1"
    session_id: UUID
    owner_id: UUID
    title: str
    title_edited_by_user: bool
    title_revision: int = Field(default=0, ge=0)
    input_kind: InputKind
    language_mode: LanguageMode
    allow_external_send: bool
    format_snapshot: FormatProfile | None = None
    status: SessionStatus
    failure: JobFailure | None = None
    started_at: datetime | None = None
    duration_ms: int | None = None
    created_at: datetime
    updated_at: datetime
    tracks: list[SessionTrack]
    current_minutes_version_id: UUID | None
    transcript_revision: int | None
    review_count: int = Field(default=0, ge=0)
    audio_retained: bool
    audio_expires_at: datetime | None = None
    shared_with: list[UUID]
    is_shared_view: bool = False


# MinutesJobSettings は後方で定義される FormatProfile を参照するため、最後に解決する。
MinutesJobSettings.model_rebuild()
