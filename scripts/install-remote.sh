#!/usr/bin/env bash
# 手元の未コミット実装を、管理対象と確認できた macOS ホストだけへ安全に導入する。
set -euo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/.." && pwd -P)"
remote_host="shonoshono@192.168.0.31"
profile="macos-colima-cpu"
configure_tailscale=0
remote_target='~/.local/share/audio-minutes/repository'
# 利用者の known_hosts はそのまま使い、未知の鍵は明示確認、変更済みの鍵は拒否する。
ssh_host_key_options=(-o StrictHostKeyChecking=ask)

usage() {
  cat <<'EOF'
usage: ./scripts/install-remote.sh [--host USER@HOST] [--tailscale]

既定の接続先 shonoshono@192.168.0.31 へ、現在の worktree（未コミット変更を含む）を
転送し、macos-colima-cpu profile を導入します。SSH の認証順とホスト鍵検証は
OpenSSH の通常設定を使用し、必要ならパスワードを端末へ直接入力します。
--tailscale を指定すると、通常導入後に Tailscale Serve の HTTPS 入口も安全に設定します。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) remote_host="${2:-}"; shift 2 ;;
    --tailscale) configure_tailscale=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

# option 注入や意図しない shell 展開を避けるため、接続先は単純な user@host のみに絞る。
if [[ ! "$remote_host" =~ ^[A-Za-z0-9._-]+@([A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])$ ]]; then
  echo "接続先は USER@HOST 形式で指定してください: $remote_host" >&2
  exit 2
fi

for tool in git tar ssh scp shasum mktemp; do
  command -v "$tool" >/dev/null 2>&1 || { echo "必要なコマンドがありません: $tool" >&2; exit 2; }
done
git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "Git worktree ではありません: $repo_root" >&2
  exit 2
}

temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/audio-minutes-remote.XXXXXX")"
remote_lock_may_exist=0
cleanup() {
  rm -rf -- "$temp_dir"
  if (( remote_lock_may_exist == 1 )); then
    print_lock_recovery
  fi
}
trap cleanup EXIT
file_list="$temp_dir/files.list"
candidate_list="$temp_dir/candidates.list"
materialized_root="$temp_dir/materialized"
archive="$temp_dir/source.tar"

is_transferable() {
  local path="$1" base="${1##*/}"
  case "$path" in
    /*|../*|*/../*) return 1 ;;
  esac
  case "/$path/" in
    */.git/*|*/.claude/*|*/.agents/*|*/.ssh/*|*/.aws/*|*/credentials/*|*/secrets/*|*/node_modules/*|*/.venv/*|*/venv/*|*/__pycache__/*|*/.pytest_cache/*|*/.ruff_cache/*|*/.mypy_cache/*|*/.swiftpm/*|*/.build/*|*/build/*|*/dist/*|*/DerivedData/*|*/coverage/*|*/htmlcov/*|*/.cache/*|*/model-cache/*|*/models/cache/*|*/recordings/*|*/artifacts/*|*/benchmarks/data/*)
      return 1 ;;
  esac
  case "$base" in
    .env.example|*.env.example) return 0 ;;
    .env|.env.*|.netrc|.npmrc|.pypirc|.git-credentials|*.pem|*.key|*.p12|*.pfx|*.mobileprovision|id_rsa|id_dsa|id_ecdsa|id_ed25519|credentials|credentials.json|secrets.json|auth.json|token.json)
      return 1 ;;
    .DS_Store|*.wav|*.mp3|*.m4a|*.aac|*.flac|*.ogg|*.opus|*.webm) return 1 ;;
  esac
  return 0
}

validate_local_regular_path() {
  local path="$1" expected_type="${2:-file}" current='' component
  local -a components=()
  case "$path" in
    *$'\n'*|*$'\r'*|*$'\t'*)
      echo "制御文字を含むパスは転送できません" >&2
      return 1 ;;
  esac
  IFS=/ read -r -a components <<<"$path"
  for component in "${components[@]}"; do
    current="${current:+$current/}$component"
    if [[ -L "$repo_root/$current" ]]; then
      echo "転送対象または祖先が symlink のため拒否します: $current" >&2
      return 1
    fi
  done
  if [[ -e "$repo_root/$path" ]]; then
    case "$expected_type" in
      file) [[ -f "$repo_root/$path" ]] || { echo "転送対象が通常ファイルではありません: $path" >&2; return 1; } ;;
      directory) [[ -d "$repo_root/$path" ]] || { echo "期待するディレクトリではありません: $path" >&2; return 1; } ;;
      *) echo "内部エラー: 未対応の実体種別です" >&2; return 1 ;;
    esac
  fi
}

materialize_regular_file() {
  local source_path="$1" destination_path="$2" destination
  validate_local_regular_path "$source_path" || return 1
  [[ -f "$repo_root/$source_path" && ! -L "$repo_root/$source_path" ]] || {
    echo "実体化する通常ファイルを確認できません: $source_path" >&2
    return 1
  }
  destination="$materialized_root/$destination_path"
  mkdir -p "$(dirname "$destination")"
  [[ ! -e "$destination" && ! -L "$destination" ]] || {
    echo "実体化先が重複しています: $destination_path" >&2
    return 1
  }
  /bin/cp -p "$repo_root/$source_path" "$destination"
  [[ -f "$destination" && ! -L "$destination" ]] || {
    echo "通常ファイルとして実体化できません: $destination_path" >&2
    return 1
  }
  printf '%s\0' "$destination_path" >>"$file_list"
  ((file_count += 1))
}

materialize_allowed_link() {
  local path="$1" raw_target target_member destination_member matched=0
  raw_target="$(readlink "$repo_root/$path")" || {
    echo "symlink の読取に失敗しました: $path" >&2
    return 1
  }
  case "$path" in
    scripts/inspect-host.sh)
      [[ "$raw_target" == '../.claude/skills/install/scripts/inspect-host.sh' ]] || {
        echo "allowlist と異なる symlink を拒否します: $path" >&2
        return 1
      }
      target_member='.claude/skills/install/scripts/inspect-host.sh'
      git -C "$repo_root" check-ignore -q -- "$target_member" && {
        echo "allowlist symlink の解決先が ignored のため拒否します: $path" >&2
        return 1
      }
      materialize_regular_file "$target_member" "$path"
      ;;
    packages/python/audio_minutes_contracts/schemas)
      [[ "$raw_target" == '../../../contracts/schemas' ]] || {
        echo "allowlist と異なる symlink を拒否します: $path" >&2
        return 1
      }
      validate_local_regular_path 'contracts/schemas' directory || return 1
      [[ -d "$repo_root/contracts/schemas" ]] || {
        echo "schemas symlink の解決先が壊れています" >&2
        return 1
      }
      while IFS= read -r -d '' target_member; do
        case "$target_member" in
          contracts/schemas/*)
            is_transferable "$target_member" || continue
            # index にだけ残る未ステージ削除は通常候補と同じく欠落として扱う。
            # ただし欠落判定より先に祖先 symlink を検査し、外部参照への置換は拒否する。
            validate_local_regular_path "$target_member" || return 1
            [[ -e "$repo_root/$target_member" ]] || continue
            destination_member="$path/${target_member#contracts/schemas/}"
            materialize_regular_file "$target_member" "$destination_member" || return 1
            ((matched += 1))
            ;;
        esac
      done <"$candidate_list"
      (( matched > 0 )) || {
        echo "schemas symlink の安全な転送対象がありません" >&2
        return 1
      }
      ;;
    *)
      echo "allowlist 外の symlink を拒否します: $path" >&2
      return 1 ;;
  esac
}

mkdir "$materialized_root"
: >"$file_list"
git -C "$repo_root" ls-files --cached --others --exclude-standard -z >"$candidate_list"
file_count=0
while IFS= read -r -d '' path; do
  is_transferable "$path" || continue
  if [[ -L "$repo_root/$path" ]]; then
    materialize_allowed_link "$path" || exit 10
    continue
  fi
  validate_local_regular_path "$path" || exit 10
  [[ -e "$repo_root/$path" ]] || continue
  materialize_regular_file "$path" "$path" || exit 10
done <"$candidate_list"

(( file_count > 0 )) || { echo "転送対象ファイルがありません" >&2; exit 2; }
if find -P "$materialized_root" -type l -print -quit | grep -q . \
    || find -P "$materialized_root" -type f -links +1 -print -quit | grep -q . \
    || find -P "$materialized_root" ! -type f ! -type d -print -quit | grep -q .; then
  echo "ローカル stage に link または特殊ファイルを検出したため拒否します" >&2
  exit 10
fi
COPYFILE_DISABLE=1 tar -C "$materialized_root" --null -T "$file_list" -cf "$archive"
archive_sha256="$(shasum -a 256 "$archive" | awk '{print $1}')"
stage_id="$(date -u '+%Y%m%dT%H%M%SZ')-$$-${temp_dir##*.}"

print_lock_recovery() {
  echo "lock: ~/.local/share/audio-minutes/install-remote.lock (owner=$stage_id)" >&2
  echo "切断後に lock が残った場合のみ、$remote_host へ SSH 接続し、owner ファイルが上記値と一致し同時実行が無いことを確認してください" >&2
  echo "確認後は owner ファイルを削除し、空の install-remote.lock を rmdir して wrapper を再実行します（stage は調査用に保持）" >&2
}

echo "接続先: $remote_host"
echo "転送元: $repo_root"
echo "転送対象: Git の追跡済みファイルと非 ignored 新規ファイル（未コミット変更を含む、${file_count}件）"
echo "除外: .git、実 .env、資格情報、音声データ、model/cache/build 生成物"
echo "固定配置先: $remote_target"
echo "導入内容: profile=${profile}、専用 Colima、3 image、固定 CPU model、offline smoke、DB migration、client、readiness、Tailscale=${configure_tailscale}"
echo "認証: OpenSSH の通常順（公開鍵の後に必要なら端末でパスワード入力）。認証内容は保存・記録しません"
printf '接続してホスト情報を確認しますか？ [y/N] '
read -r local_answer
[[ "$local_answer" == "y" || "$local_answer" == "Y" ]] || { echo "中止しました"; exit 1; }

read -r -d '' preflight_script <<'REMOTE_PREFLIGHT' || true
set -euo pipefail
umask 077
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
base="$HOME/.local/share/audio-minutes"
target="$base/repository"
stage="$base/staging/$AM_STAGE_ID"
marker="$target/.audio-minutes-remote-managed"
lock="$base/install-remote.lock"
lock_owner="$lock/owner"
next="$base/.repository.next-$AM_STAGE_ID"
previous="$base/.repository.previous-$AM_STAGE_ID"
lock_acquired=0
lock_handoff=0

preflight_cleanup() {
  if (( lock_acquired == 1 && lock_handoff == 0 )) \
      && [[ -d "$lock" && ! -L "$lock" && -f "$lock_owner" && ! -L "$lock_owner" ]] \
      && [[ "$(<"$lock_owner")" == "$AM_STAGE_ID" ]]; then
    rm -f -- "$lock_owner"
    rmdir "$lock" 2>/dev/null || true
  fi
}
trap preflight_cleanup EXIT

[[ -n "$HOME" && "$HOME" != / ]] || { echo "安全な HOME を確認できません" >&2; exit 20; }
[[ "$target" == "$HOME/.local/share/audio-minutes/repository" ]] || { echo "固定配置先の検証に失敗しました" >&2; exit 20; }
for managed_path in "$HOME/.local" "$HOME/.local/share" "$base" "$base/staging" "$base/logs" \
    "$target" "$stage" "$lock" "$next" "$previous"; do
  [[ ! -L "$managed_path" ]] || { echo "管理パスが symlink のため拒否します: $managed_path" >&2; exit 21; }
done
if [[ -e "$target" ]]; then
  [[ -d "$target" ]] || { echo "配置先がディレクトリではありません: $target" >&2; exit 21; }
  [[ -f "$marker" && ! -L "$marker" ]] || { echo "非管理の既存ディレクトリを拒否します: $target" >&2; exit 21; }
  [[ "$(<"$marker")" == audio-minutes-remote-managed-v1 ]] || { echo "管理マーカーが一致しません: $marker" >&2; exit 21; }
fi

settings="$HOME/Library/Application Support/AudioMinutes/settings.json"
if [[ -e "$settings" || -L "$settings" ]]; then
  for settings_path in "$HOME/Library" "$HOME/Library/Application Support" \
      "$HOME/Library/Application Support/AudioMinutes" "$settings"; do
    [[ ! -L "$settings_path" ]] || { echo "ClientSettings のパスが symlink のため拒否します" >&2; exit 26; }
  done
  if command -v jq >/dev/null 2>&1; then
    if ! jq -e '.deploy_dir | type == "string" and startswith("/")' "$settings" >/dev/null 2>&1; then
      echo "既存 ClientSettings の deploy_dir が絶対パスではないため拒否します" >&2
      exit 26
    fi
    configured_deploy="$(jq -r '.deploy_dir' "$settings")"
    [[ "$configured_deploy" == "$target/deploy" ]] || {
      echo "既存 ClientSettings の deploy_dir が固定配置先と一致しません。既存配置を確認して移行してください" >&2
      exit 26
    }
    [[ -e "$target" ]] || {
      echo "固定配置先が無い一方で同名 ClientSettings があります。既存配置を確認して移行してください" >&2
      exit 26
    }
  else
    echo "既存 ClientSettings がありますが jq 未導入で deploy_dir を検証できないため停止します（内容は表示しません）" >&2
    exit 28
  fi
fi

if [[ ! -e "$target" ]]; then
  conflict=''
  if command -v colima >/dev/null 2>&1; then
    if ! command -v jq >/dev/null 2>&1; then
      echo "Colima は存在しますが jq 未導入で既存 profile を検証できないため停止します" >&2
      exit 28
    fi
    if ! colima_list="$(colima list --json 2>/dev/null)"; then
      echo "Colima profile の検査に失敗したため、衝突不明のまま cold install を続行しません" >&2
      exit 28
    fi
    if ! printf '%s\n' "$colima_list" | jq -s -e \
        'all(.[]; (type == "object" and (.name | type == "string")) or
          (type == "array" and all(.[]; type == "object" and (.name | type == "string"))))' \
        >/dev/null 2>&1; then
      echo "Colima profile の検査結果を解釈できないため、衝突不明のまま cold install を続行しません" >&2
      exit 28
    fi
    if printf '%s\n' "$colima_list" \
        | jq -s -e 'any(.[]; if type == "array" then any(.[]; .name == "audio-minutes-cpu") else .name == "audio-minutes-cpu" end)' >/dev/null 2>&1; then
      conflict='Colima profile'
    fi
  fi
  if [[ -z "$conflict" ]] && command -v docker >/dev/null 2>&1 \
      && docker context inspect colima-audio-minutes-cpu >/dev/null 2>&1; then
    compose_objects=''
    if compose_objects="$(docker --context colima-audio-minutes-cpu ps -a \
        --filter label=com.docker.compose.project=audio-minutes --format '{{.ID}}' 2>/dev/null)" \
        && [[ -n "$compose_objects" ]]; then
      conflict='Compose service'
    elif compose_objects="$(docker --context colima-audio-minutes-cpu volume ls \
        --filter label=com.docker.compose.project=audio-minutes -q 2>/dev/null)" \
        && [[ -n "$compose_objects" ]]; then
      conflict='Compose volume'
    fi
  fi
  if [[ -n "$conflict" ]]; then
    echo "固定配置先が無い一方で同名の $conflict があります。既存配置を確認して移行してください" >&2
    exit 27
  fi
fi

if [[ -e "$lock" || -L "$lock" ]]; then
  existing_owner='unknown'
  if [[ -d "$lock" && ! -L "$lock" && -f "$lock_owner" && ! -L "$lock_owner" ]]; then
    candidate_owner="$(<"$lock_owner")"
    [[ "$candidate_owner" =~ ^[A-Za-z0-9.-]+$ ]] && existing_owner="$candidate_owner"
  fi
  echo "別のリモート導入または切断後の lock があります: owner=$existing_owner" >&2
  echo "同時実行中でないことを確認し、SSH 上で owner が上記値と一致する場合だけ owner ファイルと空の lock ディレクトリを削除して再実行してください" >&2
  exit 25
fi

kernel="$(uname -s)"
hostname_value="$(hostname)"
model="$(sysctl -n hw.model 2>/dev/null || printf unknown)"
arch="$(uname -m)"
memory_bytes="$(sysctl -n hw.memsize 2>/dev/null || printf unknown)"
echo "リモートホスト情報（シリアル番号などの識別子は取得しません）"
echo "  hostname: $hostname_value"
echo "  OS: $kernel $(sw_vers -productVersion 2>/dev/null || true)"
echo "  model: $model"
echo "  arch: $arch"
echo "  memory_bytes: $memory_bytes"
[[ "$kernel" == Darwin ]] || { echo "macos-colima-cpu の対象外 OS です: $kernel" >&2; exit 22; }
echo "REMOTE_HOSTNAME_${AM_STAGE_ID}=$hostname_value"

if [[ "${AM_PREPARE:-0}" != 1 ]]; then
  exit 0
fi
[[ -n "${AM_EXPECTED_HOSTNAME:-}" && "$AM_EXPECTED_HOSTNAME" == "$hostname_value" ]] || {
  echo "確認後に接続先 hostname が変わったため拒否します" >&2
  exit 23
}

mkdir -p "$base/staging" "$base/logs"
chmod 700 "$base" "$base/staging" "$base/logs"
[[ ! -e "$stage" && ! -L "$stage" ]] || { echo "stage が既に存在します: $stage" >&2; exit 24; }
if ! mkdir "$lock" 2>/dev/null; then
  existing_owner='unknown'
  if [[ -d "$lock" && ! -L "$lock" && -f "$lock_owner" && ! -L "$lock_owner" ]]; then
    candidate_owner="$(<"$lock_owner")"
    [[ "$candidate_owner" =~ ^[A-Za-z0-9.-]+$ ]] && existing_owner="$candidate_owner"
  fi
  echo "別のリモート導入または切断後の lock があります: owner=$existing_owner" >&2
  echo "同時実行中でないことを確認し、SSH 上で owner が上記値と一致する場合だけ owner ファイルと空の lock ディレクトリを削除して再実行してください" >&2
  exit 25
fi
lock_acquired=1
printf '%s\n' "$AM_STAGE_ID" >"$lock_owner.tmp"
chmod 600 "$lock_owner.tmp"
mv "$lock_owner.tmp" "$lock_owner"
mkdir "$stage"
chmod 700 "$stage"
lock_handoff=1
echo "REMOTE_PREFLIGHT_OK target=$target stage=$stage"
exit 0
REMOTE_PREFLIGHT

inspection_output="$temp_dir/inspection.log"
set +e
ssh "${ssh_host_key_options[@]}" -T -- "$remote_host" \
  "AM_STAGE_ID=$stage_id /bin/bash -s" <<<"$preflight_script" | tee "$inspection_output"
inspection_codes=("${PIPESTATUS[@]}")
set -e
if (( inspection_codes[0] != 0 )); then
  echo "REMOTE_INSTALL_FAILED stage=inspection exit=${inspection_codes[0]}" >&2
  exit "${inspection_codes[0]}"
fi
if (( inspection_codes[1] != 0 )); then
  echo "REMOTE_INSTALL_FAILED stage=inspection-log exit=${inspection_codes[1]}" >&2
  exit "${inspection_codes[1]}"
fi

remote_hostname=''
while IFS= read -r inspection_line; do
  inspection_line="${inspection_line%$'\r'}"
  case "$inspection_line" in
    "REMOTE_HOSTNAME_${stage_id}="*) remote_hostname="${inspection_line#*=}" ;;
  esac
done <"$inspection_output"
[[ "$remote_hostname" =~ ^[A-Za-z0-9._-]+$ ]] || {
  echo "リモート hostname を安全に確認できませんでした" >&2
  exit 23
}
echo "このホストへ転送する場合は次を正確に入力してください: DEPLOY $remote_hostname"
IFS= read -r remote_confirmation
[[ "$remote_confirmation" == "DEPLOY $remote_hostname" ]] || { echo "ホスト確認が一致しないため中止しました" >&2; exit 23; }

set +e
ssh "${ssh_host_key_options[@]}" -T -- "$remote_host" \
  "AM_STAGE_ID=$stage_id AM_PREPARE=1 AM_EXPECTED_HOSTNAME=$remote_hostname /bin/bash -s" <<<"$preflight_script"
preflight_code=$?
set -e
if (( preflight_code != 0 )); then
  echo "REMOTE_INSTALL_FAILED stage=preflight exit=$preflight_code" >&2
  echo "接続断が REMOTE_PREFLIGHT_OK の直後だった場合は、リモートの install-remote.lock と owner を確認してください" >&2
  exit "$preflight_code"
fi
remote_lock_may_exist=1

read -r -d '' deploy_script <<'REMOTE_DEPLOY' || true
set -euo pipefail
umask 077
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
base="$HOME/.local/share/audio-minutes"
target="$base/repository"
stage="$base/staging/$AM_STAGE_ID"
archive="$stage/source.tar"
unpack="$stage/unpacked"
next="$base/.repository.next-$AM_STAGE_ID"
previous="$base/.repository.previous-$AM_STAGE_ID"
log="$base/logs/install-$AM_STAGE_ID.log"
lock="$base/install-remote.lock"
lock_owner="$lock/owner"
marker_name='.audio-minutes-remote-managed'
marker_value='audio-minutes-remote-managed-v1'
lock_owned=0

fail() {
  local code="$1" phase="$2"
  echo "REMOTE_INSTALL_FAILED stage=$phase exit=$code" >&2
  echo "stage: $stage" >&2
  echo "log: $log" >&2
  if [[ "${AM_ENABLE_TAILSCALE:-0}" == 1 ]]; then
    echo "再開: cd $target && ./install.sh --yes --profile macos-colima-cpu --tailscale" >&2
  else
    echo "再開: cd $target && ./install.sh --yes --profile macos-colima-cpu" >&2
  fi
  exit "$code"
}

release_owned_lock() {
  if (( lock_owned == 1 )) \
      && [[ -d "$lock" && ! -L "$lock" && -f "$lock_owner" && ! -L "$lock_owner" ]] \
      && [[ "$(<"$lock_owner")" == "$AM_STAGE_ID" ]]; then
    rm -f -- "$lock_owner"
    rmdir "$lock" 2>/dev/null || true
  fi
}

deploy_exit() {
  local code=$?
  trap - EXIT
  release_owned_lock
  exit "$code"
}

validate_preserved_env_ancestors() {
  local relative="$1" current="$target" component
  local -a components=()
  IFS=/ read -r -a components <<<"$relative"
  for component in "${components[@]}"; do
    current="$current/$component"
    [[ ! -L "$current" ]] || {
      echo "既存 .env の対象または祖先が symlink のため保持を拒否します: $relative" >&2
      fail 37 preserve
    }
  done
}

[[ -n "$HOME" && "$HOME" != / && "$target" == "$HOME/.local/share/audio-minutes/repository" ]] || fail 30 target
for managed_path in "$HOME/.local" "$HOME/.local/share" "$base" "$base/staging" "$base/logs" \
    "$target" "$stage" "$lock" "$next" "$previous"; do
  [[ ! -L "$managed_path" ]] || fail 31 target
done
mkdir -p "$base/logs"
chmod 700 "$base/logs"
[[ -d "$lock" && ! -L "$lock" && -f "$lock_owner" && ! -L "$lock_owner" ]] || fail 38 lock
[[ "$(<"$lock_owner")" == "$AM_STAGE_ID" ]] || fail 38 lock
lock_owned=1
trap deploy_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ -e "$target" ]]; then
  [[ -d "$target" && -f "$target/$marker_name" && ! -L "$target/$marker_name" ]] || fail 31 target
  [[ "$(<"$target/$marker_name")" == "$marker_value" ]] || fail 31 target
  validate_preserved_env_ancestors 'deploy/profiles/macos-colima-cpu/.env'
fi
[[ -f "$archive" && ! -L "$archive" ]] || fail 32 archive
actual_sha="$(shasum -a 256 "$archive" | awk '{print $1}')"
[[ "$actual_sha" == "$AM_ARCHIVE_SHA256" ]] || fail 33 checksum

[[ ! -e "$unpack" && ! -L "$unpack" && ! -e "$next" && ! -L "$next" && ! -e "$previous" && ! -L "$previous" ]] || fail 34 stage
member_list="$stage/archive-members.txt"
verbose_list="$stage/archive-verbose.txt"
LC_ALL=C tar -tf "$archive" >"$member_list" || fail $? archive
while IFS= read -r member; do
  normalized="${member#./}"
  [[ -n "$normalized" && "$normalized" != . && "$normalized" != /* \
      && "$normalized" != .. && "$normalized" != ../* && "$normalized" != */../* \
      && "$normalized" != */.. && "$normalized" != *//* ]] || {
    echo "安全でない archive member を拒否します" >&2
    fail 39 archive
  }
done <"$member_list"
LC_ALL=C tar -tvf "$archive" >"$verbose_list" || fail $? archive
while IFS= read -r verbose_member; do
  member_type="${verbose_member%"${verbose_member#?}"}"
  case "$member_type" in
    -|d) ;;
    *) echo "link または特殊形式の archive member を拒否します" >&2; fail 39 archive ;;
  esac
done <"$verbose_list"
mkdir "$unpack"
tar -C "$unpack" -xf "$archive" || fail $? unpack
if find -P "$unpack" -type l -print -quit | grep -q . \
    || find -P "$unpack" -type f -links +1 -print -quit | grep -q . \
    || find -P "$unpack" ! -type f ! -type d -print -quit | grep -q .; then
  echo "展開後に link または特殊ファイルを検出したため拒否します" >&2
  fail 40 verify
fi
for required in install.sh scripts/install.sh scripts/doctor.sh scripts/readiness.sh scripts/service.sh scripts/configure-tailscale.sh deploy/compose.yml deploy/profiles/macos-colima-cpu/.env.example services/minutes-api/uv.lock services/transcription-worker/uv.lock services/minutes-worker/uv.lock; do
  [[ -f "$unpack/$required" && ! -L "$unpack/$required" ]] || { echo "必須ファイルがありません: $required" >&2; fail 35 verify; }
done
if find "$unpack" -type f \( -name .env -o -name '.env.*' \) ! -name '.env.example' ! -name '*.env.example' -print -quit | grep -q .; then
  echo "転送アーカイブに実 .env が含まれています" >&2
  fail 36 verify
fi

# remote umask 077 で tar が狭めた mode を、検証済み source tree 内だけで実行時契約へ戻す。
# user-executable だった file だけ実行可能にし、world-write は一切付与しない。
find -P "$unpack" -type d -exec chmod 0755 {} + || fail $? permissions
find -P "$unpack" -type f -perm -0100 -exec chmod 0755 {} + || fail $? permissions
find -P "$unpack" -type f ! -perm -0100 -exec chmod 0644 {} + || fail $? permissions

mv "$unpack" "$next"
printf '%s\n' "$marker_value" >"$next/$marker_name"
chmod 600 "$next/$marker_name"

# 再配置でソース削除を反映しつつ、リモートで生成済みの設定だけは byte 単位で保持する。
if [[ -d "$target" ]]; then
  if find -P "$target" \( -name .env -o -name '.env.*' \) \
      ! -name '.env.example' ! -name '*.env.example' ! -type f -print -quit | grep -q .; then
    echo "既存 .env 系が通常ファイルではないため保持を拒否します" >&2
    fail 37 preserve
  fi
  while IFS= read -r -d '' old_env; do
    relative="${old_env#"$target/"}"
    [[ "$relative" != "$old_env" && "$relative" != /* && "$relative" != ../* && "$relative" != */../* ]] || fail 37 preserve
    validate_preserved_env_ancestors "$relative"
    mkdir -p "$next/$(dirname "$relative")"
    cp -p "$old_env" "$next/$relative" || fail $? preserve
    chmod 0600 "$next/$relative" || fail $? preserve
  done < <(find -P "$target" -type f \( -name .env -o -name '.env.*' \) ! -name '.env.example' ! -name '*.env.example' -print0)
  mv "$target" "$previous"
fi
set +e
mv "$next" "$target"
activate_code=$?
set -e
if (( activate_code != 0 )); then
  [[ ! -e "$target" && -d "$previous" ]] && mv "$previous" "$target"
  fail "$activate_code" activate
fi

cd "$target"
install_args=(--yes --profile macos-colima-cpu)
[[ "${AM_ENABLE_TAILSCALE:-0}" == 1 ]] && install_args+=(--tailscale)
set +e
./install.sh "${install_args[@]}" 2>&1 | tee "$log"
install_pipeline_codes=("${PIPESTATUS[@]}")
set -e
(( install_pipeline_codes[0] == 0 )) || fail "${install_pipeline_codes[0]}" install
(( install_pipeline_codes[1] == 0 )) || fail "${install_pipeline_codes[1]}" log

if [[ -d "$previous" && -f "$previous/$marker_name" && "$(<"$previous/$marker_name")" == "$marker_value" ]]; then
  rm -rf -- "$previous"
fi
rm -rf -- "$stage"
echo "REMOTE_INSTALL_READY target=$target log=$log"
REMOTE_DEPLOY

deploy_file="$temp_dir/deploy.sh"
printf '%s\n' "$deploy_script" >"$deploy_file"
remote_stage=".local/share/audio-minutes/staging/$stage_id"
set +e
scp "${ssh_host_key_options[@]}" -- "$archive" "$deploy_file" "$remote_host:$remote_stage/"
transfer_code=$?
set -e
if (( transfer_code != 0 )); then
  if (( configure_tailscale )); then tailscale_arg=' --tailscale'; else tailscale_arg=''; fi
  echo "REMOTE_INSTALL_FAILED stage=transfer exit=$transfer_code" >&2
  echo "stage: ~/$remote_stage" >&2
  echo "再開: $repo_root/scripts/install-remote.sh --host $remote_host${tailscale_arg}" >&2
  print_lock_recovery
  remote_lock_may_exist=0
  exit "$transfer_code"
fi

set +e
ssh "${ssh_host_key_options[@]}" -tt -- "$remote_host" "AM_STAGE_ID=$stage_id AM_ARCHIVE_SHA256=$archive_sha256 AM_ENABLE_TAILSCALE=$configure_tailscale /bin/bash \"\$HOME/$remote_stage/deploy.sh\""
deploy_code=$?
set -e
if (( deploy_code != 0 )); then
  echo "REMOTE_INSTALL_FAILED stage=remote exit=$deploy_code" >&2
  echo "stage: ~/.local/share/audio-minutes/staging/$stage_id" >&2
  echo "log: ~/.local/share/audio-minutes/logs/install-$stage_id.log" >&2
  if (( configure_tailscale )); then tailscale_arg=' --tailscale'; else tailscale_arg=''; fi
  echo "再開: ssh -t $remote_host \"/bin/bash -lc 'export PATH=/opt/homebrew/bin:/usr/local/bin:\$PATH; cd ~/.local/share/audio-minutes/repository && ./install.sh --yes --profile macos-colima-cpu${tailscale_arg}'\"" >&2
  print_lock_recovery
  remote_lock_may_exist=0
  exit "$deploy_code"
fi

remote_lock_may_exist=0
echo "リモート導入が完了しました: $remote_host:$remote_target"
