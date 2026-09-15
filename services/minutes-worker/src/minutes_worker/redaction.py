"""ログから URL・メールアドレス・token 風文字列を除去する。認証 URL はログへ出さない (FR-124)。"""

from __future__ import annotations

import logging
import re

_URL = re.compile(r"https?://[^\s'\"<>]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOKEN = re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{8,}|oauth_[A-Za-z0-9_-]{8,})\b")


def redact(text: str) -> str:
    text = _URL.sub("<redacted-url>", text)
    text = _EMAIL.sub("<redacted-email>", text)
    return _TOKEN.sub("<redacted-token>", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        record.msg = redact(message)
        record.args = ()
        return True


def install_redaction(logger: logging.Logger | None = None) -> None:
    target = logger or logging.getLogger()
    if not any(isinstance(existing, RedactingFilter) for existing in target.filters):
        target.addFilter(RedactingFilter())
    for handler in target.handlers:
        if not any(isinstance(existing, RedactingFilter) for existing in handler.filters):
            handler.addFilter(RedactingFilter())
