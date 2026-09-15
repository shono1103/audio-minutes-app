#!/usr/bin/env bash
# DB と非機密成果物を、同じ整合点で新規ディレクトリへ保存する。Claude 資格情報は対象外。
#
# 整合性 (R13): 書き込み側サービス (API・worker) を止めてから DB dump と artifacts を取り、
# DB が参照する artifact の存在と sha256 を検証してからだけ完成扱いにする。途中で失敗した
# backup は `.partial` のまま残し、manifest.txt を書かないので restore の対象にならない。
#
# 権限 (R12): コンテナ側へホストのディレクトリを mount せず、tar を標準出力で受け取る。
# コンテナは非 root の専用 UID で動くため、ホスト 0700 のディレクトリへは書けない。
set -euo pipefail
umask 077
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"

profile="macos-colima-cpu"
destination=""
leave_stopped=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) profile="${2:-}"; shift 2 ;;
    --leave-stopped) leave_stopped=1; shift ;;
    --*) echo "usage: $0 [--profile PROFILE] [--leave-stopped] DESTINATION" >&2; exit 2 ;;
    *)
      [[ -z "$destination" ]] || { echo "保存先は1つだけ指定してください" >&2; exit 2; }
      destination="$1"; shift
      ;;
  esac
done
[[ -n "$destination" ]] || { echo "usage: $0 [--profile PROFILE] [--leave-stopped] DESTINATION" >&2; exit 2; }
validate_profile "$profile"
[[ ! -e "$destination" ]] || { echo "既存の保存先は上書きしません: $destination" >&2; exit 1; }

write_services=(minutes-api transcription-worker transcription-worker-vulkan minutes-worker)
started_state_restored=0

restore_services() {
  (( started_state_restored )) && return 0
  started_state_restored=1
  echo "サービスを再開します"
  compose "$profile" up -d >/dev/null || {
    echo "サービスの再開に失敗しました。service.sh status で確認してください" >&2
    return 1
  }
}

fail() {
  echo "backup に失敗しました: $1" >&2
  echo "未完成の backup を残しています (manifest.txt なし): $partial" >&2
  restore_services || true
  exit 1
}

mkdir -p "$destination"
destination="$(cd "$destination" && pwd -P)"
partial="$destination/.partial"
mkdir -p "$partial"

# 実行中 job はここで lease ごと中断する。lease 期限切れ後に再取得されるため、
# backup 後に該当セッションの job 状態を確認する。
echo "書き込み側サービスを停止します: ${write_services[*]}"
trap 'restore_services || true' EXIT
compose "$profile" stop "${write_services[@]}" >/dev/null \
  || fail "書き込み側サービスの停止に失敗しました"

# stop が成功を返しても、再起動中や停止未反映の container が残っていれば
# DB dump と artifacts の整合点にならない。対象 service の稼働状態を別 command で確認する。
running_services="$(
  compose "$profile" ps --services --status running --status restarting "${write_services[@]}"
)" || fail "書き込み側サービスの停止状態を確認できませんでした"
[[ -z "$running_services" ]] || fail "書き込み側サービスが稼働中です: $(tr '\n' ' ' <<<"$running_services")"

compose "$profile" exec -T db pg_dump -U am_api -d audio_minutes -Fc >"$partial/database.dump" \
  || fail "pg_dump に失敗しました"

# DB が参照する artifact の一覧 (削除済みは除く) を取り、同じ停止状態のまま突き合わせる。
compose "$profile" exec -T db psql -U am_api -d audio_minutes --no-align --tuples-only --quiet \
  --field-separator=$'\t' -c "SELECT id, sha256, byte_size FROM artifacts WHERE deleted_at IS NULL ORDER BY id" \
  >"$partial/artifacts.manifest.tsv" || fail "artifact 一覧の取得に失敗しました"

compose "$profile" run --rm --no-deps -T --entrypoint sh minutes-api -c '
  set -eu
  cd /var/lib/audio-minutes/artifacts
  missing=0
  while IFS="$(printf "\t")" read -r id sha size; do
    [ -n "$id" ] || continue
    path="$(printf "%s" "$id" | cut -c5-6)/$id"
    if [ ! -f "$path" ]; then
      echo "artifact がありません: $id" >&2
      missing=$((missing + 1))
      continue
    fi
    actual_size="$(wc -c <"$path" | tr -d " ")"
    if [ "$actual_size" != "$size" ]; then
      echo "byte_size が一致しません: $id ($actual_size != $size)" >&2
      missing=$((missing + 1))
      continue
    fi
    actual_sha="$(sha256sum "$path" | cut -d" " -f1)"
    if [ "$actual_sha" != "$sha" ]; then
      echo "sha256 が一致しません: $id" >&2
      missing=$((missing + 1))
    fi
  done
  [ "$missing" -eq 0 ] || { echo "DB が参照する artifact を $missing 件検証できませんでした" >&2; exit 1; }
' <"$partial/artifacts.manifest.tsv" || fail "DB と artifacts の整合性検証に失敗しました"

# ディレクトリ自体ではなく中身を固める。restore 側で mount 点の所有者を変えないため。
compose "$profile" run --rm --no-deps -T --entrypoint sh minutes-api -c \
  'cd /var/lib/audio-minutes/artifacts && tar -cz .' >"$partial/artifacts.tar.gz" \
  || fail "artifacts の取得に失敗しました"

mv "$partial/database.dump" "$partial/artifacts.tar.gz" "$partial/artifacts.manifest.tsv" "$destination/"
rmdir "$partial"
printf '%s\n' \
  "profile=$profile" \
  "created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "artifact_count=$(wc -l <"$destination/artifacts.manifest.tsv" | tr -d ' ')" \
  "services_stopped=${write_services[*]}" \
  >"$destination/manifest.txt"

trap - EXIT
if (( leave_stopped )); then
  echo "backup: $destination (write services は停止したままです)"
else
  restore_services
  echo "backup: $destination"
fi
