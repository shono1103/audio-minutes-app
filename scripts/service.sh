#!/usr/bin/env bash
# Compose の運用入口。volume を削除する操作は提供しない。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"

profile="macos-colima-cpu"
if [[ "${1:-}" == "--profile" ]]; then profile="${2:-}"; shift 2; fi
command_name="${1:-status}"; shift || true
validate_profile "$profile"

case "$command_name" in
  start) compose "$profile" up -d ;;
  stop) compose "$profile" stop ;;
  # .env の変更を確実に container へ反映する。単純 restart は古い環境変数を保持する。
  restart) compose "$profile" up -d --force-recreate ;;
  status) compose "$profile" ps ;;
  logs) compose "$profile" logs --tail "${1:-200}" ;;
  log-policy)
    validate_log_policy "$profile"
    echo "stdout log: Docker json-file 20m x 7 (容量保護)"
    echo "stdout archive: logs volume /stdout/<service>/YYYY-MM-DD.log"
    echo "保持期限: DB の owner 設定 $(profile_value "$profile" AM_RETENTION_LOG_DAYS) 日を minutes-api が適用"
    ;;
  log-permissions) verify_shared_log_permissions "$profile" ;;
  bootstrap) compose "$profile" exec -T minutes-api minutes-api-admin bootstrap ;;
  config)
    # `docker compose config` の通常出力は展開済み DB password / token を含む。
    # 利用者向け入口では検証だけ行い、秘密を端末やログへ出さない。
    compose "$profile" config --quiet
    echo "Compose 設定は有効です (秘密の値は表示しません)"
    ;;
  readiness) exec "$repo_root/scripts/readiness.sh" --profile "$profile" "$@" ;;
  models)
    model_command="${1:-status}"
    case "$model_command" in status|verify|smoke) ;;
      *) echo "usage: $0 [--profile PROFILE] models {status|verify|smoke}" >&2; exit 2 ;;
    esac
    exec_compose=(run --rm --no-deps -T --entrypoint python transcription-worker
      -m transcription_worker.models "$model_command")
    compose "$profile" "${exec_compose[@]}"
    ;;
  update) exec "$repo_root/scripts/update.sh" --profile "$profile" "$@" ;;
  *) echo "usage: $0 [--profile PROFILE] {start|stop|restart|status|logs|log-policy|log-permissions|bootstrap|config|readiness|models|update}" >&2; exit 2 ;;
esac
