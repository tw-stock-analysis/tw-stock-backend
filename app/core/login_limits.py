"""Bounded login attempt counters for one application process.

All admitted attempts count, including successful logins. IP and normalized email
have independent fixed windows; rejected attempts never extend a window. Counters
contain SHA-256 digests rather than raw addresses, and expire using monotonic time.

These counters reset on process restart and are not shared across workers or
replicas. A public deployment with multiple processes also needs a shared limit
at its trusted edge (or a separately designed shared rate-limit store).
"""

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256

from app.services.errors import ServiceError


@dataclass
class _Bucket:
    expires_at: float
    count: int = 0


class LoginRateLimiter:
    def __init__(
        self,
        *,
        ip_limit: int = 30,
        email_limit: int = 10,
        window_seconds: float = 60,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ip_limit < 1 or email_limit < 1 or max_keys < 1:
            raise ValueError("Login limits and capacity must be positive")
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("Login window must be finite and positive")
        self.ip_limit = ip_limit
        self.email_limit = email_limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: OrderedDict[bytes, _Bucket] = OrderedDict()
        self._last_now = -math.inf

    def check_ip(self, ip: str) -> None:
        """Count a connection's trusted client IP, not an arbitrary HTTP header."""
        self._check(b"ip:", ip, self.ip_limit)

    def check_email(self, normalized_email: str) -> None:
        """Normalize defensively to match account lookup before counting."""
        self._check(b"email:", normalized_email.strip().lower(), self.email_limit)

    def _check(self, namespace: bytes, value: str, limit: int) -> None:
        key = sha256(namespace + value.encode("utf-8")).digest()
        with self._lock:
            # Real monotonic time cannot regress. Clamping also keeps expiration
            # ordering valid when a custom test/embedding clock moves backwards.
            now = max(self._clock(), self._last_now)
            self._last_now = now
            # Insertion and expiry order are identical. Work and storage are
            # bounded by max_keys; live entries are never evicted to admit new keys.
            while self._buckets:
                oldest = next(iter(self._buckets))
                if self._buckets[oldest].expires_at > now:
                    break
                self._buckets.popitem(last=False)

            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_keys:
                    raise ServiceError(
                        "AUTH_CAPACITY_EXCEEDED", "登入服務忙碌中，請稍後再試。", 503
                    )
                bucket = _Bucket(expires_at=now + self.window_seconds)
                self._buckets[key] = bucket
            if bucket.count >= limit:
                raise ServiceError("LOGIN_RATE_LIMITED", "登入嘗試過於頻繁，請稍後再試。", 429)
            bucket.count += 1
