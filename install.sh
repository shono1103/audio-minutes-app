#!/usr/bin/env bash
# 利用者向けの薄い導入入口。実体と再開案内は scripts/install.sh に集約する。
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec "$repo_root/scripts/install.sh" "$@"
