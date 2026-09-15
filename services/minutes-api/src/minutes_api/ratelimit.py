"""メモリ内の固定窓レート制限。ログイン・招待・bootstrap の総当たりを抑える。"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from minutes_api.errors import ApiException


class RateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_seconds: int) -> None:
        now = time.monotonic()
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] < now - window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                raise ApiException(
                    429,
                    "rate_limited",
                    "試行回数が多すぎます。しばらく待ってから再試行してください",
                    headers={"Retry-After": str(window_seconds)},
                )
            bucket.append(now)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


limiter = RateLimiter()


def client_key(request_host: str | None, scope: str) -> str:
    return f"{scope}:{request_host or 'unknown'}"
