#!/usr/bin/env bash
# PostgreSQL 初回初期化時に worker 用ロールを作る (docs/integration-contract.md「DB ロール」)。
# 業務テーブルへの GRANT は API の migration (contracts/sql/jobs.sql) が行う。
set -euo pipefail

if [[ -z "${AM_WORKER_DB_PASSWORD:-}" ]]; then
  echo "AM_WORKER_DB_PASSWORD が未設定のため am_worker ロールを作成できません" >&2
  exit 1
fi

psql -v ON_ERROR_STOP=1 -v worker_password="${AM_WORKER_DB_PASSWORD}" \
  --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'am_worker') THEN
    CREATE ROLE am_worker LOGIN;
  END IF;
END
$$;
ALTER ROLE am_worker WITH PASSWORD :'worker_password';
GRANT CONNECT ON DATABASE audio_minutes TO am_worker;
GRANT USAGE ON SCHEMA public TO am_worker;
SQL
