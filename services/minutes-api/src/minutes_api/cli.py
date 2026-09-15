"""DB を伴う管理操作。秘密の token は標準出力へ一度だけ返す。"""

from __future__ import annotations

import argparse

from minutes_api.accounts import issue_bootstrap_token
from minutes_api.config import get_settings
from minutes_api.db import session_scope


def main() -> None:
    parser = argparse.ArgumentParser(prog="minutes-api-admin")
    parser.add_argument("command", choices=["bootstrap"])
    args = parser.parse_args()
    if args.command == "bootstrap":
        with session_scope() as db:
            token, expires_at = issue_bootstrap_token(db)
        print(f"{get_settings().public_base_url}/auth/bootstrap/{token}")
        print(f"expires_at={expires_at.isoformat()}")
