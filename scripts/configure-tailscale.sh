#!/usr/bin/env bash
# 既存の Tailscale Serve / WebAuthn 資格情報を壊さず、tailnet 内 HTTPS 入口を設定する。
set -euo pipefail
umask 077
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"

profile="macos-colima-cpu"
if [[ "${1:-}" == "--profile" ]]; then profile="${2:-}"; shift 2; fi
[[ $# -eq 0 ]] || { echo "usage: $0 [--profile PROFILE]" >&2; exit 2; }
validate_profile "$profile"

for tool in tailscale jq curl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Tailscale HTTPS 設定に必要なコマンドがありません: $tool" >&2; exit 2; }
done

env_file="$(profile_env "$profile")"
[[ -f "$env_file" && ! -L "$env_file" ]] || { echo "通常ファイルの profile 設定がありません: $env_file" >&2; exit 2; }

status_json="$(tailscale status --json)" || { echo "Tailscale の状態を取得できません" >&2; exit 3; }
backend_state="$(jq -er '.BackendState | select(type=="string")' <<<"$status_json")" || {
  echo "Tailscale の BackendState を解釈できません" >&2; exit 3;
}
[[ "$backend_state" == Running ]] || { echo "Tailscale が Running ではありません: $backend_state" >&2; exit 3; }
dns_name="$(jq -er '.Self.DNSName | select(type=="string" and length>0)' <<<"$status_json")" || {
  echo "Tailscale Self.DNSName を取得できません" >&2; exit 3;
}
dns_name="${dns_name%.}"
[[ "$dns_name" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && "$dns_name" == *.* ]] || {
  echo "Tailscale DNSName が不正です" >&2; exit 3;
}
canonical_origin="https://$dns_name"

read_unique_env() {
  local key="$1" count value
  count="$(grep -c "^${key}=" "$env_file" || true)"
  [[ "$count" == 1 ]] || { echo "$key は .env に正確に1件必要です（現在 ${count}件）" >&2; return 4; }
  value="$(grep "^${key}=" "$env_file" | cut -d= -f2-)"
  [[ -n "$value" && "$value" != *$'\r'* && "$value" != *$'\n'* ]] || {
    echo "$key の値が不正です" >&2; return 4;
  }
  printf '%s' "$value"
}

api_port="$(read_unique_env AM_API_PORT)"
read_unique_env AM_PUBLIC_BASE_URL >/dev/null
old_rp="$(read_unique_env AM_RP_ID)"
read_unique_env AM_ALLOWED_ORIGINS >/dev/null
[[ "$api_port" =~ ^[0-9]+$ && "$api_port" -ge 1 && "$api_port" -le 65535 ]] || {
  echo "AM_API_PORT が不正です" >&2; exit 4;
}

expected_proxy="http://127.0.0.1:$api_port"
expected_hostport="$dns_name:443"
serve_json="$(tailscale serve status --json)" || { echo "Tailscale Serve の状態を取得できません" >&2; exit 5; }
jq -e 'type=="object"' <<<"$serve_json" >/dev/null || { echo "Tailscale Serve JSON が不正です" >&2; exit 5; }

serve_is_empty=0
if jq -e 'length==0' <<<"$serve_json" >/dev/null; then
  serve_is_empty=1
elif jq -e --arg hp "$expected_hostport" --arg proxy "$expected_proxy" '
  .TCP == {"443":{"HTTPS":true}} and
  .Web == {($hp):{"Handlers":{"/":{"Proxy":$proxy}}}} and
  ((.AllowFunnel // {}) == {}) and ((.Services // {}) == {}) and ((.Foreground // {}) == {}) and
  ((keys - ["AllowFunnel","Foreground","Services","TCP","Web"]) | length == 0)
' <<<"$serve_json" >/dev/null; then
  : # 同じ単独 Serve は冪等再実行として保持する。
else
  echo "既存の Tailscale Serve/Funnel 設定が audio-minutes 専用構成と一致しないため変更しません" >&2
  exit 5
fi

# RP ID を変更すると登録済み passkey は利用不能になるため、DB を確認して拒否する。
if [[ "$old_rp" != "$dns_name" ]]; then
  table_exists="$(compose "$profile" exec -T db psql -U am_api -d audio_minutes --no-align --tuples-only --quiet \
    -c "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname='public' AND tablename='webauthn_credentials')::int" 2>/dev/null | tr -d '[:space:]')" || {
      echo "WebAuthn 資格情報の有無を安全に確認できないため RP ID を変更しません" >&2; exit 6;
    }
  if [[ "$table_exists" == 1 ]]; then
    credential_count="$(compose "$profile" exec -T db psql -U am_api -d audio_minutes --no-align --tuples-only --quiet \
      -c 'SELECT count(*) FROM webauthn_credentials' 2>/dev/null | tr -d '[:space:]')" || {
        echo "WebAuthn 資格情報を安全に確認できないため RP ID を変更しません" >&2; exit 6;
      }
    [[ "$credential_count" =~ ^[0-9]+$ ]] || { echo "WebAuthn 資格情報数を解釈できません" >&2; exit 6; }
    (( credential_count == 0 )) || {
      echo "登録済み WebAuthn 資格情報があるため AM_RP_ID の自動変更を拒否します" >&2; exit 6;
    }
  elif [[ "$table_exists" != 0 ]]; then
    echo "WebAuthn table 検査結果を解釈できません" >&2; exit 6
  fi
fi

settings="${AM_CLIENT_ROOT:-${HOME}/Library/Application Support/AudioMinutes}/settings.json"
settings_present=0
if [[ -e "$settings" || -L "$settings" ]]; then
  [[ -f "$settings" && ! -L "$settings" ]] || { echo "ClientSettings が通常ファイルではありません" >&2; exit 7; }
  jq -e --arg deploy "$repo_root/deploy" --arg profile "$profile" '
    type=="object" and .schema_version=="client-settings/1" and .profile==$profile and
    (.deploy_dir|type)=="string" and .deploy_dir==$deploy and
    (.api_base_url|type)=="string"
  ' "$settings" >/dev/null || {
    echo "ClientSettings の deploy_dir または api_base_url がこの配置と一致しません" >&2; exit 7;
  }
  settings_present=1
fi

health_timeout="${AM_TAILSCALE_HEALTH_TIMEOUT:-90}"
[[ "$health_timeout" =~ ^[0-9]+$ ]] || { echo "AM_TAILSCALE_HEALTH_TIMEOUT が不正です" >&2; exit 2; }

env_backup="$(mktemp "${TMPDIR:-/tmp}/audio-minutes-env.XXXXXX")"
settings_backup=''
env_tmp=''
settings_tmp=''
cleanup_backups() {
  rm -f -- "$env_backup" || true
  [[ -z "$settings_backup" ]] || rm -f -- "$settings_backup" || true
  [[ -z "$env_tmp" ]] || rm -f -- "$env_tmp" || true
  [[ -z "$settings_tmp" ]] || rm -f -- "$settings_tmp" || true
}
trap cleanup_backups EXIT
cp -p "$env_file" "$env_backup"
chmod 600 "$env_backup"
if (( settings_present )); then
  settings_backup="$(mktemp "${TMPDIR:-/tmp}/audio-minutes-settings.XXXXXX")"
  cp -p "$settings" "$settings_backup"
  chmod 600 "$settings_backup"
fi
env_changed=0
settings_changed=0
serve_created=0
rollback_running=0

rollback() {
  local original_code="$1" rollback_failed=0 restore_tmp=''
  (( rollback_running == 0 )) || exit "$original_code"
  rollback_running=1
  echo "Tailscale HTTPS 設定に失敗したため元の設定へ戻します" >&2
  if (( serve_created )); then
    tailscale serve --yes --https=443 off >/dev/null 2>&1 || { echo "ROLLBACK_FAILED Tailscale Serve を解除できません" >&2; rollback_failed=1; }
  fi
  if (( env_changed )); then
    restore_tmp="$(mktemp "${env_file}.rollback.XXXXXX")" \
      && cp "$env_backup" "$restore_tmp" && chmod 600 "$restore_tmp" && mv "$restore_tmp" "$env_file" \
      || { echo "ROLLBACK_FAILED .env を復元できません" >&2; rollback_failed=1; }
  fi
  if (( settings_changed )); then
    restore_tmp="$(mktemp "${settings}.rollback.XXXXXX")" \
      && cp "$settings_backup" "$restore_tmp" && chmod 600 "$restore_tmp" && mv "$restore_tmp" "$settings" \
      || { echo "ROLLBACK_FAILED ClientSettings を復元できません" >&2; rollback_failed=1; }
  fi
  if (( env_changed )); then
    compose "$profile" up -d --no-deps --force-recreate minutes-api >/dev/null 2>&1 \
      || { echo "ROLLBACK_FAILED minutes-api を元設定で再生成できません" >&2; rollback_failed=1; }
  fi
  (( rollback_failed == 0 )) || echo "ROLLBACK_INCOMPLETE 手動復旧が必要です" >&2
  exit "$original_code"
}
on_error() { local code=$?; rollback "$code"; }
on_interrupt() { rollback 130; }
on_terminate() { rollback 143; }
on_hangup() { rollback 129; }
trap on_error ERR
trap on_interrupt INT
trap on_terminate TERM
trap on_hangup HUP

if env_tmp="$(mktemp "${env_file}.tailscale.XXXXXX")"; then :; else code=$?; rollback "$code"; fi
if awk -v public="$canonical_origin" -v rp="$dns_name" -v origins="$canonical_origin" '
  /^AM_PUBLIC_BASE_URL=/ { print "AM_PUBLIC_BASE_URL=" public; next }
  /^AM_RP_ID=/ { print "AM_RP_ID=" rp; next }
  /^AM_ALLOWED_ORIGINS=/ { print "AM_ALLOWED_ORIGINS=" origins; next }
  { print }
' "$env_file" >"$env_tmp"; then :; else code=$?; rollback "$code"; fi
if chmod 600 "$env_tmp"; then :; else code=$?; rollback "$code"; fi
# mv の成功直後に signal が届いても backup を復元できるよう、置換前に対象化する。
env_changed=1
if mv "$env_tmp" "$env_file"; then :; else code=$?; rollback "$code"; fi

if (( settings_present )); then
  if settings_tmp="$(mktemp "${settings}.tailscale.XXXXXX")"; then :; else code=$?; rollback "$code"; fi
  if jq --arg api "$canonical_origin" '.api_base_url=$api' "$settings" >"$settings_tmp"; then :; else code=$?; rollback "$code"; fi
  if chmod 600 "$settings_tmp"; then :; else code=$?; rollback "$code"; fi
  settings_changed=1
  if mv "$settings_tmp" "$settings"; then :; else code=$?; rollback "$code"; fi
fi

if compose "$profile" up -d --no-deps --force-recreate minutes-api; then :; else code=$?; rollback "$code"; fi
if (( serve_is_empty )); then
  serve_created=1
  if tailscale serve --bg --yes --https=443 "$expected_proxy"; then :; else code=$?; rollback "$code"; fi
fi

if applied_json="$(tailscale serve status --json)"; then :; else code=$?; rollback "$code"; fi
if jq -e --arg hp "$expected_hostport" --arg proxy "$expected_proxy" '
  .TCP == {"443":{"HTTPS":true}} and
  .Web == {($hp):{"Handlers":{"/":{"Proxy":$proxy}}}} and
  ((.AllowFunnel // {}) == {}) and ((.Services // {}) == {}) and ((.Foreground // {}) == {}) and
  ((keys - ["AllowFunnel","Foreground","Services","TCP","Web"]) | length == 0)
' <<<"$applied_json" >/dev/null; then :; else code=$?; rollback "$code"; fi

deadline=$((SECONDS + health_timeout))
health_ready=0
while (( SECONDS <= deadline )); do
  if health_json="$(curl --fail --silent --show-error --max-time 10 "$canonical_origin/v1/health" 2>/dev/null)" \
      && jq -e '.status=="ok"' <<<"$health_json" >/dev/null 2>&1; then
    health_ready=1
    break
  fi
  (( SECONDS < deadline )) || break
  sleep 2
done
if (( health_ready != 1 )); then
  echo "Tailscale HTTPS の証明書または API health を確認できません" >&2
  rollback 8
fi

trap - ERR INT TERM HUP
cleanup_backups
trap - EXIT
echo "TAILSCALE_READY origin=$canonical_origin profile=$profile"
