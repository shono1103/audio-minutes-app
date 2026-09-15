#!/usr/bin/env bash
# audio-minutes 導入前のホスト検査 (読み取り専用)。
#
# 出力はタブ区切り `key<TAB>value`。機械処理しやすいよう 1 行 1 項目とする。
# シリアル番号、Hardware UUID、ユーザー名、ホームディレクトリ、認証情報の値は出力しない。
# API 課金へ切り替わり得る環境変数は存在有無 (present/absent) だけを出す。
#
# 使い方: inspect-host.sh [--json]
set -euo pipefail

json_mode=0
if [[ "${1:-}" == "--json" ]]; then json_mode=1; fi

declare -a keys=()
declare -a values=()

emit() {
  # emit <key> <value>。value の改行・タブは空白に潰す
  local key="$1" value="${2:-}"
  value="${value//$'\t'/ }"
  value="${value//$'\n'/ }"
  keys+=("$key")
  values+=("$value")
}

has() { command -v "$1" >/dev/null 2>&1; }

version_of() {
  # version_of <cmd> <args...>: 先頭行だけを返す。失敗時は "unknown"
  local out
  if out="$("$@" 2>/dev/null | head -n 1)"; then
    printf '%s' "${out:-unknown}"
  else
    printf 'unknown'
  fi
}

present_or_absent() {
  # 環境変数の値を読まず、設定されているかだけを返す
  if [[ -n "${!1:-}" ]]; then printf 'present'; else printf 'absent'; fi
}

os_name="$(uname -s)"
arch="$(uname -m)"
emit os.kernel "$os_name"
emit os.arch "$arch"

# --- OS ----------------------------------------------------------------------
if [[ "$os_name" == "Darwin" ]]; then
  emit os.name macOS
  emit os.version "$(sw_vers -productVersion 2>/dev/null || echo unknown)"
  emit os.build "$(sw_vers -buildVersion 2>/dev/null || echo unknown)"
  # macOS 14.2 以降が録音クライアントの対応範囲
  major="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"
  minor="$(sw_vers -productVersion 2>/dev/null | cut -d. -f2)"
  if [[ -n "$major" ]] && { (( major > 14 )) || { (( major == 14 )) && (( ${minor:-0} >= 2 )); }; }; then
    emit os.recorder_supported yes
  else
    emit os.recorder_supported no
  fi
  # ハードウェア: 全情報を出さず、必要な安全な項目だけ抽出する
  chip="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo unknown)"
  emit cpu.model "$chip"
  emit cpu.logical_cores "$(sysctl -n hw.logicalcpu 2>/dev/null || echo unknown)"
  emit cpu.physical_cores "$(sysctl -n hw.physicalcpu 2>/dev/null || echo unknown)"
  mem_bytes="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
  emit memory.total_bytes "$mem_bytes"
  emit memory.total_gib "$(( mem_bytes / 1024 / 1024 / 1024 ))"
  if has system_profiler; then
    gpu_info="$(system_profiler SPDisplaysDataType 2>/dev/null || true)"
    emit gpu.model "$(printf '%s\n' "$gpu_info" | awk -F': ' '/Chipset Model/ {print $2; exit}')"
    emit gpu.cores "$(printf '%s\n' "$gpu_info" | awk -F': ' '/Total Number of Cores/ {print $2; exit}')"
    emit gpu.metal "$(printf '%s\n' "$gpu_info" | awk -F': ' '/Metal/ {print $2; exit}')"
  fi
  emit disk.free_bytes "$(df -k / | awk 'NR==2 {print $4 * 1024}')"
  emit disk.free_gib "$(df -g / | awk 'NR==2 {print $4}')"
elif [[ "$os_name" == "Linux" ]]; then
  emit os.name Linux
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    emit os.version "${PRETTY_NAME:-unknown}"
  fi
  emit os.recorder_supported no
  emit cpu.model "$(awk -F': ' '/model name/ {print $2; exit}' /proc/cpuinfo 2>/dev/null || echo unknown)"
  emit cpu.logical_cores "$(nproc 2>/dev/null || echo unknown)"
  mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
  emit memory.total_bytes "$(( mem_kb * 1024 ))"
  emit memory.total_gib "$(( mem_kb / 1024 / 1024 ))"
  emit disk.free_bytes "$(df -k / | awk 'NR==2 {print $4 * 1024}')"
  # Linux の GPU / アクセラレーター (有無だけ)
  if has vulkaninfo; then emit gpu.vulkan.tool present; emit gpu.vulkan.devices "$(vulkaninfo --summary 2>/dev/null | grep -c 'deviceName' || echo 0)"; else emit gpu.vulkan.tool absent; fi
  if has nvidia-smi; then emit gpu.cuda.tool present; else emit gpu.cuda.tool absent; fi
  if [[ -d /dev/dri ]]; then emit gpu.dri.devices "$(ls /dev/dri 2>/dev/null | tr '\n' ' ')"; else emit gpu.dri.devices none; fi
else
  emit os.name "$os_name"
  emit os.recorder_supported no
fi

# --- Colima / Docker --------------------------------------------------------------
if has colima; then
  emit colima.installed yes
  emit colima.version "$(colima version 2>/dev/null | awk '/colima version/ {print $3; exit}')"
  if has jq && colima_json="$(colima list --json 2>/dev/null)"; then
    count=0
    while IFS= read -r line; do
      [[ -n "$line" ]] || continue
      name="$(jq -r '.name' <<<"$line")"
      emit "colima.profile.${name}.status" "$(jq -r '.status' <<<"$line")"
      emit "colima.profile.${name}.arch" "$(jq -r '.arch' <<<"$line")"
      emit "colima.profile.${name}.cpus" "$(jq -r '.cpus' <<<"$line")"
      emit "colima.profile.${name}.memory_bytes" "$(jq -r '.memory' <<<"$line")"
      emit "colima.profile.${name}.disk_bytes" "$(jq -r '.disk' <<<"$line")"
      emit "colima.profile.${name}.runtime" "$(jq -r '.runtime' <<<"$line")"
      count=$((count + 1))
    done <<<"$colima_json"
    emit colima.profiles "$count"
    # VM type (vz / krunkit) は各 profile の lima 設定から読む (存在すれば)
    for cfg in "${HOME}"/.colima/*/colima.yaml; do
      [[ -f "$cfg" ]] || continue
      pname="$(basename "$(dirname "$cfg")")"
      vmtype="$(awk -F': ' '/^vmType:/ {print $2; exit}' "$cfg" 2>/dev/null || true)"
      emit "colima.profile.${pname}.vm_type" "${vmtype:-unknown}"
    done
  else
    emit colima.profiles unknown
  fi
else
  emit colima.installed no
fi

if has docker; then
  emit docker.cli yes
  emit docker.cli_version "$(docker version --format '{{.Client.Version}}' 2>/dev/null || echo unknown)"
  emit docker.context "$(docker context show 2>/dev/null || echo unknown)"
  if docker info --format '{{.ServerVersion}}' >/dev/null 2>&1; then
    emit docker.daemon reachable
    emit docker.server_version "$(docker info --format '{{.ServerVersion}}' 2>/dev/null)"
    emit docker.server_arch "$(docker info --format '{{.Architecture}}' 2>/dev/null)"
    emit docker.server_cpus "$(docker info --format '{{.NCPU}}' 2>/dev/null)"
    emit docker.server_memory_bytes "$(docker info --format '{{.MemTotal}}' 2>/dev/null)"
  else
    emit docker.daemon unreachable
  fi
  if docker compose version >/dev/null 2>&1; then
    emit docker.compose "$(docker compose version --short 2>/dev/null || echo present)"
  else
    emit docker.compose absent
  fi
  if docker buildx version >/dev/null 2>&1; then emit docker.buildx present; else emit docker.buildx absent; fi
else
  emit docker.cli no
fi

# --- ツール ---------------------------------------------------------------------------
for tool in ffmpeg ffprobe cmake git python3 uv swift node npm openssl jq; do
  if has "$tool"; then emit "tool.${tool}" present; else emit "tool.${tool}" absent; fi
done
if has ffmpeg; then emit tool.ffmpeg.version "$(ffmpeg -version 2>/dev/null | head -n1 | awk '{print $3}')"; fi
if has swift; then emit tool.swift.version "$(swift --version 2>&1 | grep -o 'Swift version [0-9.]*' | head -n1 | awk '{print $3}')"; fi
if has python3; then emit tool.python3.version "$(python3 --version 2>&1 | awk '{print $2}')"; fi

# codec の probe / decode 可否 (ffmpeg のデコーダー一覧から)
if has ffmpeg; then
  decoders="$(ffmpeg -hide_banner -decoders 2>/dev/null || true)"
  for pair in "wav:pcm_s16le" "aac:aac" "mp3:mp3" "flac:flac"; do
    fmt="${pair%%:*}"; dec="${pair##*:}"
    if grep -q " ${dec} " <<<"$decoders"; then emit "codec.${fmt}.decode" yes; else emit "codec.${fmt}.decode" no; fi
  done
fi

# --- Claude CLI (ホスト側) ---------------------------------------------------------------
# minutes-worker コンテナ内の CLI は別途 `scripts/service.sh status` で検査する
if has claude; then
  emit claude.host.installed yes
  emit claude.host.version "$(claude --version 2>/dev/null | head -n1 | awk '{print $1}')"
  auth_help="$(claude auth --help 2>/dev/null || true)"
  login_help="$(claude auth login --help 2>/dev/null || true)"
  status_help="$(claude auth status --help 2>/dev/null || true)"
  if grep -q 'login' <<<"$auth_help"; then emit claude.host.auth_login yes; else emit claude.host.auth_login no; fi
  if grep -q -- '--claudeai' <<<"$login_help"; then emit claude.host.auth_login_claudeai yes; else emit claude.host.auth_login_claudeai no; fi
  if grep -q -- '--json' <<<"$status_help"; then emit claude.host.auth_status_json yes; else emit claude.host.auth_status_json no; fi
  if grep -q 'logout' <<<"$auth_help"; then emit claude.host.auth_logout yes; else emit claude.host.auth_logout no; fi
else
  emit claude.host.installed no
fi
if has codex; then emit codex.host.installed yes; emit codex.host.version "$(version_of codex --version)"; else emit codex.host.installed no; fi

# API 課金用・外部 provider の認証設定 (値は読まない)
for var in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY AWS_PROFILE GOOGLE_APPLICATION_CREDENTIALS; do
  emit "env.${var}" "$(present_or_absent "$var")"
done

# --- 対応アプリと Chrome 連携 (macOS) ------------------------------------------------------
if [[ "$os_name" == "Darwin" ]]; then
  app_present() {
    # bundle ID で /Applications と ~/Applications を探す (mdfind が使えれば使う)
    local bundle_id="$1"
    if has mdfind && [[ -n "$(mdfind "kMDItemCFBundleIdentifier == '${bundle_id}'" 2>/dev/null | head -n1)" ]]; then
      printf 'present'; return
    fi
    local name
    for name in "${@:2}"; do
      if [[ -d "/Applications/${name}.app" || -d "${HOME}/Applications/${name}.app" ]]; then printf 'present'; return; fi
    done
    printf 'absent'
  }
  emit app.zoom "$(app_present us.zoom.xos zoom.us)"
  emit app.teams "$(app_present com.microsoft.teams2 'Microsoft Teams' 'Microsoft Teams (work or school)')"
  emit app.chrome "$(app_present com.google.Chrome 'Google Chrome')"
  if [[ -d "/Applications/Google Chrome.app" ]]; then
    emit app.chrome.version "$(defaults read '/Applications/Google Chrome.app/Contents/Info' CFBundleShortVersionString 2>/dev/null || echo unknown)"
  fi
  host_manifest="${HOME}/Library/Application Support/Google/Chrome/NativeMessagingHosts/dev.audio_minutes.bridge.json"
  if [[ -f "$host_manifest" ]]; then
    emit chrome.native_host.manifest present
    if has jq; then
      emit chrome.native_host.path_exists "$( [[ -x "$(jq -r '.path' "$host_manifest" 2>/dev/null)" ]] && echo yes || echo no )"
      emit chrome.native_host.allowed_origins "$(jq -r '.allowed_origins | length' "$host_manifest" 2>/dev/null || echo unknown)"
    fi
  else
    emit chrome.native_host.manifest absent
  fi
  # 拡張 (開発版) の導入有無は Chrome の Preferences を読まず、GUI 側の接続確認に任せる
  emit chrome.extension.dev_id cglcpocpendfgbhepidgbpilokapdlnm
  # 権限 (TCC) は読み取り専用で判定できないため、GUI 起動時の案内に委ねる
  emit permissions.microphone unknown_until_gui
  emit permissions.system_audio unknown_until_gui
fi

# --- audio-minutes 自身の状態 ---------------------------------------------------------
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# root の scripts/ からの symlink 経由でも、skill 本体から直接呼んでも同じ repo を指す。
repo_root="$(git -C "${script_dir}" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$repo_root" ]]; then
  repo_root="$(cd "${script_dir}/../../../.." && pwd -P)"
fi
emit repo.root_present "$( [[ -f "${repo_root}/deploy/compose.yml" ]] && echo yes || echo no )"
for profile in macos-colima-cpu macos-colima-krunkit-vulkan linux-cpu; do
  if [[ -f "${repo_root}/deploy/profiles/${profile}/.env" ]]; then emit "deploy.profile.${profile}.env" present; else emit "deploy.profile.${profile}.env" absent; fi
done
emit deploy.settings_schema "$( [[ -f "${repo_root}/deploy/settings.schema.json" ]] && echo present || echo absent )"
for lock in services/minutes-api/uv.lock services/transcription-worker/uv.lock services/minutes-worker/uv.lock; do
  emit "deploy.lockfile.$(basename "$(dirname "$lock")")" "$( [[ -f "${repo_root}/${lock}" ]] && echo present || echo absent )"
done
client_settings="${HOME}/Library/Application Support/AudioMinutes/settings.json"
emit client.settings "$( [[ -f "$client_settings" ]] && echo present || echo absent )"

# --- 出力 ---------------------------------------------------------------------------------
if (( json_mode )); then
  printf '{'
  for i in "${!keys[@]}"; do
    (( i > 0 )) && printf ','
    printf '"%s":"%s"' "${keys[$i]}" "$(printf '%s' "${values[$i]}" | sed 's/\\/\\\\/g; s/"/\\"/g')"
  done
  printf '}\n'
else
  for i in "${!keys[@]}"; do
    printf '%s\t%s\n' "${keys[$i]}" "${values[$i]}"
  done
fi
