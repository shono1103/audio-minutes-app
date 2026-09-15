"""子プロセスの stdout/stderr を Docker と日次ファイルの両方へ流す。

保持日数の正本は DB の owner 設定であり、期限切れファイルは minutes-api の
retention sweep だけが削除する。この runner は秘密を解釈せず byte 列のまま転送する。
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


SHARED_GID = int(os.environ.get("AM_SHARED_GID", "10100"))
DIRECTORY_MODE = 0o2775
FILE_MODE = 0o664


def _shared_directory(path: Path) -> None:
    """共有 GID を継承する setgid/group-write のディレクトリを冪等に作る。"""
    created = False
    try:
        path.mkdir(mode=DIRECTORY_MODE)
        created = True
    except FileExistsError:
        pass
    if created:
        os.chown(path, -1, SHARED_GID)
        os.chmod(path, DIRECTORY_MODE)
    stat = path.stat()
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError(f"共有ログパスがディレクトリではありません: {path}")
    if stat.st_gid != SHARED_GID or stat.st_mode & DIRECTORY_MODE != DIRECTORY_MODE:
        raise PermissionError(
            f"共有ログディレクトリの権限が不正です: {path} "
            f"gid={stat.st_gid} mode={stat.st_mode & 0o7777:o}; "
            "scripts/install.sh または scripts/update.sh で権限移行を実行してください"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("-- の後に command が必要です")
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.service) is None:
        raise ValueError("service は英小文字・数字・ハイフンだけで指定してください")
    log_root = Path(os.environ.get("AM_LOG_DIR", "/var/log/audio-minutes"))
    stdout_root = log_root / "stdout"
    directory = stdout_root / args.service
    # 起動順に関係なく別 UID の service が次の階層を作れるよう、全階層を検査する。
    for shared in (log_root, stdout_root, directory):
        _shared_directory(shared)

    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def forward(signum: int, _frame: object) -> None:
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    assert child.stdout is not None
    current_day = ""
    log_handle = None
    try:
        for line in iter(child.stdout.readline, b""):
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            if day != current_day:
                if log_handle is not None:
                    log_handle.close()
                path = directory / f"{day}.log"
                log_handle = path.open("ab")
                os.chown(path, -1, SHARED_GID)
                os.chmod(path, FILE_MODE)
                current_day = day
            log_handle.write(line)
            log_handle.flush()
    finally:
        if log_handle is not None:
            log_handle.close()
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
