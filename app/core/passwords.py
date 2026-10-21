"""Argon2id at the raw-password boundary, with bounded work outside the event loop."""

import asyncio
import base64
import binascii
import re
import threading
from collections.abc import Callable
from typing import TypeVar

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from app.services.errors import ServiceError

MAX_PASSWORD_BYTES = 1024

# This is a public, deliberately unrelated password, not an account credential.
# Precomputation avoids blocking application startup with Argon2 work.
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$WZZMQBU3VW1YAn+Ia6chdA$"
    "EWL+vwpYz6kWt6OyMm0jzY8n1V9BcxDkrNM03zydT7s"
)
_ENCODED_HASH = re.compile(
    r"\$argon2id\$v=19\$m=([0-9]{1,9}),t=([0-9]{1,9}),p=([0-9]{1,9})"
    r"\$([A-Za-z0-9+/]+)\$([A-Za-z0-9+/]+)"
)
_Result = TypeVar("_Result")


def _valid_password(password: str) -> bool:
    if not password or len(password) > MAX_PASSWORD_BYTES:
        return False
    try:
        return len(password.encode("utf-8")) <= MAX_PASSWORD_BYTES
    except UnicodeEncodeError:
        return False


def _safe_hash(encoded: str | None) -> bool:
    """Reject malformed/corrupt cost fields before invoking the native verifier."""
    if encoded is None or len(encoded) > 512:
        return False
    match = _ENCODED_HASH.fullmatch(encoded)
    if match is None:
        return False
    memory, iterations, parallelism = map(int, match.group(1, 2, 3))
    # Allows normal Argon2id parameter upgrades without allowing corrupt values
    # to request unlimited RAM/CPU. The current recommendation is 65536/3/4.
    if not (1 <= parallelism <= 8 and 1 <= iterations <= 10):
        return False
    if not 8 * parallelism <= memory <= 262144:
        return False
    for raw, minimum in ((match[4], 8), (match[5], 16)):
        try:
            decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
        except (ValueError, binascii.Error):
            return False
        if not minimum <= len(decoded) <= 64:
            return False
        if base64.b64encode(decoded).decode("ascii").rstrip("=") != raw:
            return False
    return True


class PasswordService:
    """One shared instance per application; never normalize or log passwords.

    ``hash`` is for trusted account provisioning/password changes and raises
    ValueError for empty, invalid UTF-8, or over-1024-byte input. ``verify``
    returns False for those inputs and for missing/malformed stored hashes.
    Neither method applies a registration password-strength policy.

    At most ``max_concurrency`` operations run per instance. Additional calls
    fail with a retryable 503 instead of creating an unbounded hashing queue.
    """

    def __init__(self, *, max_concurrency: int = 2):
        if max_concurrency < 1:
            raise ValueError("Password concurrency must be positive")
        self._hasher = PasswordHash.recommended()
        self._admission = threading.BoundedSemaphore(max_concurrency)

    async def _run(self, operation: Callable[[], _Result]) -> _Result:
        if not self._admission.acquire(blocking=False):
            raise ServiceError("PASSWORD_SERVICE_BUSY", "登入服務忙碌，請稍後再試。", 503) from None
        try:
            future = asyncio.get_running_loop().run_in_executor(None, operation)
        except BaseException:
            # Submission failed before a worker could own this admission slot.
            self._admission.release()
            raise

        def completed(result: asyncio.Future[_Result]) -> None:
            # Native work continues when an HTTP request is cancelled. Only its
            # completion releases capacity, so cancellation cannot bypass the
            # bound or enqueue more than max_concurrency submitted operations.
            self._admission.release()
            if not result.cancelled():
                # A cancelled caller no longer retrieves worker exceptions.
                # Observe them without logging password-containing details.
                result.exception()

        future.add_done_callback(completed)
        return await asyncio.shield(future)

    async def hash(self, password: str) -> str:
        if not _valid_password(password):
            raise ValueError("Password must contain between 1 and 1024 UTF-8 bytes")
        return await self._run(lambda: self._hasher.hash(password))

    async def verify(self, password: str, encoded: str | None) -> bool:
        if not _valid_password(password):
            return False

        def verify_password() -> bool:
            usable = _safe_hash(encoded)
            candidate = encoded if usable and encoded is not None else _DUMMY_HASH
            try:
                valid = self._hasher.verify(password, candidate)
            except UnknownHashError:
                # Fail closed if a library update rejects an otherwise parsed
                # hash, while retaining the expensive unknown-account path.
                self._hasher.verify(password, _DUMMY_HASH)
                return False
            return usable and valid

        return await self._run(verify_password)
