#!/usr/bin/env bash
# Swift CLI/.app/NativeBridge と Chrome 開発版拡張を build・ユーザー領域へ配置する。
set -euo pipefail
umask 077
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"

profile="macos-colima-cpu"
skip_build="${AM_SKIP_CLIENT_BUILD:-0}"
if [[ "${1:-}" == "--profile" ]]; then profile="${2:-}"; shift 2; fi
[[ $# -eq 0 ]] || { echo "usage: $0 [--profile PROFILE]" >&2; exit 2; }
validate_profile "$profile"
[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS client の配置は Darwin でだけ実行します"; exit 0; }

support_dir="${AM_CLIENT_ROOT:-${HOME}/Library/Application Support/AudioMinutes}"
applications_dir="${AM_USER_APPLICATIONS_DIR:-${HOME}/Applications}"
user_bin_dir="${AM_USER_BIN_DIR:-${HOME}/.local/bin}"
native_host_dir="${AM_CHROME_NATIVE_HOST_DIR:-${HOME}/Library/Application Support/Google/Chrome/NativeMessagingHosts}"
app_dir="$applications_dir/AudioMinutes.app"
managed_bin_dir="$support_dir/bin"
extension_id="cglcpocpendfgbhepidgbpilokapdlnm"

if [[ "$skip_build" != "1" ]]; then
  command -v swift >/dev/null 2>&1 || { echo "Swift 5.10 以降が必要です" >&2; exit 2; }
  command -v npm >/dev/null 2>&1 || { echo "Node.js/npm が必要です" >&2; exit 2; }
  swift build --package-path "$repo_root/apps/cli" -c release
  swift build --package-path "$repo_root/apps/macos" -c release
  npm --prefix "$repo_root/extensions/chrome" ci
  npm --prefix "$repo_root/extensions/chrome" test
  npm --prefix "$repo_root/extensions/chrome" run build
  cli_bin="$(swift build --package-path "$repo_root/apps/cli" -c release --show-bin-path)/audio-minutes"
  mac_bin_dir="$(swift build --package-path "$repo_root/apps/macos" -c release --show-bin-path)"
else
  # shell fixture 試験専用。配布物の上書き境界だけを検証する。
  cli_bin="${AM_FIXTURE_CLI_BIN:?AM_FIXTURE_CLI_BIN が必要です}"
  mac_bin_dir="${AM_FIXTURE_MAC_BIN_DIR:?AM_FIXTURE_MAC_BIN_DIR が必要です}"
fi

for executable in "$cli_bin" "$mac_bin_dir/AudioMinutesApp" "$mac_bin_dir/NativeBridge"; do
  [[ -x "$executable" ]] || { echo "build 成果物がありません: $executable" >&2; exit 1; }
done

mkdir -p "$support_dir" "$managed_bin_dir" "$applications_dir" "$user_bin_dir" \
  "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources" "$native_host_dir"
chmod 700 "$support_dir" "$managed_bin_dir"
install -m 755 "$cli_bin" "$managed_bin_dir/audio-minutes"
install -m 755 "$mac_bin_dir/AudioMinutesApp" "$app_dir/Contents/MacOS/AudioMinutesApp"
install -m 755 "$mac_bin_dir/NativeBridge" "$app_dir/Contents/MacOS/NativeBridge"
install -m 644 "$repo_root/deploy/macos/Info.plist" "$app_dir/Contents/Info.plist"
ln -sfn "$managed_bin_dir/audio-minutes" "$user_bin_dir/audio-minutes"

settings="$support_dir/settings.json"
if [[ ! -e "$settings" ]]; then
  api_base_url="$(profile_value "$profile" AM_PUBLIC_BASE_URL)"
  context="$(docker_context "$profile")"
  colima="$(colima_profile "$profile")"
  jq -n \
    --arg api "${api_base_url:-http://localhost:8787}" \
    --arg deploy "$repo_root/deploy" \
    --arg profile "$profile" \
    --arg context "$context" \
    --arg colima "$colima" \
    '{schema_version:"client-settings/1",api_base_url:$api,deploy_dir:$deploy,profile:$profile,
      docker_context:(if $context=="" then null else $context end),
      colima_profile:(if $colima=="" then null else $colima end),default_language_mode:"auto",
      default_format_profile_id:null,last_microphone_uid:null,last_target:null,
      notifications:{minutes_completed:true,processing_failed:true},claude_send_default:true}' >"$settings.tmp"
  chmod 600 "$settings.tmp"
  mv "$settings.tmp" "$settings"
  echo "ClientSettings を作成しました: $settings"
else
  echo "既存 ClientSettings を保持します: $settings"
fi

manifest="$native_host_dir/dev.audio_minutes.bridge.json"
if [[ ! -e "$manifest" ]]; then
  jq -n --arg path "$app_dir/Contents/MacOS/NativeBridge" --arg origin "chrome-extension://${extension_id}/" \
    '{name:"dev.audio_minutes.bridge",description:"audio-minutes Chrome NativeBridge",path:$path,type:"stdio",allowed_origins:[$origin]}' \
    >"$manifest.tmp"
  chmod 600 "$manifest.tmp"
  mv "$manifest.tmp" "$manifest"
  echo "Native Messaging manifest を作成しました: $manifest"
else
  echo "既存 Native Messaging manifest を保持します: $manifest"
  if ! jq -e --arg origin "chrome-extension://${extension_id}/" \
    '.name=="dev.audio_minutes.bridge" and .type=="stdio" and (.allowed_origins|index($origin)!=null) and (.path|type=="string")' \
    "$manifest" >/dev/null; then
    echo "PENDING 既存 Native Messaging manifest の互換性を確認してください: $manifest" >&2
  fi
fi

echo "client: $app_dir"
echo "cli: $user_bin_dir/audio-minutes"
echo "extension: $repo_root/extensions/chrome/dist (Chrome で手動読込が必要)"
