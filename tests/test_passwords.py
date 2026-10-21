import asyncio
import threading

import pytest

from app.core import passwords
from app.core.passwords import PasswordService
from app.services.errors import ServiceError


async def test_argon2id_random_salts_and_exact_password_matching():
    service = PasswordService()
    raw = " 台股密碼 e\u0301 "
    first = await service.hash(raw)
    second = await service.hash(raw)
    assert first.startswith("$argon2id$")
    assert first != second
    assert await service.verify(raw, first)
    assert await service.verify(raw, second)
    assert not await service.verify(raw.strip(), first)
    assert not await service.verify(" 台股密碼 é ", first)
    assert not await service.verify("wrong password", first)


@pytest.mark.parametrize("raw", ["", "x" * 1025, "股" * 342, "\ud800"])
async def test_invalid_password_input_never_reaches_hasher(monkeypatch, raw):
    service = PasswordService()

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid input must not consume a worker")

    monkeypatch.setattr(service._hasher, "hash", unexpected)
    monkeypatch.setattr(service._hasher, "verify", unexpected)
    with pytest.raises(ValueError, match="UTF-8"):
        await service.hash(raw)
    assert not await service.verify(raw, passwords._DUMMY_HASH)


async def test_byte_limit_accepts_exact_boundary():
    service = PasswordService()
    raw = "股" * 341 + "x"
    encoded = await service.hash(raw)
    assert await service.verify(raw, encoded)


@pytest.mark.parametrize(
    "encoded",
    [
        None,
        "",
        "plain-text-password",
        "$argon2id$invalid",
        passwords._DUMMY_HASH.replace("v=19", "v=999"),
        passwords._DUMMY_HASH.replace("m=65536", "m=999999999"),
        passwords._DUMMY_HASH.replace("t=3", "t=999999999"),
        passwords._DUMMY_HASH.replace("p=4", "p=999999999"),
        passwords._DUMMY_HASH.replace("m=65536", "m=1"),
        passwords._DUMMY_HASH.replace("WZZMQBU3VW1YAn+Ia6chdA", "bad!base64"),
        passwords._DUMMY_HASH.replace("WZZMQBU3VW1YAn+Ia6chdA", "a"),
        passwords._DUMMY_HASH.replace("WZZMQBU3VW1YAn+Ia6chdA", "AAAA"),
        passwords._DUMMY_HASH.replace("WZZMQBU3VW1YAn+Ia6chdA", "WZZMQBU3VW1YAn+Ia6chdB"),
    ],
)
async def test_unknown_or_malformed_hash_uses_dummy_but_cannot_authenticate(monkeypatch, encoded):
    service = PasswordService()
    seen = []

    def verify(raw, candidate):
        seen.append(candidate)
        return True

    monkeypatch.setattr(service._hasher, "verify", verify)
    assert not await service.verify("a valid raw password", encoded)
    assert seen == [passwords._DUMMY_HASH]


async def test_dummy_password_itself_never_authenticates_an_unknown_user():
    service = PasswordService()
    assert not await service.verify("not-an-account-password-for-dummy-verification", None)
    assert not await service.verify(
        "not-an-account-password-for-dummy-verification", "$argon2id$bad"
    )


async def test_worker_is_off_loop_and_overload_is_bounded(monkeypatch):
    service = PasswordService(max_concurrency=1)
    entered, finish = threading.Event(), threading.Event()
    event_loop_thread = threading.get_ident()
    worker_threads = []

    def slow_hash(raw):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert finish.wait(timeout=5)
        return "encoded"

    monkeypatch.setattr(service._hasher, "hash", slow_hash)
    operation = asyncio.create_task(service.hash("private password"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # Running this continuation proves that the loop remains available.
        assert worker_threads == [worker_threads[0]]
        assert worker_threads[0] != event_loop_thread
        with pytest.raises(ServiceError) as caught:
            await service.verify("other private password", None)
        assert caught.value.code == "PASSWORD_SERVICE_BUSY"
        assert caught.value.status_code == 503
        assert "private password" not in str(caught.value)
    finally:
        finish.set()
        assert await operation == "encoded"
    assert await service.hash("next password") == "encoded"


async def test_failed_worker_releases_its_capacity(monkeypatch):
    service = PasswordService(max_concurrency=1)

    def broken_hash(raw):
        raise RuntimeError("worker failure")

    monkeypatch.setattr(service._hasher, "hash", broken_hash)
    with pytest.raises(RuntimeError, match="worker failure"):
        await service.hash("a valid password")
    monkeypatch.setattr(service._hasher, "hash", lambda raw: "encoded")
    assert await service.hash("a valid password") == "encoded"


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_cancelled_request_holds_capacity_until_worker_finishes(monkeypatch, worker_fails):
    service = PasswordService(max_concurrency=1)
    entered, finish, exited = threading.Event(), threading.Event(), threading.Event()

    def slow_hash(raw):
        if raw != "first password":
            return "next encoded"
        entered.set()
        try:
            assert finish.wait(timeout=5)
            if worker_fails:
                raise RuntimeError("worker failure after request cancellation")
            return "first encoded"
        finally:
            exited.set()

    monkeypatch.setattr(service._hasher, "hash", slow_hash)
    request = asyncio.create_task(service.hash("first password"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        # The request is gone, but native Argon2 work cannot be cancelled. A new
        # request must still be rejected while that work occupies the only slot.
        with pytest.raises(ServiceError) as caught:
            await service.hash("second password")
        assert caught.value.code == "PASSWORD_SERVICE_BUSY"
    finally:
        finish.set()
        assert await asyncio.to_thread(exited.wait, 2)
    # Let the event loop process the native future's completion callback.
    async with asyncio.timeout(2):
        while True:
            try:
                assert await service.hash("third password") == "next encoded"
                break
            except ServiceError as error:
                assert error.code == "PASSWORD_SERVICE_BUSY"
                await asyncio.sleep(0)
