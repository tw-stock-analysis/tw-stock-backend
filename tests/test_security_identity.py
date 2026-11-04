"""Evidence registry invariants; real PostgreSQL for durable identity and atomic imports."""

from datetime import date
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete, select

from app.core.config import Settings
from app.main import create_app
from app.models.security_identity import SecurityIdentity, SecurityListing
from app.schemas.analysis import taipei_today
from app.schemas.instruments import InstrumentRef
from app.schemas.security_identity import (
    IdentifierInput,
    IdentityEvidence,
    IdentityImportBundle,
    ListingInput,
    SecurityImport,
)
from app.services.errors import ServiceError
from app.services.security_identity import SecurityIdentityService
from app.services.security_import import (
    TWSE_PATHS,
    TWSE_SOURCE,
    build_twse_bundle,
    fetch_twse_metadata,
    official_date,
)


def evidence(day):
    return {
        "source_name": "Synthetic verified metadata fixture",
        "source_url": "https://example.test/identity-events",
        "observed_on": taipei_today(),
        "effective_date": day,
    }


def listing(market="TWSE", code="Z001", start="2020-01-01", end=None):
    return {
        "market": market,
        "code": code,
        "name": "測試證券",
        "valid_from": start,
        "valid_to": end,
        "start_evidence": evidence(start),
        "end_evidence": evidence(end) if end else None,
    }


def imported(key, listings=None, **kwargs):
    return SecurityImport(
        registry_key=key,
        name="測試證券",
        instrument_type="ordinary_share",
        listings=listings or [listing()],
        **kwargs,
    )


def bundle(*records):
    return IdentityImportBundle(securities=list(records))


@pytest_asyncio.fixture
async def registry(service_db):
    service = SecurityIdentityService(service_db.factory)
    try:
        yield SimpleNamespace(service=service, factory=service_db.factory, prefix=service_db.prefix)
    finally:
        async with service_db.factory.begin() as session:
            await session.execute(
                delete(SecurityIdentity).where(
                    SecurityIdentity.registry_key.startswith(service_db.prefix)
                )
            )


def test_dates_and_evidence_require_actual_effective_events():
    assert official_date("19620209") == date(1962, 2, 9)
    assert official_date("1151030") == date(2026, 10, 30)
    assert official_date("090/01/20") == date(2001, 1, 20)
    assert official_date("") is None
    assert official_date("1151333") is None
    with pytest.raises(ValidationError):
        ListingInput(**{**listing(), "valid_to": "2021-01-01"})
    with pytest.raises(ValidationError):
        ListingInput(**{**listing(), "start_evidence": evidence("2020-01-02")})
    with pytest.raises(ValidationError):
        IdentityImportBundle.model_validate(
            {
                "securities": [
                    {"registry_key": "bad", "name": "bad", "listings": [listing()], "prices": []}
                ]
            }
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:fake-test-only@example.test/event",
        "https://example.test/event?token=fake-test-only",
    ],
)
def test_provenance_rejects_embedded_credentials(url):
    with pytest.raises(ValidationError):
        IdentityEvidence.model_validate({**evidence("2020-01-01"), "source_url": url})


def test_isin_check_digit_and_identifier_end_provenance():
    valid = IdentifierInput(
        kind="isin", value="TW0002330008", valid_from="2020-01-01", evidence=evidence("2020-01-01")
    )
    assert valid.value == "TW0002330008"
    with pytest.raises(ValidationError):
        IdentifierInput(
            kind="isin",
            value="TW0002330009",
            valid_from="2020-01-01",
            evidence=evidence("2020-01-01"),
        )
    with pytest.raises(ValidationError):
        IdentifierInput(
            kind="name",
            value="舊名",
            valid_from="2020-01-01",
            valid_to="2021-01-01",
            evidence=evidence("2020-01-01"),
        )


async def test_stable_uuid_market_transition_and_dated_name_alias(registry):
    record = imported(
        registry.prefix + "transfer",
        [
            listing("TPEX", start="2020-01-01", end="2022-01-01"),
            listing("TWSE", start="2022-01-01"),
        ],
        issuer_ubn="12345678",
        identifiers=[
            {
                "kind": "name",
                "value": "舊名",
                "valid_from": "2020-01-01",
                "valid_to": "2022-01-01",
                "evidence": evidence("2020-01-01"),
                "end_evidence": evidence("2022-01-01"),
            }
        ],
    )
    first = await registry.service.import_bundle(bundle(record))
    repeat = await registry.service.import_bundle(bundle(record))
    stable = UUID(first["securities"][0]["id"])
    assert repeat["unchanged"] == 1 and repeat["securities"][0]["id"] == str(stable)
    before = await registry.service.get(stable, date(2019, 12, 31))
    assert before.market_at_as_of.status == "unknown"
    tpex = await registry.service.get(stable, date(2021, 12, 31))
    twse = await registry.service.get(stable, date(2022, 1, 1))
    assert tpex.market_at_as_of.market == "TPEX"
    assert twse.market_at_as_of.market == "TWSE"
    assert twse.identifiers[0].value == "舊名"
    assert (
        await registry.service.verify_range(
            InstrumentRef(code="Z001"), date(2021, 1, 1), date(2023, 1, 1)
        )
        == stable
    )
    assert (
        await registry.service.resolve(
            InstrumentRef(market="DELISTED", code="Z001"), date(2021, 6, 1)
        )
    ).security_id == stable


async def test_same_code_reuse_has_distinct_ids_and_gap_stays_unknown(registry):
    one = imported(
        registry.prefix + "old",
        [listing(start="2018-01-01", end="2020-01-01")],
        issuer_ubn="12345678",
    )
    two = imported(registry.prefix + "new", [listing(start="2021-01-01")], issuer_ubn="12345678")
    result = await registry.service.import_bundle(bundle(one, two))
    old_id, new_id = (UUID(row["id"]) for row in result["securities"])
    assert old_id != new_id
    old = await registry.service.resolve(InstrumentRef(code="Z001"), date(2019, 1, 1))
    gap = await registry.service.resolve(InstrumentRef(code="Z001"), date(2020, 1, 1))
    new = await registry.service.resolve(InstrumentRef(code="Z001"), date(2021, 1, 1))
    assert old.security_id == old_id and new.security_id == new_id
    assert gap.status == "unknown" and gap.security_id is None
    assert (
        await registry.service.lookup(code="Z001", as_of=date(2020, 6, 1))
    ).pagination.total == 0
    all_refs = await registry.service.lookup(
        code="Z001", as_of=date(2020, 6, 1), include_inactive=True
    )
    assert all_refs.pagination.total == 2
    with pytest.raises(ServiceError) as error:
        await registry.service.verify_range(
            InstrumentRef(code="Z001"), date(2019, 1, 1), date(2022, 1, 1)
        )
    assert error.value.code == "SECURITY_IDENTITY_AMBIGUOUS"


async def test_overlapping_bundle_is_fully_rolled_back(registry):
    first = imported(registry.prefix + "existing")
    await registry.service.import_bundle(bundle(first))
    with pytest.raises(ServiceError) as error:
        await registry.service.import_bundle(
            bundle(
                imported(registry.prefix + "would-have-been-new", [listing(code="Z002")]),
                imported(registry.prefix + "conflict"),
            )
        )
    assert error.value.code == "SECURITY_TIMELINE_CONFLICT"
    async with registry.factory() as session:
        keys = set(
            await session.scalars(
                select(SecurityIdentity.registry_key).where(
                    SecurityIdentity.registry_key.startswith(registry.prefix)
                )
            )
        )
    assert keys == {first.registry_key}


async def test_dry_run_persists_nothing_and_explicit_id_conflicts_do_not_merge(registry):
    incoming = imported(registry.prefix + "dry")
    preview = await registry.service.import_bundle(bundle(incoming), apply=False)
    with pytest.raises(ServiceError) as error:
        await registry.service.get(UUID(preview["securities"][0]["id"]))
    assert error.value.status_code == 404
    first = await registry.service.import_bundle(bundle(incoming))
    stable = UUID(first["securities"][0]["id"])
    with pytest.raises(ServiceError) as error:
        await registry.service.import_bundle(
            bundle(imported(registry.prefix + "changed-key", id=stable))
        )
    assert error.value.code == "SECURITY_REGISTRY_KEY_CONFLICT"


async def test_close_interval_and_identifier_is_idempotent_and_cannot_reopen(registry):
    alias = {
        "kind": "isin",
        "value": "TW0002330008",
        "valid_from": "2020-01-01",
        "evidence": evidence("2020-01-01"),
    }
    first = imported(registry.prefix + "closing", identifiers=[alias])
    result = await registry.service.import_bundle(bundle(first))
    stable = UUID(result["securities"][0]["id"])
    closed = imported(
        first.registry_key,
        [listing(end="2022-01-01")],
        identifiers=[{**alias, "valid_to": "2022-01-01", "end_evidence": evidence("2022-01-01")}],
    )
    updated = await registry.service.import_bundle(bundle(closed))
    assert updated["updated"] == 1
    assert (await registry.service.import_bundle(bundle(closed)))["unchanged"] == 1
    assert (
        await registry.service.get(stable, date(2022, 1, 1))
    ).market_at_as_of.status == "unknown"
    with pytest.raises(ServiceError) as error:
        await registry.service.import_bundle(bundle(first))
    assert error.value.code == "SECURITY_CLOSED_INTERVAL_CONFLICT"


async def test_range_rejects_unknown_gaps_emerging_and_ticker_changes(registry):
    cases = [
        (
            "gap",
            [listing(start="2020-01-01", end="2021-01-01"), listing(start="2021-02-01")],
            "SECURITY_RANGE_GAP",
        ),
        (
            "emerging",
            [
                listing("EMERGING", start="2020-01-01", end="2021-01-01"),
                listing("TWSE", start="2021-01-01"),
            ],
            "SECURITY_MARKET_UNSUPPORTED",
        ),
        (
            "code",
            [
                listing(code="Z002", start="2020-01-01", end="2021-01-01"),
                listing(start="2021-01-01"),
            ],
            "SECURITY_CODE_CHANGE_UNSUPPORTED",
        ),
    ]
    for label, listings, expected in cases:
        record = imported(registry.prefix + label, listings)
        result = await registry.service.import_bundle(bundle(record))
        with pytest.raises(ServiceError) as error:
            await registry.service.verify_range(
                InstrumentRef(code="Z001"), date(2020, 6, 1), date(2022, 1, 1)
            )
        assert error.value.code == expected
        async with registry.factory.begin() as session:
            await session.execute(
                delete(SecurityIdentity).where(
                    SecurityIdentity.id == UUID(result["securities"][0]["id"])
                )
            )


async def test_corrupt_overlaps_are_ambiguous_even_if_end_has_unique_identity(registry):
    result = await registry.service.import_bundle(bundle(imported(registry.prefix + "corrupt")))
    stable = UUID(result["securities"][0]["id"])
    async with registry.factory.begin() as session:
        corrupt = ListingInput.model_validate(listing("TPEX", start="2020-06-01", end="2021-01-01"))
        session.add(
            SecurityListing(
                security_id=stable,
                **corrupt.model_dump(exclude={"start_evidence", "end_evidence"}),
                start_evidence=corrupt.start_evidence.model_dump(mode="json"),
                end_evidence=corrupt.end_evidence.model_dump(mode="json"),
            )
        )
    assert (
        await registry.service.get(stable, date(2020, 7, 1))
    ).market_at_as_of.status == "ambiguous"
    with pytest.raises(ServiceError) as error:
        await registry.service.verify_range(
            InstrumentRef(code="Z001"), date(2020, 1, 1), date(2022, 1, 1)
        )
    assert error.value.code == "SECURITY_IDENTITY_AMBIGUOUS"


def test_twse_builder_uses_actual_trading_dates_keeps_reuse_separate_and_has_no_quotes():
    basic = [
        {
            "公司代號": "Z001",
            "公司簡稱": "新證券",
            "上市日期": "20220103",
            "營利事業統一編號": "12345678",
            "close": "999",
        }
    ]
    newly = [
        {"Code": "Z001", "Company": "舊證券", "ApprovedListingDate": "1090102", "ListingDate": ""},
        {
            "Code": "Z002",
            "Company": "只有核准日",
            "ApprovedListingDate": "",
            "ListingDate": "1100104",
        },
        {"Code": "Z003", "Company": "無終止資料", "ApprovedListingDate": "1090102"},
    ]
    ended = [{"Code": "Z001", "Company": "舊證券", "DelistingDate": "110/01/04"}]
    data, summary = build_twse_bundle(basic, newly, ended)
    assert len(data.securities) == 2
    assert len({row.registry_key for row in data.securities}) == 2
    assert data.securities[0].listings[0].valid_from == date(2022, 1, 3)
    assert data.securities[1].listings[0].valid_to == date(2021, 1, 4)
    assert data.securities[0].instrument_type == "other"
    assert "close" not in data.model_dump_json()
    assert summary["skipped"]["missing_or_invalid_date"] == 1
    assert summary["skipped"]["unpaired_history"] == 1


async def test_twse_fetches_only_three_fixed_metadata_endpoints_sequentially():
    calls = []
    payloads = [[{"公司代號": "Z001", "公司簡稱": "證券", "上市日期": "20220103"}], [], []]

    async def handler(request):
        calls.append(request)
        assert request.headers["cache-control"] == "no-cache"
        return httpx.Response(200, json=payloads[(len(calls) - 1) % 3])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_twse_metadata(client)
        await fetch_twse_metadata(client)
    assert [str(r.url) for r in calls] == [TWSE_SOURCE + p for p in TWSE_PATHS] * 2
    assert all(r.method == "GET" for r in calls)


async def test_public_identity_http_is_read_only_and_no_store(registry):
    result = await registry.service.import_bundle(bundle(imported(registry.prefix + "http")))
    stable = result["securities"][0]["id"]
    app = create_app(Settings(_env_file=None, postgres_password="unused-test-setting"))
    app.state.session_factory = registry.factory
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        lookup = await client.get("/api/v1/securities?code=Z001&as_of=2021-01-01")
        assert lookup.status_code == 200
        assert lookup.headers["cache-control"] == "no-store"
        assert lookup.json()["data"][0]["id"] == stable
        resolution = await client.get(
            "/api/v1/securities/resolve?market=TWSE&code=Z001&as_of=2021-01-01"
        )
        assert resolution.json()["status"] == "verified"
        detail = await client.get(f"/api/v1/securities/{stable}?as_of=2019-01-01")
        assert detail.json()["market_at_as_of"]["status"] == "unknown"
        assert (await client.post("/api/v1/securities", json={})).status_code == 405
        assert (await client.get("/api/v1/securities?limit=101")).status_code == 422
        assert (await client.get("/api/v1/securities?as_of=2200-01-01")).status_code == 422


async def test_concurrent_conflicting_imports_have_one_winner(registry):
    import asyncio

    outcomes = await asyncio.wait_for(
        asyncio.gather(
            registry.service.import_bundle(bundle(imported(registry.prefix + "race-a"))),
            registry.service.import_bundle(bundle(imported(registry.prefix + "race-b"))),
            return_exceptions=True,
        ),
        timeout=10,
    )
    errors = [result for result in outcomes if isinstance(result, ServiceError)]
    assert len(errors) == 1 and errors[0].code == "SECURITY_TIMELINE_CONFLICT"
    async with registry.factory() as session:
        keys = list(
            await session.scalars(
                select(SecurityIdentity.registry_key).where(
                    SecurityIdentity.registry_key.startswith(registry.prefix)
                )
            )
        )
    assert len(keys) == 1
