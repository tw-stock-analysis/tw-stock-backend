"""Synthetic official-contract samples; never runtime fallback datasets."""

import json
from datetime import date

import httpx
import pytest

from app.services.data_catalog import DATASETS, get_dataset
from app.services.errors import ServiceError
from app.services.providers.open_data import OpenDataProvider, decode_records, parse_date


def test_revenue_keeps_native_unit_and_period_separate_from_publication_date():
    snapshot = decode_records(
        DATASETS["twse_revenue"],
        json.dumps(
            [
                {
                    "出表日期": "1151016",
                    "資料年月": "11509",
                    "公司代號": "2330",
                    "營業收入-當月營收": "1,234,567.890123",
                    "營業收入-去年同月增減(%)": "--",
                },
            ]
        ).encode(),
    )
    assert snapshot.data_as_of == date(2026, 10, 16)
    assert snapshot.records[0].fields["資料年月"] == "11509"
    assert snapshot.records[0].fields["營業收入-當月營收"] == "1,234,567.890123"
    assert snapshot.records[0].fields["營業收入-去年同月增減(%)"] == "--"
    assert snapshot.records[0].symbol == "2330"
    assert snapshot.records[0].record_date_field == "出表日期"


def test_json_numbers_do_not_lose_precision():
    snapshot = decode_records(
        DATASETS["twse_prices"],
        b'[{"Code":"2330",'
        b'"Date":"20261016","ClosingPrice":1234.1234567890123456789,'
        b'"TradeVolume":9007199254740993}]',
    )
    assert snapshot.records[0].fields["ClosingPrice"] == "1234.1234567890123456789"
    assert snapshot.records[0].fields["TradeVolume"] == "9007199254740993"


def test_tdcc_csv_bom_and_quoted_native_columns():
    body = "\ufeff資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n"
    body += '20261016,0050,1,20,"1,500",0.01\n'
    snapshot = decode_records(DATASETS["tdcc_distribution"], body.encode())
    assert snapshot.records[0].symbol == "0050"
    assert snapshot.records[0].fields["股數"] == "1,500"
    assert snapshot.data_as_of == date(2026, 10, 16)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("115/10/16", date(2026, 10, 16)),
        ("20261016", date(2026, 10, 16)),
        ("1151016", date(2026, 10, 16)),
        ("2026年10月16日", date(2026, 10, 16)),
        ("11509", None),
        ("2026-09", None),
        ("--", None),
    ],
)
def test_dates_do_not_invent_days_for_reporting_months(value, expected):
    assert parse_date(value) == expected


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b"[]",
        b"<html>maintenance</html>",
        b'[{"Code":"2330","x":NaN}]',
        b'[{"Code":"2330","x":{}}]',
        b'[{"Code":"2330","Date":"1150230"}]',
        b'[{"UnexpectedSymbol":"2330","ClosingPrice":"100"}]',
        b'[{"Code":"2330","Code":"2317","Date":"20261016"}]',
    ],
)
def test_bad_or_changed_source_is_not_empty_success(body):
    with pytest.raises(ServiceError) as exc:
        decode_records(DATASETS["twse_prices"], body)
    assert exc.value.code == "SOURCE_DATA_INVALID"


def test_empty_event_snapshot_is_valid_but_empty_price_snapshot_is_not():
    snapshot = decode_records(DATASETS["twse_announcements"], b"[]")
    assert snapshot.records == []
    assert snapshot.data_as_of is None


def test_catalog_never_accepts_arbitrary_urls_or_traversal():
    for key in ["http://localhost/", "../twse_prices", "finmind_income", "twse_t86"]:
        with pytest.raises(ServiceError):
            get_dataset(key)
    assert DATASETS["twse_lending_limits"].category.value == "lending-limits"
    assert len(DATASETS) == len(set(DATASETS))


@pytest.mark.parametrize("status", [301, 403, 429, 500])
async def test_source_failures_are_sanitized_and_redirects_are_not_followed(status):
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(
            status, text="private upstream details", headers={"Location": "http://localhost/"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ServiceError) as exc:
            await OpenDataProvider(client).fetch(DATASETS["twse_prices"])
    assert len(calls) == 1
    assert "private" not in exc.value.message


async def test_tpex_checks_live_contract_and_reuses_discovery():
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("swagger.json"):
            return httpx.Response(200, json={"paths": {"/tpex_3insti_daily_trading": {"get": {}}}})
        return httpx.Response(
            200, json=[{"SecuritiesCode": "6488", "Date": "1151016", "ForeignNet": "-100"}]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OpenDataProvider(client)
        result = await provider.fetch(DATASETS["tpex_institutional"])
        assert result.records[0].symbol == "6488"
        assert result.records[0].fields["ForeignNet"] == "-100"
        await provider.fetch(DATASETS["tpex_institutional"])
        with pytest.raises(ServiceError) as exc:
            await provider.fetch(DATASETS["tpex_prices"])
    assert exc.value.code == "SOURCE_CONTRACT_CHANGED"
    assert calls.count("/openapi/swagger.json") == 1
    assert len(calls) == 3


async def test_oversized_source_body_is_rejected(monkeypatch):
    monkeypatch.setattr("app.services.providers.open_data.MAX_BYTES", 8)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"x" * 9),
        )
    ) as client:
        with pytest.raises(ServiceError) as exc:
            await OpenDataProvider(client).fetch(DATASETS["twse_prices"])
    assert exc.value.code == "SOURCE_DATA_INVALID"
