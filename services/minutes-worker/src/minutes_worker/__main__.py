"""エントリーポイント: 内部制御 API (別スレッド) とジョブループ (メインスレッド) を起動する。"""

from __future__ import annotations

import logging
import os
import signal
import threading

import uvicorn

from minutes_worker.claude_auth import ClaudeAuthAdapter
from minutes_worker.config import WorkerConfig
from minutes_worker.control_api import create_app
from minutes_worker.redaction import install_redaction
from minutes_worker.runner import build_runner


def main() -> None:
    config = WorkerConfig()
    logging.basicConfig(level=config.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_redaction(logging.getLogger())
    logging.getLogger("uvicorn.access").disabled = True  # パスやクエリに秘密が乗らないよう access log を出さない

    extra_env = {"AM_MOCK_CLAUDE_SCENARIO": os.environ.get("AM_MOCK_CLAUDE_SCENARIO", "")} if config.claude_mock else {}
    auth = ClaudeAuthAdapter(
        config.resolved_claude_bin(),
        config.claude_home,
        login_timeout_seconds=config.login_timeout_seconds,
        extra_env=extra_env,
    )
    runner = build_runner(config, auth=auth)
    app = create_app(auth, config.internal_token)
    host, _, port = config.internal_bind.rpartition(":")
    server = uvicorn.Server(uvicorn.Config(app, host=host or "0.0.0.0", port=int(port), log_level="warning", access_log=False))
    api_thread = threading.Thread(target=server.run, daemon=True, name="control-api")
    api_thread.start()

    def shutdown(*_: object) -> None:
        runner.stop()
        server.should_exit = True
        auth.close()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    runner.run_forever()


if __name__ == "__main__":
    main()
