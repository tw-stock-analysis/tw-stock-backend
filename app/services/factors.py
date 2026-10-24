"""Sequential, bounded candidate screening; missing results never count as matches."""

import asyncio
from datetime import timedelta
from decimal import Decimal, localcontext

from app.schemas.lab import FactorQuery
from app.services.errors import ServiceError
from app.services.finmind import FinMindService
from app.services.instruments import InstrumentService
from app.services.research_transforms import financial_rows
from app.services.stock_analysis import institution_points, revenue_points

METRICS = (
    "revenue_yoy",
    "gross_margin",
    "operating_margin",
    "debt_ratio",
    "foreign_streak",
    "trust_streak",
)
RULES = {
    "min_revenue_yoy": "revenue_yoy",
    "min_gross_margin": "gross_margin",
    "min_operating_margin": "operating_margin",
    "max_debt_ratio": "debt_ratio",
    "min_foreign_streak": "foreign_streak",
    "min_trust_streak": "trust_streak",
}


async def screen(query: FactorQuery, source: FinMindService) -> dict:
    required = {metric for rule, metric in RULES.items() if getattr(query.rules, rule) is not None}
    if query.sort != "code":
        required.add(query.sort)
    rows = [
        {
            **ref.model_dump(),
            "name": None,
            "status": "not_requested",
            "matches": None,
            "metrics": {key: None for key in METRICS},
            "dates": {},
            "sources": [],
            "error": None,
        }
        for ref in query.instruments
    ]
    try:
        async with asyncio.timeout(50):
            catalogue = await InstrumentService(source).catalogue()
            for ref, row in zip(query.instruments, rows, strict=True):
                try:
                    item = InstrumentService.resolve(catalogue, ref, query.as_of)
                    row["name"] = item.name
                    metrics, dates, sources = row["metrics"], row["dates"], row["sources"]

                    async def fetch(dataset, days):
                        start = query.as_of - timedelta(days=days)
                        data, meta = await source.fetch(dataset, ref.code, start, query.as_of)
                        sources.append(meta.model_dump(mode="json"))
                        return data, start

                    if "revenue_yoy" in required:
                        data, _ = await fetch("TaiwanStockMonthRevenue", 850)
                        revenue = revenue_points(data, ref.code, query.as_of)
                        if revenue:
                            latest_revenue = revenue[-1]
                            previous_month = (
                                str(int(latest_revenue.month[:4]) - 1) + latest_revenue.month[4:]
                            )
                            previous = next((p for p in revenue if p.month == previous_month), None)
                            if previous is not None and Decimal(previous.revenue) > 0:
                                with localcontext() as context:
                                    context.prec = 64
                                    metrics["revenue_yoy"] = format(
                                        (
                                            Decimal(latest_revenue.revenue)
                                            / Decimal(previous.revenue)
                                            - 1
                                        )
                                        * 100,
                                        "f",
                                    )
                            dates["revenue"] = latest_revenue.month
                    if required & {"gross_margin", "operating_margin"}:
                        data, start = await fetch("TaiwanStockFinancialStatements", 730)
                        income = financial_rows(data, ref.code, start, query.as_of, "income")
                        if income:
                            latest_income = max(p["date"] or "" for p in income)
                            subjects = {
                                p["subject"]: p["value"]
                                for p in income
                                if p["date"] == latest_income
                            }
                            revenue_value = subjects.get("Revenue")
                            if revenue_value is not None and Decimal(revenue_value) > 0:
                                for subject, metric in (
                                    ("GrossProfit", "gross_margin"),
                                    ("OperatingIncome", "operating_margin"),
                                ):
                                    value = subjects.get(subject)
                                    if value is not None:
                                        with localcontext() as context:
                                            context.prec = 64
                                            metrics[metric] = format(
                                                Decimal(value) / Decimal(revenue_value) * 100, "f"
                                            )
                            dates["income"] = latest_income
                    if "debt_ratio" in required:
                        data, start = await fetch("TaiwanStockBalanceSheet", 730)
                        balance = financial_rows(data, ref.code, start, query.as_of, "balance")
                        if balance:
                            latest = max(p["date"] or "" for p in balance)
                            subjects = {
                                p["subject"]: p["value"] for p in balance if p["date"] == latest
                            }
                            assets, liabilities = (
                                subjects.get("TotalAssets"),
                                subjects.get("Liabilities"),
                            )
                            if (
                                assets is not None
                                and liabilities is not None
                                and Decimal(assets) > 0
                            ):
                                with localcontext() as context:
                                    context.prec = 64
                                    metrics["debt_ratio"] = format(
                                        Decimal(liabilities) / Decimal(assets) * 100, "f"
                                    )
                            dates["balance"] = latest
                    if required & {"foreign_streak", "trust_streak"}:
                        data, start = await fetch("TaiwanStockInstitutionalInvestorsBuySell", 90)
                        points = institution_points(data, ref.code, start, query.as_of)
                        for name, metric in (
                            ("Foreign_Investor", "foreign_streak"),
                            ("Investment_Trust", "trust_streak"),
                        ):
                            if points:
                                count = 0
                                for point in reversed(points):
                                    trade = next((t for t in point.trades if t.name == name), None)
                                    if trade is None:
                                        break
                                    if Decimal(trade.net) <= 0:
                                        break
                                    count += 1
                                if any(t.name == name for t in points[-1].trades):
                                    metrics[metric] = str(count)
                                dates[metric] = points[-1].date.isoformat()
                    missing = any(metrics[key] is None for key in required)
                    row["status"] = "insufficient" if missing else "ok"
                    row["matches"] = (
                        None
                        if missing
                        else all(
                            Decimal(metrics[metric]) <= Decimal(str(getattr(query.rules, rule)))
                            if rule.startswith("max_")
                            else Decimal(metrics[metric])
                            >= Decimal(str(getattr(query.rules, rule)))
                            for rule, metric in RULES.items()
                            if getattr(query.rules, rule) is not None
                        )
                    )
                except ServiceError as error:
                    if error.code == "REQUEST_CANCELLED":
                        raise
                    row.update(status="error", error={"code": error.code, "message": error.message})
                    if (
                        error.status_code in {429, 503}
                        or "ACCESS" in error.code
                        or "RATE" in error.code
                    ):
                        break
    except TimeoutError:
        # The in-flight row can contain partial metrics, but remains explicitly unclassified.
        pass
    if query.sort == "code":
        rows.sort(key=lambda row: (row["market"], row["code"]), reverse=query.descending)
    else:
        available = [r for r in rows if r["metrics"][query.sort] is not None]
        missing_rows = [r for r in rows if r["metrics"][query.sort] is None]
        available.sort(
            key=lambda row: Decimal(row["metrics"][query.sort]), reverse=query.descending
        )
        rows = available + missing_rows
    return {
        "rows": rows,
        "complete": all(r["status"] == "ok" for r in rows),
        "as_of": query.as_of,
        "note": (
            "僅篩選本次指定的最多 10 檔候選，非全市場掃描。缺值不列為符合；"
            "法人連買依來源可用日期，非保證連續交易日。日期為資料期間，財報可能重編，"
            "並非當日已公告資訊快照，不可直接用於歷史選股回測。"
        ),
    }
