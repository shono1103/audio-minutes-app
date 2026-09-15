"""FastAPI schema を契約ディレクトリへ出力する。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from minutes_api.app import create_app


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("../../contracts/api/openapi.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
