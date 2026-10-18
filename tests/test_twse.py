from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.services.errors import ServiceError
from app.services.twse import (
    MASTER_URL,
    PRICES_URL,
    InvalidSourceData,
    TWSEProvider,
    number,
    parse_snapshot,
    source_date,
)


def test_normalizes_dates_decimals_and_excludes_non_company_securities(snapshot):
    assert snapshot.master_date == date(2024, 1, 3)
    assert snapshot.prices_date == date(2024, 1, 2)
    assert [row["ticker"] for row in snapshot.prices] == ["2330", "2317"]
    quote = snapshot.prices[0]
    assert quote["close"] == Decimal("593.00")
    assert quote["volume_shares"] == Decimal("12345678")
    assert quote["transactions"] == 1234
    assert snapshot.prices[1]["close"] is None
    assert snapshot.prices[1]["volume_shares"] == 0


@pytest.mark.parametrize("value", ["1130102", "113/01/02", "20240102", "2024-01-02"])
def test_date_formats(value):
    assert source_date(value) == date(2024, 1, 2)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "oops", "1.0000001", "1e3", 123])
def test_rejects_invalid_or_negative_values(value):
    with pytest.raises(InvalidSourceData):
        number(value)


@pytest.mark.parametrize(
    "case", ["empty", "missing_field", "duplicates", "bad_ohlc", "mixed_dates"]
)
def test_rejects_bad_source_snapshots_atomically(source_rows, case):
    master, prices = source_rows
    if case == "empty":
        master.clear()
    elif case == "missing_field":
        del prices[0]["ClosingPrice"]
    elif case == "duplicates":
        prices.append(prices[0])
    elif case == "bad_ohlc":
        prices[0]["ClosingPrice"] = "600.00"
    else:
        prices[1]["Date"] = "1130101"
    with pytest.raises(InvalidSourceData):
        parse_snapshot(master, prices)


def test_version_is_independent_of_response_order(source_rows):
    master, prices = source_rows
    first = parse_snapshot(master, prices)
    second = parse_snapshot(list(reversed(master)), list(reversed(prices)))
    assert first.master_version == second.master_version
    assert first.prices_version == second.prices_version


def test_non_comparable_change_is_not_a_zero(source_rows):
    master, prices = source_rows
    prices[0]["Change"] = "X0.00"
    assert parse_snapshot(master, prices).prices[0]["change"] is None


def test_depositary_receipts_are_excluded_from_the_initial_equity_scope(source_rows):
    master, prices = source_rows
    master.append(
        {
            "出表日期": "1130103",
            "公司代號": "910322",
            "公司簡稱": "測試-DR",
            "公司名稱": "測試存託憑證",
            "產業別": "20",
        }
    )
    assert len(parse_snapshot(master, prices).instruments) == 3


async def test_provider_uses_only_the_documented_official_paths(source_rows):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, json=source_rows[0 if str(request.url) == MASTER_URL else 1])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await TWSEProvider(client).fetch()
    assert requested == [MASTER_URL, PRICES_URL]
    assert snapshot.prices_date == date(2024, 1, 2)


@pytest.mark.parametrize("kind", ["403", "429", "500", "timeout", "html"])
async def test_upstream_failure_does_not_become_empty_or_fake_quotes(kind):
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("private upstream details", request=request)
        if kind == "html":
            return httpx.Response(200, text="<html>not data</html>")
        return httpx.Response(int(kind), text="private upstream details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ServiceError) as error:
            await TWSEProvider(client).fetch()
    assert error.value.status_code == 503
    assert "private" not in error.value.message
