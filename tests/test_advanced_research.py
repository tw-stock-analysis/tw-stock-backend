"""Fixed topics, exact financial arithmetic, paid-source failures and read-only routing."""

from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_finmind_service, get_twse_service
from app.core.config import Settings
from app.db.session import get_session
from app.main import create_app
from app.schemas.advanced_research import AdvancedQuery
from app.services.advanced_research import SPECS, TOPICS
from app.services.finmind import BROKER_URL, FinMindService
from app.services.research_transforms import etf_rows, fill_progress, valuations
from app.services.twse import TwseService

CODE = "2330"
DAY = "2026-07-01"
TOKEN = "test-only-advanced-secret"


def source_row(**values):
    return {"date": DAY, "stock_id": CODE, **values}


PAYLOADS = {
    "TaiwanStockPER": [source_row(PER="20.125", PBR="3", dividend_yield="0")],
    "TaiwanStockDividend": [
        source_row(
            year="114年第4季",
            CashEarningsDistribution="2.5",
            CashStatutorySurplus="0.5",
            StockEarningsDistribution="0",
            StockStatutorySurplus="0",
            CashExDividendTradingDate=DAY,
            StockExDividendTradingDate="",
            CashDividendPaymentDate="2026-07-31",
            AnnouncementDate="2026-06-01",
        )
    ],
    "TaiwanStockDividendResult": [
        source_row(
            before_price="100",
            reference_price="90",
            stock_and_cache_dividend="10",
            stock_or_cache_dividend="息",
        )
    ],
    "TaiwanStockPrice": [
        source_row(open="95", max="95", min="95", close="95"),
        source_row(date="2026-07-02", open="100", max="100", min="100", close="100"),
    ],
    "TaiwanStockPriceAdj": [source_row(open="85", max="85", min="85", close="85")],
    "TaiwanStockHoldingSharesPer": [
        source_row(
            HoldingSharesLevel="1000001以上",
            people="200",
            unit="12345678901234567890",
            percent="52.5",
        )
    ],
    "TaiwanStockTradingDailyReport": [
        source_row(
            date="2026-07-31",
            securities_trader_id="1020",
            securities_trader="合庫",
            price="99",
            buy="1000",
            sell="0",
        ),
        source_row(
            date="2026-07-31",
            securities_trader_id="1020",
            securities_trader="合庫",
            price="100",
            buy="0",
            sell="1500",
        ),
    ],
    "TaiwanStockActiveETFHolding": [
        source_row(
            component_stock_id=CODE,
            component_stock_name="測試公司",
            asset_type="stock",
            shares="1000",
            weight="9.5",
            market_value="0",
            currency="TWD",
        )
    ],
    "TaiwanStockActiveETFHoldingChange": [
        source_row(component_stock_id=CODE, component_stock_name="測試公司", buy="0", sell="100")
    ],
    "TaiwanStockSplitPrice": [],
    "TaiwanStockCapitalReductionReferencePrice": [],
    "TaiwanStockParValueChange": [],
}
PAYLOADS["TaiwanStockFinancialStatements"] = [
    source_row(date="2026-06-30", type=key, value=value, origin_name=key)
    for key, value in (
        ("EPS", "2.35"),
        ("Revenue", "1000"),
        ("GrossProfit", "400"),
        ("OperatingIncome", "200"),
        ("IncomeAfterTaxes", "150"),
    )
]
PAYLOADS["TaiwanStockBalanceSheet"] = [
    source_row(
        date="2026-06-30", type="TotalAssets", value="100000000000000000000", origin_name="資產總額"
    )
]
PAYLOADS["TaiwanStockCashFlowsStatement"] = [
    source_row(
        date=day, type="CashFlowsFromOperatingActivities", value=value, origin_name="營業活動現金流"
    )
    for day, value in (("2026-03-31", "100"), ("2026-06-30", "250"))
]


@pytest.fixture
def advanced_api():
    app = create_app(Settings(_env_file=None, postgres_password="test-only", app_env="testing"))
    calls, replies = [], {}

    async def forbidden_db():
        raise AssertionError("Research cannot open a database session")
        yield

    app.dependency_overrides[get_session] = forbidden_db

    def upstream(request):
        if request.url.host == "openapi.twse.com.tw":
            calls.append("TWSE")
            return httpx.Response(200, json=[{"Code": CODE, "Name": "測試公司"}])
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert "token" not in request.url.params
        if str(request.url.copy_with(query=None)) == BROKER_URL:
            assert set(request.url.params) == {"data_id", "date"}
            dataset = "TaiwanStockTradingDailyReport"
        else:
            dataset = request.url.params["dataset"]
        if dataset == "TaiwanStockParValueChange":
            assert "data_id" not in request.url.params
        else:
            assert request.url.params["data_id"] == CODE
        calls.append(dataset)
        return replies.get(dataset) or httpx.Response(
            200, json={"status": 200, "data": deepcopy(PAYLOADS[dataset])}
        )

    async def twse():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            yield TwseService(client)

    async def finmind():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            yield FinMindService(client, TOKEN)

    app.dependency_overrides[get_twse_service] = twse
    app.dependency_overrides[get_finmind_service] = finmind
    with TestClient(app) as client:
        yield client, calls, replies


def query(client, topic, start="2026-01-01", code=CODE):
    if topic == "brokers":
        start = "2026-07-31"
    if topic == "etf":
        start = DAY
    return client.get(
        f"/api/v1/stocks/{code}/research",
        params={"topic": topic, "start_date": start, "end_date": "2026-07-31"},
    )


@pytest.mark.parametrize("topic", TOPICS)
def test_all_topics_use_fixed_fresh_ordered_sources_and_no_database(advanced_api, topic):
    client, calls, _ = advanced_api
    for _ in range(2):
        response = query(client, topic)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["complete"] is True
        assert TOKEN not in response.text
    assert calls == ["TWSE", *(SPECS[key].dataset for key in TOPICS[topic])] * 2


def test_financials_keep_eps_periods_and_only_difference_cashflow(advanced_api):
    client, _, _ = advanced_api
    blocks = {b["id"]: b["result"]["data"] for b in query(client, "financials").json()["blocks"]}
    assert blocks["earnings"][0]["eps"] == "2.35"
    assert blocks["earnings"][0]["gross_margin"] == "40.0000"
    assert blocks["earnings"][0]["operating_margin"] == "20.0000"
    assert blocks["balance"][0]["value"] == "100000000000000000000"
    assert blocks["balance"][0]["basis"] == "期末存量"
    assert [r["quarter_value"] for r in blocks["cashflow"]] == ["100", "150"]


def test_missing_cashflow_previous_quarter_never_becomes_zero(advanced_api):
    client, _, replies = advanced_api
    replies["TaiwanStockCashFlowsStatement"] = httpx.Response(
        200, json={"status": 200, "data": PAYLOADS["TaiwanStockCashFlowsStatement"][1:]}
    )
    cashflow = next(
        b for b in query(client, "financials").json()["blocks"] if b["id"] == "cashflow"
    )
    assert cashflow["result"]["data"][0]["quarter_value"] is None


def test_dividends_preserve_source_period_and_brokers_aggregate_price_bands(advanced_api):
    client, _, _ = advanced_api
    point = query(client, "dividends").json()["blocks"][0]["result"]["data"][0]
    assert point["year"] == "114年第4季"
    assert point["cash"] == "3.0"
    broker = query(client, "brokers").json()["blocks"][0]["result"]["data"][0]
    assert broker["buy"] == "1000" and broker["sell"] == "1500" and broker["net"] == "-500"


def test_etf_missing_valuation_and_absent_change_asset_type_are_not_fabricated(advanced_api):
    client, _, _ = advanced_api
    blocks = query(client, "etf").json()["blocks"]
    assert blocks[0]["result"]["data"][0]["market_value"] is None
    assert blocks[1]["result"]["data"][0]["asset_type"] is None
    assert blocks[1]["result"]["data"][0]["net"] == "-100"


@pytest.mark.parametrize("http_status", [200, 400])
def test_paid_source_business_400_is_access_denied_and_does_not_leak_body(
    advanced_api, http_status
):
    client, _, replies = advanced_api
    replies["TaiwanStockPriceAdj"] = httpx.Response(
        http_status, json={"status": 400, "msg": f"Please update your user level. {TOKEN}"}
    )
    response = query(client, "adjusted")
    assert response.json()["complete"] is False
    blocks = response.json()["blocks"]
    assert blocks[0]["result"]["error"]["code"] == "FINMIND_ACCESS_DENIED"
    assert blocks[0]["result"]["data"] is None
    assert blocks[1]["result"]["status"] == "ok"
    assert TOKEN not in response.text


def test_quota_failure_stops_remaining_sources_and_fill_needs_all_sources(advanced_api):
    client, calls, replies = advanced_api
    replies["TaiwanStockPrice"] = httpx.Response(429)
    body = query(client, "fill").json()
    assert body["blocks"][0]["result"]["status"] == "not_requested"
    assert body["blocks"][0]["result"]["error"]["code"] == "FINMIND_SOURCE_INCOMPLETE"
    assert calls == ["TWSE", "TaiwanStockDividendResult", "TaiwanStockPrice"]


@pytest.mark.parametrize(
    "params",
    [
        {"topic": "arbitrary"},
        {"topic": "valuation", "dataset": "TaiwanStockPER"},
        {"topic": "valuation", "end_date": "2999-01-01"},
        {"topic": "valuation", "start_date": "2000-01-01", "end_date": "2026-01-01"},
        {"topic": "etf", "start_date": "2026-01-01", "end_date": "2026-07-31"},
        {"topic": "brokers", "start_date": "2026-07-01", "end_date": "2026-07-31"},
    ],
)
def test_invalid_topics_and_queries_never_call_sources(advanced_api, params):
    client, calls, _ = advanced_api
    assert client.get(f"/api/v1/stocks/{CODE}/research", params=params).status_code == 422
    assert calls == []


def test_unknown_stock_never_calls_finmind(advanced_api):
    client, calls, _ = advanced_api
    assert query(client, "valuation", code="999999").status_code == 404
    assert calls == ["TWSE"]


def test_wrong_stock_or_duplicate_financial_subject_isolated_to_source(advanced_api):
    client, _, replies = advanced_api
    bad = deepcopy(PAYLOADS["TaiwanStockFinancialStatements"])
    bad.append(bad[0])
    replies["TaiwanStockFinancialStatements"] = httpx.Response(
        200, json={"status": 200, "data": bad}
    )
    blocks = query(client, "financials").json()["blocks"]
    assert blocks[0]["result"]["data"] is None
    assert blocks[1]["result"]["error"]["code"] == "FINMIND_DATA_INVALID"
    assert blocks[2]["result"]["status"] == blocks[3]["result"]["status"] == "ok"


def test_fill_uses_close_and_stops_at_next_event_or_adjustment(advanced_api):
    client, _, _ = advanced_api
    result = query(client, "fill").json()["blocks"][0]["result"]["data"][0]
    assert result["fill_date"] == "2026-07-02" and result["trading_days"] == "2"
    events = [{"date": DAY, "kind": "息", "before": "100", "reference": "90"}]
    prices = [{"date": DAY, "close": "95"}, {"date": "2026-07-02", "close": "105"}]
    interrupted = fill_progress(events, prices, [{"date": "2026-07-02"}])[0]
    assert interrupted["fill_date"] is None
    assert interrupted["progress"] == "50.0000"
    assert interrupted["boundary"] == "2026-07-02"


def test_valuation_unavailable_ratios_remain_null_not_zero():
    from datetime import date

    rows = valuations(
        [source_row(PER="0", PBR="-1", dividend_yield="0")],
        CODE,
        date(2026, 7, 1),
        date(2026, 7, 31),
    )
    assert rows[0] == {"date": DAY, "pe": None, "pb": None, "yield": "0"}


def test_etf_cash_and_short_derivatives_keep_units_and_distinct_series():
    from datetime import date

    rows = [
        source_row(
            component_stock_id="",
            component_stock_name="現金",
            asset_type="cash",
            shares="500",
            weight="2",
            market_value="500",
            currency="TWD",
        ),
        source_row(
            component_stock_id="TX",
            component_stock_name="期貨空單",
            asset_type="futures",
            shares="-3",
            weight="-2",
            market_value="-200",
            currency="TWD",
        ),
    ]
    result = etf_rows(rows, CODE, date(2026, 7, 1), date(2026, 7, 31), False)
    assert result[0]["component"] is None
    assert result[1]["shares"] == "-3"
    assert result[0]["holding_key"] != result[1]["holding_key"]


def test_etf_range_is_at_most_31_calendar_dates_and_topic_defaults_are_valid():
    assert AdvancedQuery.model_validate({"topic": "etf"}).start_date is not None
    query = AdvancedQuery.model_validate({"topic": "brokers", "end_date": "2026-07-31"})
    assert query.start_date == query.end_date
    with pytest.raises(ValueError):
        AdvancedQuery.model_validate(
            {"topic": "etf", "start_date": "2026-07-01", "end_date": "2026-08-01"}
        )


def test_generic_400_is_not_misclassified_as_paid_access(advanced_api):
    client, _, replies = advanced_api
    replies["TaiwanStockPER"] = httpx.Response(400, json={"status": 400, "msg": "Bad parameter"})
    point = query(client, "valuation").json()["blocks"][0]["result"]
    assert point["error"]["code"] == "FINMIND_UNAVAILABLE"
