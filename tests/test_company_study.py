"""Composition must be sequential, free-source only and preserve financial meaning."""

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.analysis import FinMindMeta
from app.schemas.company_study import CompanyStudy, StudyQuery
from app.schemas.stocks import StockQuoteResponse, StockValuationResponse
from app.services.company_study import CompanyStudyService, health
from app.services.errors import ServiceError

CODE = "2330"
END = date(2026, 7, 31)


def quote():
    return StockQuoteResponse.model_validate(
        {
            "data": {
                "code": CODE,
                "name": "測試公司",
                "closing_price": "100",
                "change": "-2",
                "date": "1150731",
            },
            "meta": {
                "source_url": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
                "retrieved_at": datetime.now(UTC),
            },
        }
    )


class Source:
    def __init__(self, industry="半導體業", fail=None):
        self.calls = []
        self.industry, self.fail = industry, fail
        self.active = False

    async def catalogue(self, dataset):
        self.calls.append(dataset)
        return [{"stock_id": CODE, "type": "twse", "industry_category": self.industry}], None

    async def fetch(self, dataset, code, start, end):
        assert not self.active
        self.active = True
        self.calls.append(dataset)
        await asyncio.sleep(0)
        self.active = False
        if dataset == self.fail:
            raise ServiceError("FINMIND_ACCESS_DENIED", "本次無權限。", 503)
        return [], FinMindMeta(
            dataset=dataset,
            retrieved_at=datetime.now(UTC),
            query_start=start,
            query_end=end,
            row_count=0,
            units={},
        )


def harness(industry="半導體業", fail=None):
    source = Source(industry, fail)
    stocks = SimpleNamespace(
        quote=AsyncMock(return_value=quote()),
        valuation=AsyncMock(return_value=StockValuationResponse(data=None, meta=quote().meta)),
    )
    notices = SimpleNamespace(
        risks=AsyncMock(side_effect=ServiceError("SOURCE_UNAVAILABLE", "公告不可用。", 503)),
        calendar=AsyncMock(side_effect=ServiceError("SOURCE_UNAVAILABLE", "日曆不可用。", 503)),
    )
    return CompanyStudyService(stocks, source, notices), source, stocks, notices


async def test_financial_view_uses_each_free_source_once_and_no_quotes_database():
    service, source, stocks, _ = harness()
    result = await service.research(CODE, StudyQuery(view="financial", end_date=END))
    assert result.complete
    assert source.calls == [
        "TaiwanStockInfo",
        "TaiwanStockMonthRevenue",
        "TaiwanStockFinancialStatements",
        "TaiwanStockBalanceSheet",
        "TaiwanStockCashFlowsStatement",
        "TaiwanStockDividend",
    ]
    assert stocks.quote.await_count == 1 and stocks.valuation.await_count == 0
    assert all(metric.value is None for metric in result.metrics)
    assert all(metric.status == "insufficient" for metric in result.metrics)
    assert result.change_percent == "-1.9608"


async def test_optional_notices_do_not_discard_successful_public_quote():
    service, source, _, notices = harness()
    result = await service.research(CODE, StudyQuery(end_date=END))
    assert result.quote.data.closing_price == "100" and not result.complete
    assert result.history.status == "ok"
    assert notices.risks.await_count == notices.calendar.await_count == 1
    assert any("公告不可用" in text for text in result.notes)
    assert len(source.calls) == len(set(source.calls))
    assert not any(
        "Adj" in call or "Holding" in call or "TradingDailyReport" in call for call in source.calls
    )


async def test_permission_failure_stops_later_finmind_calls_and_marks_unavailable():
    service, source, _, _ = harness(fail="TaiwanStockFinancialStatements")
    result = await service.research(CODE, StudyQuery(view="financial", end_date=END))
    assert source.calls[-1] == "TaiwanStockFinancialStatements"
    assert not result.complete
    assert all(block.result.status == "error" for block in result.blocks)
    assert next(m for m in result.metrics if m.key == "eps").status == "unavailable"


async def test_etf_does_not_request_company_financials():
    service, source, _, _ = harness(industry="ETF")
    result = await service.research(CODE, StudyQuery(view="financial", end_date=END))
    assert source.calls == ["TaiwanStockInfo", "TaiwanStockDividend"]
    assert all(m.status == "not_applicable" for m in result.metrics if m.key != "cash_dividend")


async def test_notes_view_fetches_only_identity_quote_and_no_private_data():
    service, source, _, notices = harness()
    result = await service.research(CODE, StudyQuery(view="notes", end_date=END))
    assert source.calls == [] and notices.risks.await_count == 0
    assert result.complete and result.metrics == []


async def test_cancelled_request_stops_before_source_call():
    service, source, stocks, _ = harness()
    with pytest.raises(ServiceError, match="查詢已取消"):
        await service.research(CODE, StudyQuery(end_date=END), AsyncMock(return_value=True))
    assert not source.calls and stocks.quote.await_count == 0


async def test_budget_timeout_keeps_quote_and_never_claims_complete(monkeypatch):
    service, _, _, _ = harness()
    real_timeout = asyncio.timeout
    monkeypatch.setattr("app.services.company_study.asyncio.timeout", lambda _: real_timeout(0))
    result = await service.research(CODE, StudyQuery(end_date=END))
    assert not result.complete and result.quote.data.code == CODE
    assert any("時間上限" in note for note in result.notes)


def test_health_uses_latest_same_period_keeps_negative_eps_and_ytd_cashflow():
    study = CompanyStudy(
        code=CODE,
        name="測試",
        view="financial",
        start_date=END,
        end_date=END,
        quote=quote(),
        industry="半導體業",
        security_kind="company",
    )
    from app.services.advanced_research import block

    for key, values in {
        "income": [
            ("2026-03-31", "Revenue", "1000"),
            ("2026-06-30", "EPS", "-1.2"),
            ("2026-06-30", "Revenue", "200"),
            ("2026-06-30", "GrossProfit", "80"),
            ("2026-03-31", "OperatingIncome", "400"),
        ],
        "balance": [("2026-06-30", "TotalAssets", "1000"), ("2026-06-30", "Liabilities", "350")],
        "cashflow": [("2026-06-30", "CashFlowsFromOperatingActivities", "-50")],
    }.items():
        item = block(key)
        item.result.status = "ok"
        item.result.data = [
            {"date": day, "subject": subject, "value": value} for day, subject, value in values
        ]
        study.blocks.append(item)
    values = {m.key: m for m in health(study)}
    assert values["eps"].value == "-1.2"
    assert values["gross_margin"].value == "40.0000"
    assert values["operating_margin"].value is None
    assert values["debt_ratio"].value == "35.0000"
    assert values["operating_cashflow"].value == "-50"
    assert "年初累計" in values["operating_cashflow"].label
    study.industry = "金融保險業"
    assert next(m for m in health(study) if m.key == "gross_margin").status == "not_applicable"
    balance = study.blocks[1]
    balance.result.data[0]["value"] = "0"
    assert next(m for m in health(study) if m.key == "debt_ratio").value is None


async def test_multiple_compatible_categories_are_retained():
    service, source, _, _ = harness()
    source.catalogue = AsyncMock(
        return_value=(
            [
                {"stock_id": CODE, "type": "twse", "industry_category": "半導體業"},
                {"stock_id": CODE, "type": "twse", "industry_category": "電子工業"},
            ],
            None,
        )
    )
    result = await service.research(CODE, StudyQuery(view="financial", end_date=END))
    assert result.security_kind == "company"
    assert set(result.industry.split("／")) == {"半導體業", "電子工業"}


async def test_notice_cancellation_stops_before_remaining_official_requests():
    from app.services.notices import NoticeService

    twse = SimpleNamespace(
        fetch=AsyncMock(
            return_value=SimpleNamespace(
                data=[], meta=SimpleNamespace(retrieved_at=datetime.now(UTC))
            )
        )
    )
    disconnected = AsyncMock(side_effect=[False, True])
    notices = NoticeService(twse, disconnected)
    with pytest.raises(ServiceError) as error:
        await notices.fetch_sources(
            ["attention-securities", "disposition-securities"], calendar=False
        )
    assert error.value.code == "REQUEST_CANCELLED"
    assert twse.fetch.await_count == 1
