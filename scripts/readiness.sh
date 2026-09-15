#!/usr/bin/env bash
# Compose の生存ではなく API・DB/queue・artifact・worker・固定モデルの業務準備を待つ。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"

profile="macos-colima-cpu"
timeout_seconds="${AM_READINESS_TIMEOUT:-300}"
if [[ "${1:-}" == "--profile" ]]; then profile="${2:-}"; shift 2; fi
if [[ "${1:-}" == "--timeout" ]]; then timeout_seconds="${2:-}"; shift 2; fi
[[ $# -eq 0 && "$timeout_seconds" =~ ^[0-9]+$ ]] || {
  echo "usage: $0 [--profile PROFILE] [--timeout SECONDS]" >&2; exit 2;
}
validate_profile "$profile"

api_port="$(profile_value "$profile" AM_API_PORT)"
api_url="http://localhost:${api_port:-8787}/v1/health"
deadline=$((SECONDS + timeout_seconds))
last="開始待ち"

while (( SECONDS <= deadline )); do
  api_ready=0 db_ready=0 artifact_ready=0 workers_ready=0 models_ready=0
  if curl --fail --silent --max-time 3 "$api_url" | jq -e '.status=="ok"' >/dev/null 2>&1; then api_ready=1; fi
  if compose "$profile" exec -T db pg_isready -U am_api -d audio_minutes >/dev/null 2>&1; then db_ready=1; fi
  if compose "$profile" run --rm --no-deps -T --entrypoint sh minutes-api -c \
      'test -r /var/lib/audio-minutes/artifacts && test -w /var/lib/audio-minutes/artifacts' >/dev/null 2>&1; then
    artifact_ready=1
  fi
  if compose "$profile" run --rm --no-deps -T --entrypoint python transcription-worker \
      -m transcription_worker.models status --quiet >/dev/null 2>&1; then
    models_ready=1
  fi
  worker_state="$(compose "$profile" exec -T db psql -U am_api -d audio_minutes --no-align --tuples-only --quiet -c "
    SELECT
      EXISTS (
        SELECT 1 FROM worker_heartbeats wh
        WHERE kind='transcription' AND heartbeat_at > now() - interval '180 seconds'
          AND backend->>'ready'='true' AND jsonb_array_length(models) >= 2
          AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(models) model WHERE model->>'ready' IS DISTINCT FROM 'true')
      )::int || '|' ||
      EXISTS (
        SELECT 1 FROM worker_heartbeats
        WHERE kind='minutes' AND heartbeat_at > now() - interval '180 seconds'
      )::int
  " 2>/dev/null | tr -d '[:space:]' || true)"
  [[ "$worker_state" == "1|1" ]] && workers_ready=1

  last="api=$api_ready db_queue=$db_ready artifacts=$artifact_ready models=$models_ready workers=$workers_ready"
  if (( api_ready && db_ready && artifact_ready && models_ready && workers_ready )); then
    echo "READY $last"
    exit 0
  fi
  sleep 5
done

echo "NOT_READY timeout=${timeout_seconds}s $last" >&2
echo "復旧: $repo_root/scripts/service.sh --profile $profile status" >&2
echo "ログ: $repo_root/scripts/service.sh --profile $profile logs 200" >&2
echo "モデル: $repo_root/scripts/service.sh --profile $profile models status" >&2
exit 1
