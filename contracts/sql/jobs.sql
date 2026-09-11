-- 永続ジョブテーブル。API (am_api) が所有し、worker (am_worker) は限定操作だけを使う。
-- API の Alembic migration はこのファイルの内容を取り込む。

CREATE TABLE IF NOT EXISTS jobs (
    job_id            uuid PRIMARY KEY,
    schema_version    text NOT NULL DEFAULT 'job/1',
    kind              text NOT NULL CHECK (kind IN ('transcription', 'minutes_generation')),
    session_id        uuid NOT NULL,
    owner_id          uuid NOT NULL,
    input             jsonb NOT NULL,
    settings          jsonb NOT NULL,
    idempotency_key   text NOT NULL UNIQUE,
    status            text NOT NULL CHECK (status IN ('queued', 'leased', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt           integer NOT NULL DEFAULT 0,
    max_attempts      integer NOT NULL DEFAULT 3,
    timeout_seconds   integer NOT NULL,
    cancel_requested  boolean NOT NULL DEFAULT false,
    lease_owner       text,
    lease_expires_at  timestamptz,
    heartbeat_at      timestamptz,
    available_at      timestamptz NOT NULL DEFAULT now(),
    result            jsonb,
    failure           jsonb,
    revision          integer NOT NULL DEFAULT 0,  -- 更新ごとに +1。古い attempt の完了拒否に使う
    created_at        timestamptz NOT NULL DEFAULT now(),
    started_at        timestamptz,
    finished_at       timestamptz
);

CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs (kind, status, available_at) WHERE status IN ('queued', 'leased', 'running');
CREATE INDEX IF NOT EXISTS jobs_session_idx ON jobs (session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS job_events (
    event_id    bigserial PRIMARY KEY,
    job_id      uuid NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    attempt     integer NOT NULL,
    worker_id   text,
    event       text NOT NULL,   -- claimed | heartbeat | progress | completed | failed | cancelled | lease_lost
    detail      jsonb,           -- 本文・秘密を含めない
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS job_events_job_idx ON job_events (job_id, event_id);

-- worker 用ロール。session/minutes などの業務テーブルへは権限を与えない。
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'am_worker') THEN
        CREATE ROLE am_worker LOGIN;
    END IF;
END
$$;

GRANT SELECT, UPDATE ON jobs TO am_worker;
GRANT INSERT, SELECT ON job_events TO am_worker;
GRANT USAGE, SELECT ON SEQUENCE job_events_event_id_seq TO am_worker;

-- worker の生存・能力報告。API の /v1/capabilities と Compose healthcheck が読む。
CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id     text PRIMARY KEY,
    kind          text NOT NULL,      -- transcription | minutes
    version       text NOT NULL,
    backend       jsonb,              -- transcript.processing.backend と同じ形 (transcription のみ)
    models        jsonb,              -- 準備済みモデル一覧
    claude_state  text,               -- minutes-worker の Claude 状態 (URL や秘密は含めない)
    heartbeat_at  timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON worker_heartbeats TO am_worker;
