#!/usr/bin/env bash
# cold clone から CPU MVP を構築する再実行可能な installer。
# 既存 .env、ClientSettings、録音、成果物、DB、model、Claude 資格情報は削除・上書きしない。
set -euo pipefail
umask 077
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"

profile="macos-colima-cpu"
prepare_only=0
assume_yes=0
skip_client=0
configure_tailscale=0
readiness_timeout="${AM_READINESS_TIMEOUT:-300}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) profile="${2:-}"; shift 2 ;;
    --prepare-only) prepare_only=1; shift ;;
    --yes) assume_yes=1; shift ;;
    --skip-client) skip_client=1; shift ;;
    --tailscale) configure_tailscale=1; shift ;;
    --readiness-timeout) readiness_timeout="${2:-}"; shift 2 ;;
    *) echo "usage: $0 [--profile PROFILE] [--prepare-only] [--yes] [--skip-client] [--tailscale] [--readiness-timeout SECONDS]" >&2; exit 2 ;;
  esac
done
validate_profile "$profile"
[[ "$readiness_timeout" =~ ^[0-9]+$ ]] || { echo "readiness timeout は秒数で指定してください" >&2; exit 2; }

stage="preflight"
on_error() {
  local code=$?
  local tailscale_arg=''
  (( configure_tailscale )) && tailscale_arg=' --tailscale'
  echo "INSTALL_FAILED stage=$stage exit=$code" >&2
  echo "復旧: $repo_root/scripts/install.sh --profile $profile${tailscale_arg}" >&2
  echo "診断: $repo_root/scripts/doctor.sh --profile $profile" >&2
  echo "状態: $repo_root/scripts/service.sh --profile $profile status" >&2
  exit "$code"
}
trap on_error ERR

example="$repo_root/deploy/profiles/$profile/.env.example"
target="$(profile_env "$profile")"
[[ -f "$example" ]] || { echo "設定雛形がありません: $example" >&2; exit 2; }

required_files=(
  "$repo_root/deploy/compose.yml"
  "$repo_root/deploy/settings.schema.json"
  "$repo_root/services/minutes-api/uv.lock"
  "$repo_root/services/transcription-worker/uv.lock"
  "$repo_root/services/minutes-worker/uv.lock"
  "$repo_root/scripts/doctor.sh"
  "$repo_root/scripts/service.sh"
  "$repo_root/scripts/readiness.sh"
  "$repo_root/scripts/configure-tailscale.sh"
)
for required_file in "${required_files[@]}"; do
  [[ -f "$required_file" ]] || { echo "導入に必要なファイルがありません: ${required_file#"$repo_root/"}" >&2; exit 2; }
done
ensure_install_dependencies "$profile" "$assume_yes"

stage="settings"
if [[ ! -f "$target" ]]; then
  api_password="$(openssl rand -hex 24)"
  worker_password="$(openssl rand -hex 24)"
  secret_key="$(openssl rand -hex 32)"
  internal_token="$(openssl rand -hex 32)"
  awk -v api="$api_password" -v worker="$worker_password" -v secret="$secret_key" -v internal="$internal_token" '
    /^AM_API_DB_PASSWORD=$/ { print "AM_API_DB_PASSWORD=" api; next }
    /^AM_WORKER_DB_PASSWORD=$/ { print "AM_WORKER_DB_PASSWORD=" worker; next }
    /^AM_SECRET_KEY=$/ { print "AM_SECRET_KEY=" secret; next }
    /^AM_INTERNAL_TOKEN=$/ { print "AM_INTERNAL_TOKEN=" internal; next }
    { print }
  ' "$example" >"$target.tmp"
  chmod 600 "$target.tmp"
  mv "$target.tmp" "$target"
  echo "設定を作成しました: $target (秘密の値は表示しません)"
else
  echo "既存設定を保持します: $target"
fi

if (( prepare_only )); then
  echo "PREPARED Compose・モデル取得・client build は実行していません"
  exit 0
fi

if (( ! assume_yes )); then
  echo "profile=$profile"
  echo "実行内容: 専用 Colima の安全な起動、3 image build、固定 revision モデル取得 (最大数 GiB)、offline smoke、永続 volume/DB migration、client build・ユーザー領域配置"
  echo "保持対象: 既存 .env、ClientSettings、録音、成果物、DB、モデル cache、Claude 資格情報"
  read -r -p "上記を実行しますか？ [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "中止しました"; exit 1; }
fi

stage="colima"
ensure_colima_running "$profile"

stage="doctor"
"$repo_root/scripts/doctor.sh" --profile "$profile"
compose "$profile" config --quiet

stage="images"
echo "3つのサーバー component image を build します"
compose "$profile" build minutes-api transcription-worker minutes-worker
migrate_shared_volume_permissions "$profile"
verify_shared_log_permissions "$profile"

stage="models"
prepare_models "$profile"

stage="client"
install_client_for_profile "$profile" "$skip_client"

stage="services"
compose "$profile" up -d --no-build

stage="readiness"
"$repo_root/scripts/readiness.sh" --profile "$profile" --timeout "$readiness_timeout"

if (( configure_tailscale )); then
  stage="tailscale"
  "$repo_root/scripts/configure-tailscale.sh" --profile "$profile"
fi

trap - ERR
echo "INSTALL_READY profile=$profile"
echo "PENDING 初回 owner 登録: $repo_root/scripts/service.sh --profile $profile bootstrap"
echo "PENDING Claude subscription: owner 登録後、GUI の owner 設定から本人がログインしてください"
if [[ "$profile" == macos-* ]]; then
  echo "PENDING 録音権限: AudioMinutes.app の初回録音時にマイクとシステム音声を本人が許可してください"
  echo "PENDING Chrome: chrome://extensions で $repo_root/extensions/chrome/dist を読み込み、固定 ID と接続状態を確認してください"
fi
