#!/usr/bin/env bash
# 読み取り専用の導入前検査。秘密の値は出力しない。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"

profile="macos-colima-cpu"
if [[ "${1:-}" == "--profile" ]]; then profile="${2:-}"; shift 2; fi
[[ $# -eq 0 ]] || { echo "usage: $0 [--profile PROFILE]" >&2; exit 2; }
validate_profile "$profile"

host_json="$($repo_root/scripts/inspect-host.sh --json)"
errors=0
check_file() { [[ -f "$1" ]] || { echo "NG  必須ファイルなし: ${1#"$repo_root"/}"; errors=$((errors + 1)); }; }
check_tool() { command -v "$1" >/dev/null 2>&1 || { echo "NG  必須ツールなし: $1"; errors=$((errors + 1)); }; }

echo "profile=$profile"
for path in \
  "$repo_root/deploy/compose.yml" \
  "$repo_root/deploy/settings.schema.json" \
  "$repo_root/services/minutes-api/uv.lock" \
  "$repo_root/services/transcription-worker/uv.lock" \
  "$repo_root/services/minutes-worker/uv.lock"; do
  check_file "$path"
done
check_file "$repo_root/scripts/configure-tailscale.sh"
for tool in docker jq openssl git curl; do check_tool "$tool"; done

os_name="$(jq -r '.["os.name"] // "unknown"' <<<"$host_json")"
if [[ "$profile" == macos-* && "$os_name" != "macOS" ]]; then
  echo "NG  macOS profile を $os_name では使用できません"; errors=$((errors + 1))
elif [[ "$profile" == "linux-cpu" && "$os_name" != "Linux" ]]; then
  echo "NG  linux-cpu は Linux への明示配置専用です"; errors=$((errors + 1))
fi

if [[ "$profile" == macos-* ]]; then
  for tool in colima swift node npm; do check_tool "$tool"; done
fi

context="$(docker_context "$profile")"
if ! docker_for_profile "$profile" info >/dev/null 2>&1; then
  echo "NG  Docker daemon に接続できません (context=${context:-current})"
  errors=$((errors + 1))
elif ! docker_for_profile "$profile" compose version >/dev/null 2>&1; then
  echo "NG  Docker Compose plugin を利用できません (context=${context:-current})"
  errors=$((errors + 1))
else
  server_arch="$(docker_for_profile "$profile" info --format '{{.Architecture}}')"
  server_cpus="$(docker_for_profile "$profile" info --format '{{.NCPU}}')"
  server_memory="$(docker_for_profile "$profile" info --format '{{.MemTotal}}')"
  case "$server_arch" in arm64|aarch64|amd64|x86_64) ;; *)
    echo "NG  Docker daemon の CPU architecture が未対応です: $server_arch"; errors=$((errors + 1)) ;;
  esac
  if (( server_cpus < 2 )); then
    echo "NG  文字起こしには Docker VM へ 2 CPU 以上が必要です: $server_cpus"; errors=$((errors + 1))
  fi
  if (( server_memory < 6442450944 )); then
    echo "NG  文字起こしには Docker VM へ 6 GiB 以上が必要です: $server_memory bytes"; errors=$((errors + 1))
  fi
  if ! validate_log_policy "$profile"; then
    echo "NG  stdout log の容量 rotation・共有 GID・API 保持期限設定が一致しません"
    errors=$((errors + 1))
  else
    echo "OK  stdout log: Docker 20m x 7、DB owner 設定を正本に API が日次 archive を削除"
  fi
fi
if [[ "$profile" == "macos-colima-krunkit-vulkan" ]]; then
  echo "注意: GPU profile は既存 VM と別の Colima profile を利用し、明示許可なしに作成・変更しません"
fi

# ブラウザー認証の origin 整合 (R11)。RP ID が canonical origin の host と違うと
# パスキーの登録・認証がブラウザー側で拒否される。IP は RP ID として使えない。
env_file="$(profile_env "$profile")"
if [[ -f "$env_file" ]]; then
  base_url="$(grep -E '^AM_PUBLIC_BASE_URL=' "$env_file" | tail -1 | cut -d= -f2- | tr -d '\r')"
  rp_id="$(grep -E '^AM_RP_ID=' "$env_file" | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' | tr -d '\r')"
  origins="$(grep -E '^AM_ALLOWED_ORIGINS=' "$env_file" | tail -1 | cut -d= -f2- | tr -d '\r')"
  base_host="${base_url#*://}"; base_host="${base_host%%/*}"; base_host="${base_host%%:*}"
  base_origin="${base_url%/}"
  if [[ -n "$base_host" && -n "$rp_id" && "$base_host" != "$rp_id" ]]; then
    echo "NG  AM_RP_ID ($rp_id) が AM_PUBLIC_BASE_URL の host ($base_host) と一致しません"
    errors=$((errors + 1))
  fi
  if [[ "$rp_id" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "NG  AM_RP_ID に IP アドレスは使えません (WebAuthn の RP ID はドメイン文字列)"
    errors=$((errors + 1))
  fi
  if [[ -n "$origins" && ",$origins," != *",$base_origin,"* ]]; then
    echo "NG  AM_ALLOWED_ORIGINS に canonical origin ($base_origin) が含まれていません"
    errors=$((errors + 1))
  fi
fi

if (( errors > 0 )); then
  echo "doctor: $errors 件の問題があります" >&2
  exit 1
fi
echo "doctor: OK"
