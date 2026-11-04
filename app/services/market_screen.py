"""Full catalogue screening from whole-market snapshots; no per-stock fallback or storage."""

import asyncio
from datetime import date
from decimal import Decimal, localcontext
from typing import Any

from app.schemas.market_screen import MarketScreenQuery
from app.services.errors import ServiceError
from app.services.finmind import FinMindService, Row, invalid_data
from app.services.instruments import InstrumentService
from app.services.stock_analysis import month_shift, number, record_date

METRICS = ("revenue_yoy", "gross_margin", "operating_margin", "debt_ratio")
RULES = {
    "min_revenue_yoy": "revenue_yoy",
    "min_gross_margin": "gross_margin",
    "min_operating_margin": "operating_margin",
    "max_debt_ratio": "debt_ratio",
}


def revenue_snapshot(rows: list[Row], day: date, period: date) -> dict[str, Decimal]:
    result = {}
    for row in rows:
        code = row.get("stock_id")
        if not code or record_date(row) != day or code in result:
            raise invalid_data()
        year, month = (
            number(row, "revenue_year", integer=True),
            number(row, "revenue_month", integer=True),
        )
        if (year, month) != (period.year, period.month):
            raise invalid_data()
        result[code] = number(row, "revenue", integer=True, signed=True)
    return result


def financial_snapshot(
    rows: list[Row], day: date, subjects: set[str]
) -> dict[str, dict[str, Decimal]]:
    result: dict[str, dict[str, Decimal]] = {}
    seen = set()
    for row in rows:
        code, subject = row.get("stock_id"), row.get("type")
        if not code or not subject or record_date(row) != day or (code, subject) in seen:
            raise invalid_data()
        seen.add((code, subject))
        if subject in subjects:
            result.setdefault(code, {})[subject] = number(row, "value", signed=True)
    return result


async def market_screen(query: MarketScreenQuery, source: FinMindService) -> dict:
    try:
        async with asyncio.timeout(50):
            catalogue = await InstrumentService(source).catalogue()
            instruments = [i for i in catalogue.data if i.market in query.markets]
            if not instruments:
                raise ServiceError(
                    "MARKET_UNIVERSE_EMPTY", "來源名錄未提供所選市場，未推定為掃描完成。", 502
                )
            required = {
                metric for rule, metric in RULES.items() if getattr(query.rules, rule) is not None
            }
            if query.sort != "code":
                required.add(query.sort)
            sources = [meta.model_dump(mode="json") for meta in catalogue.sources]
            current_revenue: dict[str, Decimal] = {}
            previous_revenue: dict[str, Decimal] = {}
            income: dict[str, dict[str, Decimal]] = {}
            balance: dict[str, dict[str, Decimal]] = {}
            if "revenue_yoy" in required:
                for period in (query.revenue_month, month_shift(query.revenue_month, -12)):
                    day = month_shift(period, 1)
                    data, meta = await source.snapshot("TaiwanStockMonthRevenue", day)
                    values = revenue_snapshot(data, day, period)
                    del data
                    sources.append(meta.model_dump(mode="json"))
                    if period == query.revenue_month:
                        current_revenue = values
                    else:
                        previous_revenue = values
            if required & {"gross_margin", "operating_margin"}:
                data, meta = await source.snapshot(
                    "TaiwanStockFinancialStatements", query.report_date
                )
                income = financial_snapshot(
                    data, query.report_date, {"Revenue", "GrossProfit", "OperatingIncome"}
                )
                del data
                sources.append(meta.model_dump(mode="json"))
            if "debt_ratio" in required:
                data, meta = await source.snapshot("TaiwanStockBalanceSheet", query.report_date)
                balance = financial_snapshot(
                    data, query.report_date, {"TotalAssets", "Liabilities"}
                )
                del data
                sources.append(meta.model_dump(mode="json"))
            rows: list[dict[str, Any]] = []
            with localcontext() as context:
                context.prec = 64
                for instrument in instruments:
                    metrics: dict[str, str | None] = {key: None for key in METRICS}
                    code = instrument.code
                    if code in current_revenue and previous_revenue.get(code, Decimal(0)) > 0:
                        metrics["revenue_yoy"] = format(
                            (current_revenue[code] / previous_revenue[code] - 1) * 100, "f"
                        )
                    statement = income.get(code, {})
                    if statement.get("Revenue", Decimal(0)) > 0:
                        for subject, key in (
                            ("GrossProfit", "gross_margin"),
                            ("OperatingIncome", "operating_margin"),
                        ):
                            if subject in statement:
                                metrics[key] = format(
                                    statement[subject] / statement["Revenue"] * 100, "f"
                                )
                    statement = balance.get(code, {})
                    if (
                        statement.get("TotalAssets", Decimal(0)) > 0
                        and "Liabilities" in statement
                        and statement["Liabilities"] >= 0
                    ):
                        metrics["debt_ratio"] = format(
                            statement["Liabilities"] / statement["TotalAssets"] * 100, "f"
                        )
                    ambiguous = instrument.historical_identity == "ambiguous"
                    if ambiguous:
                        metrics = {key: None for key in METRICS}
                    missing = any(metrics[key] is None for key in required)
                    matches = None
                    status = "error" if ambiguous else "insufficient" if missing else "ok"
                    if status == "ok":
                        matches = all(
                            Decimal(metrics[metric] or "0") <= getattr(query.rules, rule)
                            if rule.startswith("max_")
                            else Decimal(metrics[metric] or "0") >= getattr(query.rules, rule)
                            for rule, metric in RULES.items()
                            if getattr(query.rules, rule) is not None
                        )
                    rows.append(
                        {
                            "market": instrument.market,
                            "code": code,
                            "name": instrument.name,
                            "status": status,
                            "matches": matches,
                            "metrics": metrics,
                            "dates": {
                                "revenue_month": query.revenue_month.isoformat(),
                                "report_date": query.report_date.isoformat(),
                            },
                            "error": {
                                "code": "INSTRUMENT_IDENTITY_AMBIGUOUS",
                                "message": "來源名錄身分衝突，未推定符合條件。",
                            }
                            if ambiguous
                            else None,
                        }
                    )
            coverage = {
                "universe": len(rows),
                "matched": sum(r["matches"] is True for r in rows),
                "not_matched": sum(r["matches"] is False for r in rows),
                "insufficient": sum(r["status"] == "insufficient" for r in rows),
                "errors": sum(r["status"] == "error" for r in rows),
                "complete": all(r["status"] == "ok" for r in rows),
            }
            selected = [
                r
                for r in rows
                if query.result_filter == "all"
                or (query.result_filter == "matched" and r["matches"] is True)
                or (query.result_filter == "insufficient" and r["matches"] is None)
            ]
            if query.sort == "code":
                selected.sort(key=lambda r: (r["market"], r["code"]), reverse=query.descending)
            else:
                available = [r for r in selected if r["metrics"][query.sort] is not None]
                missing_rows = [r for r in selected if r["metrics"][query.sort] is None]
                available.sort(
                    key=lambda r: (Decimal(r["metrics"][query.sort]), r["code"]),
                    reverse=query.descending,
                )
                selected = available + missing_rows
            offset = (query.page - 1) * query.limit
            return {
                "rows": selected[offset : offset + query.limit],
                "pagination": {"page": query.page, "limit": query.limit, "total": len(selected)},
                "coverage": coverage,
                "sources": sources,
                "notes": [
                    "以整份來源名錄及全市場單期快照篩選，不逐檔繞過付費限制；每次查詢及換頁重新取得來源。",
                    (
                        "營收月份 M 使用來源 M+1 月首日紀錄；財報使用指定季末。"
                        "缺值與身分衝突不計為不符合。"
                    ),
                    "目前名錄不等於指定歷史期間的完整上市名單；財報缺公告時間且可能重編，本結果不是當日已知資訊快照，不直接用於歷史因子回測。",
                ],
            }
    except TimeoutError as error:
        raise ServiceError(
            "RESEARCH_TIMEOUT", "全市場來源查詢超時；未回傳不完整掃描，請手動重試。", 503
        ) from error
