#!/usr/bin/env bash
# 停止中の配置へ backup を復元する。明示フラグなしでは実行しない。
# backup.sh が manifest.txt を書いた完成 backup だけを受け付け、復元後に
# DB が参照する artifact の存在と sha256 を検証する。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"

profile="macos-colima-cpu"
confirmed=0
if [[ "${1:-}" == "--profile" ]]; then profile="${2:-}"; shift 2; fi
source_dir="${1:-}"; shift || true
[[ "${1:-}" == "--confirm-overwrite" ]] && confirmed=1
[[ -n "$source_dir" && -f "$source_dir/database.dump" && -f "$source_dir/artifacts.tar.gz" ]] || {
  echo "usage: $0 [--profile PROFILE] BACKUP_DIR --confirm-overwrite" >&2; exit 2;
}
[[ -f "$source_dir/manifest.txt" ]] || {
  echo "manifest.txt がありません。未完成の backup は復元できません: $source_dir" >&2; exit 2;
}
(( confirmed )) || { echo "復元先の DB・artifacts を上書きするため --confirm-overwrite が必要です" >&2; exit 2; }
validate_profile "$profile"
source_dir="$(cd "$source_dir" && pwd -P)"

compose "$profile" stop minutes-api transcription-worker transcription-worker-vulkan minutes-worker 2>/dev/null || true
migrate_shared_volume_permissions "$profile"
compose "$profile" exec -T db pg_restore -U am_api -d audio_minutes --clean --if-exists <"$source_dir/database.dump"

# 既存の成果物を消してから、API 所有の一時ディレクトリへ展開して中身だけ移す。
# archive の `.` を mount 点へ直接展開すると、非 root の API user が root 所有の
# mount 点へ chmod / utime しようとして失敗するため、mount 点自体には触れない。
compose "$profile" run --rm --no-deps -T --entrypoint sh minutes-api -c '
  set -eu
  cd /var/lib/audio-minutes/artifacts
  find . -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  mkdir .restore
  tar -xz --no-same-owner -C .restore
  find .restore -mindepth 1 -maxdepth 1 -exec mv -t . -- {} +
  rmdir .restore
' <"$source_dir/artifacts.tar.gz"

if [[ -f "$source_dir/artifacts.manifest.tsv" ]]; then
  compose "$profile" run --rm --no-deps -T --entrypoint sh minutes-api -c '
    set -eu
    cd /var/lib/audio-minutes/artifacts
    bad=0
    while IFS="$(printf "\t")" read -r id sha size; do
      [ -n "$id" ] || continue
      path="$(printf "%s" "$id" | cut -c5-6)/$id"
      if [ ! -f "$path" ]; then echo "artifact がありません: $id" >&2; bad=$((bad + 1)); continue; fi
      if [ "$(sha256sum "$path" | cut -d" " -f1)" != "$sha" ]; then
        echo "sha256 が一致しません: $id" >&2; bad=$((bad + 1))
      fi
    done
    [ "$bad" -eq 0 ] || { echo "復元後の検証に失敗しました ($bad 件)" >&2; exit 1; }
  ' <"$source_dir/artifacts.manifest.tsv" || {
    echo "復元後の検証に失敗しました。サービスは起動していません: $source_dir" >&2
    exit 1
  }
fi

compose "$profile" up -d
echo "restore: 完了。service.sh status と既知音声で確認してください"
