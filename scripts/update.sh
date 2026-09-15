#!/usr/bin/env bash
# backup 後に image/model/client/readiness を一体で更新し、失敗時は旧 image と backup へ戻す。
set -euo pipefail
umask 077
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"

profile="macos-colima-cpu"
backup_dir=""
assume_yes=0
skip_client=0
readiness_timeout="${AM_READINESS_TIMEOUT:-300}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) profile="${2:-}"; shift 2 ;;
    --backup-dir) backup_dir="${2:-}"; shift 2 ;;
    --yes) assume_yes=1; shift ;;
    --skip-client) skip_client=1; shift ;;
    --readiness-timeout) readiness_timeout="${2:-}"; shift 2 ;;
    *) echo "usage: $0 [--profile PROFILE] --backup-dir DIR [--yes] [--skip-client] [--readiness-timeout SECONDS]" >&2; exit 2 ;;
  esac
done

validate_profile "$profile"
[[ "$readiness_timeout" =~ ^[0-9]+$ ]] || { echo "readiness timeout は秒数で指定してください" >&2; exit 2; }
[[ -n "$backup_dir" ]] || { echo "更新前 backup の保存先を --backup-dir で指定してください" >&2; exit 2; }
[[ ! -e "$backup_dir" ]] || { echo "既存の保存先は上書きしません: $backup_dir" >&2; exit 1; }

required_files=(
  "$repo_root/deploy/compose.yml"
  "$repo_root/deploy/settings.schema.json"
  "$repo_root/services/minutes-api/uv.lock"
  "$repo_root/services/transcription-worker/uv.lock"
  "$repo_root/services/minutes-worker/uv.lock"
  "$repo_root/scripts/readiness.sh"
  "$repo_root/scripts/install-client.sh"
)
for required_file in "${required_files[@]}"; do
  [[ -f "$required_file" ]] || { echo "更新に必要なファイルがありません: $required_file" >&2; exit 2; }
done

ensure_install_dependencies "$profile" "$assume_yes"
ensure_colima_running "$profile"
compose "$profile" config --quiet
"$repo_root/scripts/doctor.sh" --profile "$profile"
"$repo_root/scripts/backup.sh" --profile "$profile" --leave-stopped "$backup_dir"

# backup の整合点から成功または rollback 完了まで、通常の write services を再開しない。
# backup 自体の失敗時は backup.sh が旧サービスを再開する。
rollback_refs=()
maintenance_enabled=0
rollback_update() {
  local code=$? pair original rollback_ref
  trap - ERR
  echo "UPDATE_FAILED: write services を停止したまま旧 image と backup へ rollback します" >&2
  compose "$profile" stop minutes-api transcription-worker transcription-worker-vulkan minutes-worker >/dev/null 2>&1 || true
  if (( maintenance_enabled )); then
    set_update_maintenance "$profile" disable || true
    maintenance_enabled=0
  fi
  for pair in "${rollback_refs[@]}"; do
    original="${pair%%|*}"; rollback_ref="${pair#*|}"
    docker_for_profile "$profile" image tag "$rollback_ref" "$original" || true
  done
  if ! "$repo_root/scripts/restore.sh" --profile "$profile" "$backup_dir" --confirm-overwrite; then
    echo "ROLLBACK_FAILED backup=${backup_dir}。write services は停止中です。手動確認してください" >&2
    exit "$code"
  fi
  if ! compose "$profile" up -d --no-build --force-recreate >/dev/null; then
    echo "ROLLBACK_FAILED backup=${backup_dir}。旧 image でサービスを再作成できませんでした" >&2
    exit "$code"
  fi
  echo "ROLLBACK_READY backup=${backup_dir}。rollback 完了後に旧サービスを再開しました" >&2
  exit "$code"
}
trap rollback_update ERR

if (( ! assume_yes )); then
  echo "backup を作成しました: $backup_dir"
  echo "image、固定モデル、client を更新し、業務 readiness が失敗した場合はこの backup と旧 image へ戻します。"
  read -r -p "profile=$profile を更新しますか？ [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || {
    trap - ERR
    compose "$profile" up -d --no-build >/dev/null
    echo "更新を中止し、旧サービスを再開しました。backup は保持しています: $backup_dir"
    exit 1
  }
fi

tag="$(profile_value "$profile" AM_IMAGE_TAG)"
tag="${tag:-0.1.0}"
rollback_suffix="am-update-rollback-$$"
image_refs=(
  "audio-minutes/minutes-api:$tag"
  "audio-minutes/transcription-worker:$tag"
  "audio-minutes/minutes-worker:$tag"
)
[[ "$(compose_profile "$profile")" == "gpu" ]] && image_refs+=("audio-minutes/transcription-worker-vulkan:$tag")
for image_ref in "${image_refs[@]}"; do
  rollback_ref="${image_ref}-${rollback_suffix}"
  if docker_for_profile "$profile" image inspect "$image_ref" >/dev/null 2>&1; then
    docker_for_profile "$profile" image tag "$image_ref" "$rollback_ref"
    rollback_refs+=("$image_ref|$rollback_ref")
  fi
done

echo "3つのサーバー component image を build します"
compose "$profile" build minutes-api transcription-worker minutes-worker
migrate_shared_volume_permissions "$profile"
verify_shared_log_permissions "$profile"
prepare_models "$profile"
install_client_for_profile "$profile" "$skip_client"
# readiness 中に通常 API request・background task・worker claim を通さない。marker の
# 削除だけが更新の commit point で、それまでは失敗しても backup 後の新規データはない。
set_update_maintenance "$profile" enable
maintenance_enabled=1
compose "$profile" up -d --no-build --remove-orphans
"$repo_root/scripts/readiness.sh" --profile "$profile" --timeout "$readiness_timeout"
set_update_maintenance "$profile" disable
maintenance_enabled=0

trap - ERR
for pair in "${rollback_refs[@]}"; do
  rollback_ref="${pair#*|}"
  docker_for_profile "$profile" image rm "$rollback_ref" >/dev/null 2>&1 || true
done
echo "UPDATE_READY profile=$profile backup=$backup_dir"
