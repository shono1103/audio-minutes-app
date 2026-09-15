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
    -- Claude への最初の外部送信直前に worker が記録する。minutes job がこの印を
    -- 付けた後で lease を失った場合、再取得して二重送信せず outcome=unknown で止める。
    external_dispatch_started_at timestamptz,
    -- API reconciler が業務テーブルへ結果を反映し終えた時刻。複数 API 間の排他に使う。
    reconciled_at      timestamptz,
    revision          integer NOT NULL DEFAULT 0,  -- 更新ごとに +1。古い attempt の完了拒否に使う
    created_at        timestamptz NOT NULL DEFAULT now(),
    started_at        timestamptz,
    finished_at       timestamptz
);

CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs (kind, status, available_at) WHERE status IN ('queued', 'leased', 'running');
CREATE INDEX IF NOT EXISTS jobs_session_idx ON jobs (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_reconcile_idx ON jobs (finished_at, job_id) WHERE reconciled_at IS NULL;

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

-- minutes-worker が Claude へ byte を送る直前にだけ呼ぶ原子的な許可境界。
-- worker に業務テーブルの SELECT 権限を渡さず、job の fencing と現在の業務状態を
-- 同じ transaction で検査する。条件が一つでも無効なら job 自体を cancelled にして、
-- 古い worker が外部送信も結果確定もできないよう lease を失効させる。
CREATE OR REPLACE FUNCTION authorize_minutes_dispatch(
    p_job_id uuid,
    p_attempt integer,
    p_worker_id text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_allowed boolean;
    v_session_id uuid;
BEGIN
    -- API側のdelete/retry/finalize/retentionと同じくsession→jobの順でlockする。
    -- 先にjobをlockすると、sessionを保持してjob取消を待つdeleteとdeadlockし得る。
    SELECT session_id INTO v_session_id FROM public.jobs WHERE job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    PERFORM 1 FROM public.sessions WHERE id = v_session_id FOR UPDATE;

    SELECT (
        j.cancel_requested = false
        AND j.lease_expires_at > now()
        AND s.id IS NOT NULL
        AND s.deleted_at IS NULL
        AND s.minutes_job_id = j.job_id
        AND s.owner_id = j.owner_id
        AND s.allow_external_send = true
        AND u.id IS NOT NULL
        AND u.disabled_at IS NULL
        AND c.state = 'logged_in'
        AND c.connected_owner_id = s.owner_id
        AND j.settings ->> 'allow_external_send' = 'true'
        AND j.settings ->> 'connected_owner_id' = s.owner_id::text
    )
      INTO v_allowed
      FROM public.jobs AS j
      LEFT JOIN public.sessions AS s ON s.id = j.session_id
      LEFT JOIN public.users AS u ON u.id = s.owner_id
      LEFT JOIN public.claude_connection AS c ON c.id = 1
     WHERE j.job_id = p_job_id
       AND j.kind = 'minutes_generation'
       AND j.attempt = p_attempt
       AND j.lease_owner = p_worker_id
       AND j.status IN ('leased', 'running')
     FOR UPDATE OF j;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF NOT COALESCE(v_allowed, false) THEN
        UPDATE public.jobs
           SET status = 'cancelled', cancel_requested = true, finished_at = now(),
               lease_owner = NULL, lease_expires_at = NULL, revision = revision + 1
         WHERE job_id = p_job_id
           AND attempt = p_attempt
           AND lease_owner = p_worker_id
           AND status IN ('leased', 'running');
        RETURN false;
    END IF;

    UPDATE public.jobs
       SET external_dispatch_started_at = COALESCE(external_dispatch_started_at, now()),
           heartbeat_at = now(), revision = revision + 1
     WHERE job_id = p_job_id
       AND attempt = p_attempt
       AND lease_owner = p_worker_id
       AND status IN ('leased', 'running');
    RETURN FOUND;
END;
$$;

REVOKE ALL ON FUNCTION authorize_minutes_dispatch(uuid, integer, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION authorize_minutes_dispatch(uuid, integer, text) TO am_worker;

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
