"""Actual PostgreSQL transactions; synthetic TWSE transport, never simulated DB locks."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.models import AuthSession, User, VerificationToken, Watchlist, WatchlistItem
from app.services import accounts as account_module
from app.services.accounts import AccountService
from app.services.auth_cleanup import cleanup_credentials
from app.services.credentials import SessionService, VerificationService, digest_token
from app.services.errors import ServiceError
from app.services.stocks import StockService
from app.services.twse import TwseService
from app.services.watchlists import WatchlistService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Requires migrated PostgreSQL"),
]


@pytest_asyncio.fixture
async def source():
    state = SimpleNamespace(
        status=200, codes=["0050", "2330", "00632R", "2000", "2001"], calls=[], before_reply=None
    )

    async def handler(request):
        assert request.url.path == "/v1/exchangeReport/STOCK_DAY_ALL"
        assert request.headers["cache-control"] == "no-cache"
        state.calls.append(request)
        if state.before_reply:
            callback, state.before_reply = state.before_reply, None
            await callback()
        return httpx.Response(
            state.status, json=[{"Code": code, "Name": f"測試{code}"} for code in state.codes]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        state.stocks = StockService(TwseService(client))
        yield state


async def outcomes(*operations):
    return await asyncio.wait_for(asyncio.gather(*operations, return_exceptions=True), timeout=10)


def assert_error(error, code):
    assert isinstance(error, ServiceError), error
    assert error.code == code


async def test_email_re_registration_never_inherits_old_identity(service_db, source):
    accounts = AccountService(service_db.factory)
    owner = await service_db.user()
    lists = WatchlistService(service_db.factory, source.stocks)
    old_list = await lists.create(owner.id, "私人清單")
    session_service = SessionService(service_db.factory)
    login = await session_service.issue(owner.id, timedelta(days=1))
    reset = await VerificationService(service_db.factory).issue(
        owner.id, "reset_password", timedelta(minutes=30)
    )
    await accounts.soft_delete(owner.id)
    await accounts.soft_delete(owner.id)  # Idempotent.
    fresh_id = await accounts.create(owner.email.upper(), password_hash="$argon2id$new-hash")
    assert fresh_id != owner.id
    async with service_db.factory() as session:
        old = await session.get(User, owner.id)
        fresh = await session.get(User, fresh_id)
        assert old.deleted_at is not None and old.status == "disabled"
        assert fresh.deleted_at is None and fresh.status == "pending_verification"
        assert (
            await session.scalar(select(Watchlist.id).where(Watchlist.user_id == fresh_id)) is None
        )
        assert await session.get(Watchlist, old_list) is not None
        assert (await session.get(AuthSession, login.id)).revoked_at is not None
        assert (await session.get(VerificationToken, reset.id)).revoked_at is not None
    with pytest.raises(ServiceError, match="憑證"):
        await accounts.reset_password(reset.value, password_hash="$argon2id$another-hash")
    with pytest.raises(ServiceError) as caught:
        await session_service.authenticate(login.value)
    assert caught.value.code == "SESSION_INVALID"


async def test_concurrent_account_creation_has_one_winner(service_db):
    accounts = AccountService(service_db.factory)
    email = service_db.email()
    results = await outcomes(
        accounts.create(email, password_hash="$argon2id$a"),
        accounts.create(email.upper(), password_hash="$argon2id$b"),
    )
    errors = [item for item in results if isinstance(item, Exception)]
    assert len(errors) == 1
    assert_error(errors[0], "EMAIL_IN_USE")


async def test_case_insensitive_list_name_is_unique_per_user(service_db, source):
    owner, another = await service_db.user(), await service_db.user()
    service = WatchlistService(service_db.factory, source.stocks)
    target = await service.create(owner.id, " ETF ")
    await service.create(another.id, "etf")
    with pytest.raises(ServiceError) as caught:
        await service.create(owner.id, "etf")
    assert caught.value.code == "WATCHLIST_NAME_EXISTS"
    second = await service.create(owner.id, "長期")
    with pytest.raises(ServiceError) as caught:
        await service.rename(owner.id, second, " eTf ")
    assert caught.value.code == "WATCHLIST_NAME_EXISTS"
    assert (await service.get(owner.id, target)).name == "ETF"


async def test_concurrent_creates_cannot_exceed_ten_lists(service_db, source):
    owner = await service_db.user()
    service = WatchlistService(service_db.factory, source.stocks)
    for number in range(9):
        await service.create(owner.id, str(number))
    results = await outcomes(service.create(owner.id, "第十"), service.create(owner.id, "第十一"))
    errors = [item for item in results if isinstance(item, Exception)]
    assert len(errors) == 1
    assert_error(errors[0], "WATCHLIST_LIMIT_REACHED")
    assert len(await service.list(owner.id)) == 10


async def test_concurrent_additions_cannot_exceed_one_hundred_items(service_db, source):
    owner = await service_db.user()
    service = WatchlistService(service_db.factory, source.stocks)
    target = await service.create(owner.id, "容量測試")
    async with service_db.factory.begin() as session:
        session.add_all(
            [
                WatchlistItem(watchlist_id=target, stock_code=str(1000 + index), position=index)
                for index in range(99)
            ]
        )
    results = await outcomes(
        service.add_item(owner.id, target, "2000"), service.add_item(owner.id, target, "2001")
    )
    errors = [item for item in results if isinstance(item, Exception)]
    assert len(errors) == 1
    assert_error(errors[0], "WATCHLIST_ITEM_LIMIT_REACHED")
    view = await service.get(owner.id, target)
    assert len(view.items) == 100
    assert len({item.position for item in view.items}) == 100


@pytest.mark.parametrize("action", ["get", "rename", "delete", "add", "remove", "reorder"])
async def test_other_users_cannot_access_or_mutate_a_list(service_db, source, action):
    owner, outsider = await service_db.user(), await service_db.user()
    service = WatchlistService(service_db.factory, source.stocks)
    target = await service.create(owner.id, "私人")
    operations = {
        "get": lambda: service.get(outsider.id, target),
        "rename": lambda: service.rename(outsider.id, target, "新名稱"),
        "delete": lambda: service.delete(outsider.id, target),
        "add": lambda: service.add_item(outsider.id, target, "0050"),
        "remove": lambda: service.remove_item(outsider.id, target, "0050"),
        "reorder": lambda: service.reorder(outsider.id, target, []),
    }
    with pytest.raises(ServiceError) as caught:
        await operations[action]()
    assert caught.value.code == "WATCHLIST_NOT_FOUND"
    assert source.calls == []
    assert (await service.get(owner.id, target)).name == "私人"


async def test_fresh_validation_releases_db_connection_and_never_caches(service_db, source):
    owner = await service_db.user()
    service = WatchlistService(service_db.factory, source.stocks)
    target = await service.create(owner.id, "即時驗證")

    async def assert_no_connection_held():
        assert service_db.engine.pool.checkedout() == 0

    source.before_reply = assert_no_connection_held
    await service.add_item(owner.id, target, " 0050 ")
    with pytest.raises(ServiceError) as caught:
        await service.add_item(owner.id, target, "0050")
    assert caught.value.code == "WATCHLIST_ITEM_EXISTS"
    assert len(source.calls) == 2
    view = await service.get(owner.id, target)
    assert [(item.stock_code, item.position) for item in view.items] == [("0050", 0)]


@pytest.mark.parametrize(
    ("status", "codes", "expected"),
    [
        (503, ["0050"], "SOURCE_UNAVAILABLE"),
        (200, ["2330"], "STOCK_NOT_FOUND"),
        (200, ["0050", "0050"], "SOURCE_DATA_INVALID"),
    ],
)
async def test_source_failure_or_unknown_stock_never_writes(
    service_db, source, status, codes, expected
):
    owner = await service_db.user()
    service = WatchlistService(service_db.factory, source.stocks)
    target = await service.create(owner.id, "來源測試")
    before = await service.get(owner.id, target)
    source.status, source.codes = status, codes
    with pytest.raises(ServiceError) as caught:
        await service.add_item(owner.id, target, "0050")
    assert caught.value.code == expected
    after = await service.get(owner.id, target)
    assert after.items == ()
    assert after.updated_at == before.updated_at


async def test_soft_delete_during_source_request_prevents_later_insert(service_db, source):
    owner = await service_db.user()
    service = WatchlistService(service_db.factory, source.stocks)
    target = await service.create(owner.id, "競爭測試")

    async def delete_during_request():
        await AccountService(service_db.factory).soft_delete(owner.id)

    source.before_reply = delete_during_request
    with pytest.raises(ServiceError) as caught:
        await asyncio.wait_for(service.add_item(owner.id, target, "0050"), timeout=5)
    assert caught.value.code == "ACCOUNT_UNAVAILABLE"
    async with service_db.factory() as session:
        assert await session.scalar(select(WatchlistItem.watchlist_id)) is None


async def test_append_reorder_remove_are_contiguous_and_touch_parent(service_db, source):
    owner = await service_db.user()
    service = WatchlistService(service_db.factory, source.stocks)
    target = await service.create(owner.id, "排序")
    old_time = datetime.now(UTC) - timedelta(days=1)
    async with service_db.factory.begin() as session:
        await session.execute(
            update(Watchlist).where(Watchlist.id == target).values(updated_at=old_time)
        )
    for code in ("0050", "2330", "00632R"):
        await service.add_item(owner.id, target, code)
    assert (await service.get(owner.id, target)).updated_at > old_time
    await service.reorder(owner.id, target, ["00632R", "2330", "0050"])
    view = await service.get(owner.id, target)
    assert [(item.stock_code, item.position) for item in view.items] == [
        ("00632R", 0),
        ("2330", 1),
        ("0050", 2),
    ]
    await service.remove_item(owner.id, target, "2330")
    await service.add_item(owner.id, target, "2330")
    view = await service.get(owner.id, target)
    assert [(item.stock_code, item.position) for item in view.items] == [
        ("00632R", 0),
        ("0050", 1),
        ("2330", 2),
    ]
    for invalid in (["0050"], ["0050", "0050"]):
        with pytest.raises(ServiceError):
            await service.reorder(owner.id, target, invalid)
    assert (await service.get(owner.id, target)).items == view.items


async def test_position_uniqueness_is_checked_at_real_commit(service_db, source):
    owner = await service_db.user()
    target = await WatchlistService(service_db.factory, source.stocks).create(owner.id, "排序約束")
    with pytest.raises(IntegrityError, match="uq_watchlist_items_watchlist_id_position"):
        async with service_db.factory.begin() as session:
            session.add_all(
                [
                    WatchlistItem(watchlist_id=target, stock_code="0050", position=0),
                    WatchlistItem(watchlist_id=target, stock_code="2330", position=0),
                ]
            )
            await session.flush()  # Succeeds; the surrounding real commit must reject duplicates.


async def test_new_verification_revokes_old_and_only_hashes_are_stored(service_db):
    owner = await service_db.user()
    service = VerificationService(service_db.factory)
    first = await service.issue(owner.id, "reset_password", timedelta(minutes=30))
    second = await service.issue(owner.id, "reset_password", timedelta(minutes=30))
    assert first.value != second.value and len(second.value) == 43
    assert second.value not in repr(second)
    async with service_db.factory() as session:
        assert (await session.get(VerificationToken, first.id)).revoked_at is not None
        current = await session.get(VerificationToken, second.id)
        assert current.token_hash == digest_token(second.value)
        assert current.revoked_at is None
    with pytest.raises(ServiceError) as caught:
        await AccountService(service_db.factory).reset_password(
            first.value, password_hash="$argon2id$new"
        )
    assert caught.value.code == "TOKEN_INVALID"


async def test_parallel_token_issuance_leaves_one_pending_per_purpose(service_db):
    owner = await service_db.user()
    service = VerificationService(service_db.factory)
    results = await outcomes(
        service.issue(owner.id, "reset_password", timedelta(minutes=30)),
        service.issue(owner.id, "reset_password", timedelta(minutes=30)),
    )
    assert all(not isinstance(item, Exception) for item in results)
    async with service_db.factory() as session:
        pending = list(
            await session.scalars(
                select(VerificationToken).where(
                    VerificationToken.user_id == owner.id,
                    VerificationToken.revoked_at.is_(None),
                    VerificationToken.consumed_at.is_(None),
                )
            )
        )
        assert len(pending) == 1


async def test_parallel_reset_token_replay_has_exactly_one_success(service_db):
    owner = await service_db.user()
    sessions = SessionService(service_db.factory)
    issued = await sessions.issue(owner.id, timedelta(days=1))
    service = VerificationService(service_db.factory)
    reset = await service.issue(owner.id, "reset_password", timedelta(minutes=30))
    other = await service.issue(owner.id, "verify_email", timedelta(hours=1))
    accounts = AccountService(service_db.factory)
    results = await outcomes(
        accounts.reset_password(reset.value, password_hash="$argon2id$one"),
        accounts.reset_password(reset.value, password_hash="$argon2id$two"),
    )
    assert sum(item is None for item in results) == 1
    errors = [item for item in results if isinstance(item, Exception)]
    assert len(errors) == 1
    assert_error(errors[0], "TOKEN_INVALID")
    winner = "$argon2id$one" if results[0] is None else "$argon2id$two"
    async with service_db.factory() as session:
        assert (await session.get(User, owner.id)).password_hash == winner
        assert (await session.get(VerificationToken, reset.id)).consumed_at is not None
        assert (await session.get(VerificationToken, other.id)).revoked_at is not None
        assert (await session.get(AuthSession, issued.id)).revoked_at is not None


async def test_reset_failure_rolls_back_consumption_password_and_revocation(
    service_db, monkeypatch
):
    owner = await service_db.user()
    login = await SessionService(service_db.factory).issue(owner.id, timedelta(days=1))
    reset = await VerificationService(service_db.factory).issue(
        owner.id, "reset_password", timedelta(minutes=30)
    )

    async def fail_after_password_change(*_args):
        raise RuntimeError("controlled transaction failure")

    monkeypatch.setattr(account_module, "revoke_credentials", fail_after_password_change)
    with pytest.raises(RuntimeError):
        await AccountService(service_db.factory).reset_password(
            reset.value, password_hash="$argon2id$new"
        )
    async with service_db.factory() as session:
        assert (await session.get(User, owner.id)).password_hash == owner.password_hash
        assert (await session.get(VerificationToken, reset.id)).consumed_at is None
        assert (await session.get(AuthSession, login.id)).revoked_at is None


async def test_email_verification_checks_purpose_and_is_single_use(service_db):
    owner = await service_db.user(status="pending_verification")
    token = await VerificationService(service_db.factory).issue(
        owner.id, "verify_email", timedelta(hours=1)
    )
    accounts = AccountService(service_db.factory)
    with pytest.raises(ServiceError) as caught:
        await accounts.reset_password(token.value, password_hash="$argon2id$new")
    assert caught.value.code == "TOKEN_INVALID"
    await accounts.verify_email(token.value)
    with pytest.raises(ServiceError) as caught:
        await accounts.verify_email(token.value)
    assert caught.value.code == "TOKEN_INVALID"
    async with service_db.factory() as session:
        user = await session.get(User, owner.id)
        assert user.status == "active" and user.email_verified_at is not None
        assert (
            await session.scalar(select(AuthSession.id).where(AuthSession.user_id == owner.id))
            is None
        )


async def test_expiry_is_rechecked_after_waiting_for_user_lock(service_db):
    owner = await service_db.user()
    token = await VerificationService(service_db.factory).issue(
        owner.id, "reset_password", timedelta(milliseconds=300)
    )
    async with service_db.factory.begin() as blocker:
        await blocker.scalar(select(User).where(User.id == owner.id).with_for_update())
        pending = asyncio.create_task(
            AccountService(service_db.factory).reset_password(
                token.value, password_hash="$argon2id$new"
            )
        )
        await asyncio.sleep(0.5)
        assert not pending.done()
    with pytest.raises(ServiceError) as caught:
        await pending
    assert caught.value.code == "TOKEN_INVALID"


async def test_soft_delete_and_session_issuance_cannot_leave_active_sessions(service_db):
    owner = await service_db.user()
    results = await outcomes(
        AccountService(service_db.factory).soft_delete(owner.id),
        SessionService(service_db.factory).issue(owner.id, timedelta(days=1)),
    )
    assert results[0] is None
    async with service_db.factory() as session:
        assert (
            await session.scalar(
                select(AuthSession.id).where(
                    AuthSession.user_id == owner.id,
                    AuthSession.revoked_at.is_(None),
                )
            )
            is None
        )


async def test_multi_device_sessions_and_owner_scoped_logout(service_db):
    owner, outsider = await service_db.user(), await service_db.user()
    service = SessionService(service_db.factory)
    first = await service.issue(owner.id, timedelta(days=1))
    second = await service.issue(owner.id, timedelta(days=1))
    assert (await service.authenticate(first.value)).user_id == owner.id
    with pytest.raises(ServiceError) as caught:
        await service.revoke(outsider.id, first.id)
    assert caught.value.code == "SESSION_NOT_FOUND"
    await service.revoke_others(owner.id, first.id)
    assert (await service.authenticate(first.value)).id == first.id
    with pytest.raises(ServiceError):
        await service.authenticate(second.value)
    await service.revoke(owner.id, first.id)
    with pytest.raises(ServiceError):
        await service.authenticate(first.value)


@pytest.mark.parametrize("status", ["disabled", "pending_verification", "deleted", "expired"])
async def test_session_authentication_rejects_unavailable_accounts_and_expiry(service_db, status):
    owner = await service_db.user()
    service = SessionService(service_db.factory)
    login = await service.issue(owner.id, timedelta(days=1))
    async with service_db.factory.begin() as session:
        now = datetime.now(UTC)
        if status == "deleted":
            await session.execute(update(User).where(User.id == owner.id).values(deleted_at=now))
        elif status == "expired":
            await session.execute(
                update(AuthSession)
                .where(AuthSession.id == login.id)
                .values(
                    created_at=now - timedelta(days=2),
                    last_seen_at=now - timedelta(days=2),
                    expires_at=now - timedelta(days=1),
                )
            )
        else:
            await session.execute(update(User).where(User.id == owner.id).values(status=status))
    with pytest.raises(ServiceError) as caught:
        await service.authenticate(login.value)
    assert caught.value.code == "SESSION_INVALID"


async def test_pending_token_unique_index_is_independent_of_expiry(service_db):
    owner = await service_db.user()
    now = datetime.now(UTC)
    async with service_db.factory.begin() as session:
        session.add(
            VerificationToken(
                user_id=owner.id,
                purpose="reset_password",
                token_hash=digest_token("expired"),
                created_at=now - timedelta(days=2),
                expires_at=now - timedelta(days=1),
            )
        )
    with pytest.raises(IntegrityError, match="uq_verification_tokens_pending"):
        async with service_db.factory.begin() as session:
            session.add(
                VerificationToken(
                    user_id=owner.id,
                    purpose="reset_password",
                    token_hash=digest_token("new"),
                    expires_at=now + timedelta(days=1),
                )
            )
    # Issuance explicitly revokes even expired pending tokens before replacement.
    await VerificationService(service_db.factory).issue(
        owner.id, "reset_password", timedelta(hours=1)
    )


async def test_cleanup_dry_run_batches_and_retains_recent_or_active_rows(service_db):
    owner = await service_db.user()
    now = datetime.now(UTC)
    old = now - timedelta(days=60)
    async with service_db.factory.begin() as session:
        for number, state in enumerate(["expired_old", "revoked_old", "recent", "active"]):
            session.add(
                AuthSession(
                    user_id=owner.id,
                    token_hash=digest_token(f"session-{number}"),
                    created_at=old,
                    last_seen_at=old,
                    expires_at=now - timedelta(days=31)
                    if state == "expired_old"
                    else now + timedelta(days=1),
                    revoked_at=now - timedelta(days=31)
                    if state == "revoked_old"
                    else (now - timedelta(days=29) if state == "recent" else None),
                )
            )
        for number, state in enumerate(
            ["expired_old", "revoked_old", "consumed_old", "recent", "active"]
        ):
            session.add(
                VerificationToken(
                    user_id=owner.id,
                    purpose="verify_email" if state == "active" else "reset_password",
                    token_hash=digest_token(f"verification-{number}"),
                    created_at=old,
                    expires_at=now - timedelta(days=31)
                    if state == "expired_old"
                    else now + timedelta(days=1),
                    revoked_at=now - timedelta(days=31)
                    if state == "revoked_old"
                    else (now - timedelta(days=29) if state == "recent" else None),
                    consumed_at=now - timedelta(days=31) if state == "consumed_old" else None,
                )
            )
    preview = await cleanup_credentials(service_db.factory, dry_run=True)
    assert (preview.sessions, preview.verification_tokens, preview.dry_run) == (2, 3, True)
    results = await outcomes(
        cleanup_credentials(service_db.factory, batch_size=1),
        cleanup_credentials(service_db.factory, batch_size=1),
    )
    assert all(not isinstance(item, Exception) for item in results)
    assert sum(item.sessions for item in results) == 2
    assert sum(item.verification_tokens for item in results) == 3
    async with service_db.factory() as session:
        assert await session.scalar(select(func.count()).select_from(AuthSession)) == 2
        assert await session.scalar(select(func.count()).select_from(VerificationToken)) == 2
        assert await session.scalar(select(User.id).where(User.id == owner.id)) == owner.id


async def test_original_checks_and_fk_indexes_still_exist(service_db):
    async with service_db.factory() as session:
        checks = set(
            (
                await session.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE connamespace = 'app'::regnamespace"
                    )
                )
            ).scalars()
        )
        assert {"ck_users_status_allowed", "ck_verification_tokens_purpose_allowed"} <= checks
        indexes = set(
            (
                await session.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname = 'app'")
                )
            ).scalars()
        )
        assert {
            "ix_auth_sessions_user_id",
            "ix_auth_sessions_expires_at",
            "ix_verification_tokens_user_id_purpose",
            "ix_verification_tokens_expires_at",
            "ix_watchlists_user_id_created_at",
        } <= indexes
