#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "${script_dir}/.." && pwd -P)"

validate_profile() {
  case "$1" in
    macos-colima-cpu|macos-colima-krunkit-vulkan|linux-cpu) ;;
    *) echo "未対応の profile です: $1" >&2; return 2 ;;
  esac
}

profile_env() {
  if [[ -n "${AM_PROFILE_ENV_OVERRIDE:-}" ]]; then
    printf '%s' "$AM_PROFILE_ENV_OVERRIDE"
    return
  fi
  printf '%s/deploy/profiles/%s/.env' "$repo_root" "$1"
}

profile_value() {
  local profile="$1" key="$2" env_file value
  env_file="$(profile_env "$profile")"
  [[ -f "$env_file" ]] || return 0
  value="$(grep -E "^${key}=" "$env_file" | tail -1 | cut -d= -f2- | tr -d '\r')"
  printf '%s' "$value"
}

docker_context() {
  profile_value "$1" AM_DOCKER_CONTEXT
}

colima_profile() {
  local configured
  configured="$(profile_value "$1" AM_COLIMA_PROFILE)"
  printf '%s' "${configured:-default}"
}

docker_for_profile() {
  local profile="$1" context
  shift
  context="$(docker_context "$profile")"
  if [[ -n "$context" ]]; then
    docker --context "$context" "$@"
  else
    docker "$@"
  fi
}

compose_project() {
  # 既存配置へ触れずに backup / restore や migration を検証するときだけ、
  # 呼出し側が隔離 project 名を明示できる。未指定時の運用名は従来どおり。
  if [[ -n "${AM_COMPOSE_PROJECT:-}" ]]; then
    printf '%s' "$AM_COMPOSE_PROJECT"
    return
  fi
  if [[ "$1" == "macos-colima-krunkit-vulkan" ]]; then
    printf 'audio-minutes-gpu-eval'
  else
    printf 'audio-minutes'
  fi
}

compose_profile() {
  if [[ "$1" == "macos-colima-krunkit-vulkan" ]]; then printf 'gpu'; else printf 'cpu'; fi
}

compose() {
  local profile="$1" env_file
  shift
  env_file="$(profile_env "$profile")"
  [[ -f "$env_file" ]] || { echo "設定がありません: $env_file" >&2; return 2; }
  docker_for_profile "$profile" compose \
    --project-name "$(compose_project "$profile")" \
    --env-file "$env_file" \
    -f "$repo_root/deploy/compose.yml" \
    --profile "$(compose_profile "$profile")" \
    "$@"
}

host_kernel() {
  printf '%s' "${AM_HOST_KERNEL_OVERRIDE:-$(uname -s)}"
}

pending_dependency() {
  # 機械処理できる 1 行と、人が次に行う操作を対にして出す。
  printf 'PENDING code=%s component=%s action=%s\n' "$1" "$2" "$3" >&2
}

ensure_install_dependencies() {
  local profile="$1" assume_yes="$2" kernel tool answer
  local -a missing=() formulas=()
  kernel="$(host_kernel)"

  if [[ "$profile" == macos-* && "$kernel" != "Darwin" ]]; then
    pending_dependency wrong_os macos "run_on_supported_macos"
    return 2
  fi
  if [[ "$profile" == "linux-cpu" && "$kernel" != "Linux" ]]; then
    pending_dependency wrong_os linux "run_on_linux"
    return 2
  fi

  # macOS で Homebrew が既にある場合だけ、プロジェクトが管理できる依存を補う。
  # Xcode/Swift の導入や Linux package manager の操作は OS 全体へ影響するため自動化しない。
  if [[ "$kernel" == "Darwin" ]]; then
    if command -v docker >/dev/null 2>&1; then
      if ! docker compose version >/dev/null 2>&1; then
        missing+=(docker-compose); formulas+=(docker-compose)
      fi
    else
      missing+=(docker docker-compose); formulas+=(docker docker-compose)
    fi
    command -v colima >/dev/null 2>&1 || { missing+=(colima); formulas+=(colima); }
    command -v jq >/dev/null 2>&1 || { missing+=(jq); formulas+=(jq); }
    command -v node >/dev/null 2>&1 || { missing+=(node); formulas+=(node); }
    if (( ${#formulas[@]} > 0 )); then
      if ! command -v brew >/dev/null 2>&1; then
        pending_dependency homebrew_required "${missing[*]}" "install_homebrew_then_rerun"
        return 2
      fi
      echo "Homebrew で不足ツールを導入します: ${formulas[*]}"
      if (( ! assume_yes )); then
        read -r -p "brew install ${formulas[*]} を実行しますか？ [y/N] " answer
        [[ "$answer" == "y" || "$answer" == "Y" ]] || {
          pending_dependency consent_required "${missing[*]}" "rerun_with_yes_or_approve"
          return 2
        }
      fi
      brew install "${formulas[@]}"
    fi

    for tool in docker colima jq node npm; do
      command -v "$tool" >/dev/null 2>&1 || {
        pending_dependency dependency_missing "$tool" "rerun_after_homebrew_install"
        return 2
      }
    done
    docker compose version >/dev/null 2>&1 || {
      pending_dependency dependency_missing docker-compose "configure_docker_cli_plugin_then_rerun"
      return 2
    }
    if ! command -v swift >/dev/null 2>&1; then
      pending_dependency os_action_required swift "install_xcode_15_3_or_command_line_tools"
      return 2
    fi
  else
    # Linux server build は Node/Swift を container 内で完結させる。
    for tool in docker jq; do
      command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done
    if command -v docker >/dev/null 2>&1 && ! docker compose version >/dev/null 2>&1; then
      missing+=(docker-compose)
    fi
    if (( ${#missing[@]} > 0 )); then
      pending_dependency package_manager_required "${missing[*]}" "install_with_your_linux_package_manager"
      return 2
    fi
  fi

  for tool in git openssl curl; do
    command -v "$tool" >/dev/null 2>&1 || {
      pending_dependency os_action_required "$tool" "install_os_command_line_tools"
      return 2
    }
  done
}

ensure_colima_running() {
  local profile="$1" selected_colima context_before context_after profile_exists start_code=0
  local host_cpus host_memory_bytes host_memory_gib vm_cpus vm_memory
  local -a start_args
  [[ "$profile" == macos-* ]] || return 0
  selected_colima="$(colima_profile "$profile")"
  if colima status --profile "$selected_colima" >/dev/null 2>&1; then
    echo "既存の Colima profile を変更せず使用します: $selected_colima"
    return 0
  fi

  context_before="$(docker context show)"
  profile_exists=0
  if colima list --json 2>/dev/null | jq -s -e --arg name "$selected_colima" 'any(.[]; .name==$name)' >/dev/null; then
    profile_exists=1
  fi
  if (( profile_exists )); then
    echo "停止中の専用 Colima profile を既存設定のまま起動します: $selected_colima"
    start_args=(--profile "$selected_colima" --activate=false)
  else
    host_cpus="$(sysctl -n hw.logicalcpu)"
    host_memory_bytes="$(sysctl -n hw.memsize)"
    host_memory_gib=$((host_memory_bytes / 1024 / 1024 / 1024))
    vm_cpus=4; (( host_cpus < vm_cpus )) && vm_cpus="$host_cpus"
    vm_memory=8; (( host_memory_gib / 2 < vm_memory )) && vm_memory=$((host_memory_gib / 2))
    (( vm_cpus >= 2 && vm_memory >= 6 )) || {
      echo "専用 VM に必要な 2 CPU / 6 GiB を割り当てられません" >&2
      return 1
    }
    echo "専用 Colima profile を新規作成します: $selected_colima (${vm_cpus} CPU / ${vm_memory} GiB / 60 GiB)"
    start_args=(--profile "$selected_colima" --activate=false --cpu "$vm_cpus" --memory "$vm_memory" --disk 60)
  fi
  colima start "${start_args[@]}" || start_code=$?
  context_after="$(docker context show)"
  if [[ "$context_after" != "$context_before" ]]; then
    echo "Colima が global Docker context を変更したため元へ戻します: $context_before" >&2
    docker context use "$context_before" >/dev/null
    return 1
  fi
  echo "global Docker context を保持しました: $context_before"
  (( start_code == 0 )) || return "$start_code"
}

prepare_models() {
  local profile="$1"
  if compose "$profile" run --rm --no-deps -T --entrypoint python transcription-worker \
      -m transcription_worker.models verify --quiet \
    && compose "$profile" run --rm --no-deps -T --entrypoint python transcription-worker \
      -m transcription_worker.models status --quiet; then
    echo "固定モデルと offline smoke は準備済みです。既存 cache を保持します"
    return 0
  fi
  echo "固定 revision の CPU モデルを取得・再開します。取得時だけ配布元へ接続します"
  compose "$profile" run --rm --no-deps -T -e HF_HUB_OFFLINE=0 --entrypoint python transcription-worker \
    -m transcription_worker.models prefetch --online
  compose "$profile" run --rm --no-deps -T -e HF_HUB_OFFLINE=1 --entrypoint python transcription-worker \
    -m transcription_worker.models verify
  echo "offline で両モデルを load し、短い合成音声を推論します"
  compose "$profile" run --rm --no-deps -T -e HF_HUB_OFFLINE=1 --entrypoint python transcription-worker \
    -m transcription_worker.models smoke
}

install_client_for_profile() {
  local profile="$1" skip_client="$2"
  if [[ "$profile" == macos-* && "$skip_client" != "1" ]]; then
    "$repo_root/scripts/install-client.sh" --profile "$profile"
  fi
}

validate_log_policy() {
  local profile="$1" days rendered
  days="$(profile_value "$profile" AM_RETENTION_LOG_DAYS)"
  days="${days:-14}"
  rendered="$(compose "$profile" config --format json)"
  jq -e --arg days "$days" '
    (.services | to_entries | all(
      .value.logging.driver == "json-file" and
      .value.logging.options["max-size"] == "20m" and
      .value.logging.options["max-file"] == "7"
    )) and
    ([.services["minutes-api"], .services["minutes-worker"],
      .services["transcription-worker"], .services["transcription-worker-vulkan"]]
      | map(select(. != null))
      | all(.environment.AM_LOG_DIR == "/var/log/audio-minutes" and
            (.environment.AM_SHARED_GID | tostring) == "10100")) and
    ((.services["minutes-api"].environment.AM_RETENTION_LOG_DAYS | tostring) == $days) and
    ([.services["minutes-worker"], .services["transcription-worker"],
      .services["transcription-worker-vulkan"]]
      | map(select(. != null))
      | all(.environment.AM_RETENTION_LOG_DAYS == null))
  ' <<<"$rendered" >/dev/null
}

# 共有 volume (artifacts / logs / models) の GID。各イメージの am-shared と一致させる。
AM_SHARED_GID=10100

# 既存 volume の移行。API と worker は別 UID・同じ補助 GID で共有 volume を使うため、
# 旧イメージが worker 専用 UID で初期化した volume を共通 GID + setgid へ揃える。
# 冪等なので毎回の更新で実行してよい。資格情報 volume (claude-credentials) は対象外で、
# 共有 GID を与えず minutes-worker だけに mount したままにする。
#
# compose の service は cap_drop: [ALL] で chown できないため、同じイメージを
# docker run で root 実行し、対象 volume だけを mount する。
migrate_shared_volume_permissions() {
  local profile="$1" project image tag env_file volume configured_tag
  env_file="$(profile_env "$profile")"
  [[ -f "$env_file" ]] || { echo "設定がありません: $env_file" >&2; return 2; }
  project="$(compose_project "$profile")"
  configured_tag="$(grep -E '^AM_IMAGE_TAG=' "$env_file" | tail -1 | cut -d= -f2-)"
  tag="${AM_IMAGE_TAG:-$configured_tag}"
  image="audio-minutes/minutes-api:${tag:-0.1.0}"
  docker_for_profile "$profile" image inspect "$image" >/dev/null 2>&1 || {
    echo "移行をとばします (イメージ未 build): $image" >&2
    return 0
  }
  for volume in artifacts logs models; do
    docker_for_profile "$profile" volume inspect "${project}_${volume}" >/dev/null 2>&1 || continue
    docker_for_profile "$profile" run --rm --user 0:0 -v "${project}_${volume}:/data" "$image" sh -c '
      set -eu
      chgrp -R '"$AM_SHARED_GID"' /data
      chmod -R g+rwX /data
      find /data -type d -exec chmod g+s {} +
    ' >/dev/null
  done
}

# 空の logs volume をどの service が最初に初期化しても、3つの実 UID が同じ階層へ
# 作成でき、minutes-api が worker の期限切れログを削除できることを実コンテナで確認する。
# probe 共有 directory の mode はその所有者 (minutes-api) だけが設定し、各 service は
# 自分が作成した directory にだけ chmod する。別 UID 所有 directory の chmod は EPERM になる。
verify_shared_log_permissions() {
  local profile="$1" probe service status=0 initialized=0 cleanup_status=0
  probe=".permission-probe-$(openssl rand -hex 12)"

  # 一意な probe root を API UID が原子的に作る。mkdir -p にしないことで、
  # 衝突時に既存 directory を probe や cleanup の対象にしない。
  if compose "$profile" run --rm --no-deps -T \
    -e "AM_PERMISSION_PROBE=$probe" -e "AM_SHARED_GID=$AM_SHARED_GID" \
    --entrypoint sh minutes-api -c '
      set -eu
      root=/var/log/audio-minutes
      probe_root="$root/$AM_PERMISSION_PROBE"
      [ "$(stat -c %g "$root")" = "$AM_SHARED_GID" ]
      [ "$(stat -c %a "$root")" = 2775 ]
      umask 0002
      mkdir "$probe_root"
      chmod 2775 "$probe_root"
      [ "$(stat -c %g "$probe_root")" = "$AM_SHARED_GID" ]
      [ "$(stat -c %a "$probe_root")" = 2775 ]
    ' >/dev/null; then
    initialized=1
  else
    status=$?
  fi

  if (( status == 0 )); then
    for service in minutes-api transcription-worker minutes-worker; do
      if compose "$profile" run --rm --no-deps -T \
        -e "AM_PERMISSION_PROBE=$probe" -e "AM_PERMISSION_SERVICE=$service" \
        -e "AM_SHARED_GID=$AM_SHARED_GID" --entrypoint sh "$service" -c '
          set -eu
          probe_root="/var/log/audio-minutes/$AM_PERMISSION_PROBE"
          service_root="$probe_root/$AM_PERMISSION_SERVICE"
          nested="$service_root/nested"
          umask 0002
          mkdir "$service_root"
          chmod 2775 "$service_root"
          mkdir "$nested"
          chmod 2775 "$nested"
          for directory in "$service_root" "$nested"; do
            [ "$(stat -c %g "$directory")" = "$AM_SHARED_GID" ]
            mode="$(stat -c %a "$directory")"
            [ "$mode" = 2775 ] || { echo "サービスログディレクトリの mode が不正です: $directory ($mode)" >&2; exit 1; }
          done
          printf "%s\n" "$AM_PERMISSION_SERVICE" >"$nested/active.log"
          chmod 0664 "$nested/active.log"
          [ "$(cat "$nested/active.log")" = "$AM_PERMISSION_SERVICE" ]
          [ "$(stat -c %g "$nested/active.log")" = "$AM_SHARED_GID" ]
          [ "$(stat -c %a "$nested/active.log")" = 664 ]
          if [ "$AM_PERMISSION_SERVICE" != minutes-api ]; then
            printf "%s\n" "$AM_PERMISSION_SERVICE" >"$nested/retention-expired.log"
            chmod 0664 "$nested/retention-expired.log"
            touch -t 200001010000 "$nested/retention-expired.log"
          fi
        ' >/dev/null; then
        :
      else
        status=$?
        break
      fi
    done
  fi

  # API の保持期限ロジックを一意な probe root に限定し、worker UID が作った
  # 期限切れ archive だけを API UID で削除できることを確認する。既存ログは走査しない。
  if (( status == 0 )); then
    if compose "$profile" run --rm --no-deps -T \
      -e "AM_LOG_DIR=/var/log/audio-minutes/$probe" \
      --entrypoint python minutes-api -c '
from datetime import datetime, timezone
from minutes_api.config import get_settings
from minutes_api.retention import RetentionPolicy, sweep_log_files
root = get_settings().log_dir
removed = sweep_log_files(None, RetentionPolicy(upload_hours=24, audio_days=30, log_days=1), at=datetime.now(timezone.utc))
assert removed == 2, f"probeの期限切れログ削除数が不正です: {removed}"
for service in ("minutes-api", "transcription-worker", "minutes-worker"):
    assert (root / service / "nested" / "active.log").read_text().strip() == service
for service in ("transcription-worker", "minutes-worker"):
    assert not (root / service / "nested" / "retention-expired.log").exists()
      ' >/dev/null; then
      :
    else
      status=$?
    fi
  fi

  # 初期化に成功した場合だけ、API が所有する一意な probe root を必ず片付ける。
  if (( initialized )); then
    if compose "$profile" run --rm --no-deps -T \
      -e "AM_PERMISSION_PROBE=$probe" --entrypoint sh minutes-api -c '
        set -eu
        target="/var/log/audio-minutes/$AM_PERMISSION_PROBE"
        case "$target" in
          /var/log/audio-minutes/.permission-probe-*) ;;
          *) echo "probe cleanup 対象が不正です: $target" >&2; exit 2 ;;
        esac
        rm -rf -- "$target"
        test ! -e "$target"
      ' >/dev/null; then
      :
    else
      cleanup_status=$?
      (( status != 0 )) || status=$cleanup_status
    fi
  fi
  (( status == 0 )) || return "$status"
  echo "共有 logs volume: 3 UID の作成と API UID の削除を確認しました"
}

set_update_maintenance() {
  local profile="$1" state="$2"
  case "$state" in
    enable)
      compose "$profile" run --rm --no-deps -T --entrypoint sh minutes-api -c '
        set -eu
        marker=/var/log/audio-minutes/.update-maintenance
        umask 0002
        : >"$marker"
        chgrp "${AM_SHARED_GID:-10100}" "$marker"
        chmod 0664 "$marker"
      ' >/dev/null
      ;;
    disable)
      compose "$profile" run --rm --no-deps -T --entrypoint sh minutes-api -c \
        'rm -f -- /var/log/audio-minutes/.update-maintenance' >/dev/null
      ;;
    *) echo "maintenance state が不正です: $state" >&2; return 2 ;;
  esac
}
