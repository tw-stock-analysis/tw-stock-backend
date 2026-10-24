"""Finite per-request analysis; decimal calculations never persist market records."""

from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from app.schemas.analysis import (
    AnalysisError,
    AnalysisQuery,
    AnalysisSection,
    HistoryPoint,
    InstitutionPoint,
    InstitutionTrade,
    RevenuePoint,
    StockAnalysisResponse,
    TechnicalIndicators,
)
from app.services.errors import ServiceError
from app.services.finmind import FinMindService, Row, invalid_data
from app.services.stocks import QuoteProvider
from app.services.technical_indicators import technical_indicators

LABELS = {
    "Foreign_Investor": "外資（不含外資自營）",
    "Foreign_Dealer": "外資自營商",
    "Investment_Trust": "投信",
    "Dealer": "自營商（合計）",
    "Dealer_self": "自營商自行買賣",
    "Dealer_Hedging": "自營商避險",
}


def number(row: Row, key: str, *, integer: bool = False, signed: bool = False) -> Decimal:
    value = row.get(key)
    try:
        if not isinstance(value, str) or not value or len(value) > 40:
            raise ValueError("Missing numeric value")
        parsed = Decimal(value)
        exponent = parsed.as_tuple().exponent
        if (
            not parsed.is_finite()
            or not isinstance(exponent, int)
            or exponent < -12
            or parsed.copy_abs() > Decimal("1e30")
            or (not signed and parsed < 0)
            or (integer and parsed != parsed.to_integral_value())
        ):
            raise ValueError("Invalid numeric value")
        return parsed
    except (ValueError, InvalidOperation) as error:
        raise invalid_data() from error


def record_date(row: Row, key: str = "date") -> date:
    try:
        value = row.get(key)
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("Missing ISO date")
        return date.fromisoformat(value)
    except ValueError as error:
        raise invalid_data() from error


def validate_identity(row: Row, code: str, start: date, end: date) -> date:
    day = record_date(row)
    if row.get("stock_id") != code or not start <= day <= end:
        raise invalid_data()
    return day


def display(value: Decimal) -> str:
    return format(value, "f")


def rounded(value: Decimal) -> str:
    return display(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def month_shift(day: date, count: int) -> date:
    month = day.year * 12 + day.month - 1 + count
    return date(month // 12, month % 12 + 1, 1)


def history_points(rows: list[Row], code: str, start: date, end: date) -> list[HistoryPoint]:
    warmup = max(date(1994, 10, 1), start - timedelta(days=180))
    by_date = {}
    for row in rows:
        day = validate_identity(row, code, warmup, end)
        if day in by_date:
            raise invalid_data()
        prices = [number(row, field) for field in ("open", "max", "min", "close")]
        if (
            prices[1] < prices[2]
            or not prices[2] <= prices[3] <= prices[1]
            or (prices[3] == 0 and any(prices))
        ):
            raise invalid_data()
        by_date[day] = (
            prices,
            number(row, "Trading_Volume", integer=True),
            number(row, "Trading_money"),
        )
    ordered = sorted(by_date.items())
    available = [(day, values[0][3]) for day, values in ordered if values[0][3] > 0]
    indicators = dict(
        zip(
            (day for day, _ in available),
            technical_indicators([close for _, close in available]),
            strict=True,
        )
    )
    points = []
    for day, (prices, volume, amount) in ordered:
        if day < start:
            continue
        points.append(
            HistoryPoint(
                date=day,
                open=display(prices[0]),
                high=display(prices[1]),
                low=display(prices[2]),
                close=display(prices[3]),
                volume=display(volume),
                amount=display(amount),
                opening_reference=not prices[2] <= prices[0] <= prices[1],
                price_available=prices[3] > 0,
                **indicators.get(day, TechnicalIndicators()).model_dump(),
            )
        )
    return points


def revenue_points(rows: list[Row], code: str, end: date) -> list[RevenuePoint]:
    start, visible = month_shift(end, -60), month_shift(end, -35)
    by_month: dict[date, tuple[Row, Decimal]] = {}
    for row in rows:
        day = validate_identity(row, code, start, end)
        year, month = (
            number(row, "revenue_year", integer=True),
            number(row, "revenue_month", integer=True),
        )
        try:
            period = date(int(year), int(month), 1)
        except (ValueError, OverflowError) as error:
            raise invalid_data() from error
        if not month_shift(start, -1) <= period <= day or period in by_month:
            raise invalid_data()
        by_month[period] = row, number(row, "revenue", integer=True, signed=True)
    result = []
    with localcontext() as context:
        context.prec = 64

        def complete_sum(periods: list[date]) -> Decimal | None:
            if any(period not in by_month for period in periods):
                return None
            return sum((by_month[period][1] for period in periods), Decimal(0))

        def growth(current: Decimal | None, previous: Decimal | None) -> str | None:
            if current is None or previous is None or previous <= 0:
                return None
            return rounded((current - previous) / previous * 100)

        for period, (row, value) in sorted(by_month.items()):
            if period < visible:
                continue
            changes = []
            for offset in (-12, -1):
                previous = by_month.get(month_shift(period, offset))
                changes.append(
                    rounded((value - previous[1]) / previous[1] * 100)
                    if previous and previous[1] > 0
                    else None
                )
            year_months = [date(period.year, month, 1) for month in range(1, period.month + 1)]
            rolling_months = [month_shift(period, offset) for offset in range(-11, 1)]
            ytd = complete_sum(year_months)
            trailing12 = complete_sum(rolling_months)
            result.append(
                RevenuePoint(
                    month=period.strftime("%Y-%m"),
                    revenue=display(value),
                    yoy=changes[0],
                    mom=changes[1],
                    record_date=record_date(row),
                    recorded_at=record_date(row, "create_time") if row.get("create_time") else None,
                    ytd_revenue=display(ytd) if ytd is not None else None,
                    ytd_yoy=growth(ytd, complete_sum([month_shift(m, -12) for m in year_months])),
                    trailing12_revenue=display(trailing12) if trailing12 is not None else None,
                    trailing12_yoy=growth(
                        trailing12, complete_sum([month_shift(m, -12) for m in rolling_months])
                    ),
                )
            )
    return result


def institution_points(
    rows: list[Row], code: str, start: date, end: date
) -> list[InstitutionPoint]:
    groups: dict[date, dict[str, tuple[Decimal, Decimal]]] = defaultdict(dict)
    warmup = max(date(1994, 10, 1), start - timedelta(days=90))
    for row in rows:
        day = validate_identity(row, code, warmup, end)
        name = row.get("name")
        if not name or len(name) > 100 or name in groups[day]:
            raise invalid_data()
        groups[day][name] = number(row, "buy", integer=True), number(row, "sell", integer=True)
    days = sorted(groups)
    result = []
    with localcontext() as context:
        context.prec = 64
        for index, day in enumerate(days):
            if day < start:
                continue
            trades = []
            for name, (buy, sell) in sorted(groups[day].items()):
                totals: list[str | None] = []
                for size in (5, 20):
                    window = days[max(0, index + 1 - size) : index + 1]
                    totals.append(
                        display(
                            sum(
                                (groups[d][name][0] - groups[d][name][1] for d in window),
                                Decimal(0),
                            )
                        )
                        if len(window) == size and all(name in groups[d] for d in window)
                        else None
                    )
                trades.append(
                    InstitutionTrade(
                        name=name,
                        label=LABELS.get(name, name),
                        buy=display(buy),
                        sell=display(sell),
                        net=display(buy - sell),
                        net5=totals[0],
                        net20=totals[1],
                    )
                )
            result.append(InstitutionPoint(date=day, trades=trades))
    return result


class StockAnalysisService:
    def __init__(self, stocks: QuoteProvider, finmind: FinMindService):
        self.stocks = stocks
        self.finmind = finmind

    async def analyse(
        self,
        code: str,
        query: AnalysisQuery,
        disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> StockAnalysisResponse:
        async def check_connection() -> None:
            if disconnected is not None and await disconnected():
                raise ServiceError("REQUEST_CANCELLED", "查詢已取消。", 499)

        await check_connection()
        # The route supplies the identity provider; no persisted stock master.
        identity = await self.stocks.quote(code)
        start, end = query.start_date, query.end_date
        assert start is not None  # AnalysisQuery fills and validates this before any source call.
        history: AnalysisSection[HistoryPoint] = AnalysisSection(status="not_requested")
        revenue: AnalysisSection[RevenuePoint] = AnalysisSection(status="not_requested")
        institutions: AnalysisSection[InstitutionPoint] = AnalysisSection(status="not_requested")
        specs: list[
            tuple[
                str,
                date,
                AnalysisSection[HistoryPoint]
                | AnalysisSection[RevenuePoint]
                | AnalysisSection[InstitutionPoint],
            ]
        ] = [
            ("TaiwanStockPrice", max(date(1994, 10, 1), start - timedelta(days=180)), history),
            ("TaiwanStockMonthRevenue", month_shift(end, -60), revenue),
            (
                "TaiwanStockInstitutionalInvestorsBuySell",
                max(date(1994, 10, 1), start - timedelta(days=90)),
                institutions,
            ),
        ]
        blocked: AnalysisError | None = None
        for dataset, source_start, section in specs:
            await check_connection()
            if blocked:
                section.error = blocked
                continue
            try:
                rows, meta = await self.finmind.fetch(dataset, code, source_start, end)
                if dataset == "TaiwanStockPrice":
                    history.data = history_points(rows, code, start, end)
                    meta.price_basis = "unadjusted"
                    meta.units = {
                        "price": "TWD",
                        "volume": "share",
                        "amount": "TWD",
                        "macd": "TWD",
                        "bollinger": "TWD",
                        "rsi14": "percent",
                    }
                elif dataset == "TaiwanStockMonthRevenue":
                    revenue.data = revenue_points(rows, code, end)
                    meta.units = {"revenue": "TWD", "growth": "percent"}
                else:
                    institutions.data = institution_points(rows, code, start, end)
                    meta.units = {"buy": "share", "sell": "share", "net": "share"}
                section.meta = meta
                section.status = "ok"
            except ServiceError as error:
                section.status = "error"
                section.error = AnalysisError(code=error.code, message=error.message)
                if error.status_code == 503:
                    # Auth, quota and network failures apply to the remaining datasets too.
                    blocked = section.error
        return StockAnalysisResponse(
            code=code,
            name=identity.data.name,
            start_date=start,
            end_date=end,
            revenue_start_month=month_shift(end, -35).strftime("%Y-%m"),
            complete=all(section.status == "ok" for _, _, section in specs),
            identity_source=identity.meta,
            history=history,
            revenue=revenue,
            institutions=institutions,
        )
