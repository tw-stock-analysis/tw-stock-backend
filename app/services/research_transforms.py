"""Validated, per-request research transformations with explicit missing-value semantics."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, localcontext

from app.services.finmind import Row, invalid_data
from app.services.stock_analysis import display, number, record_date, rounded, validate_identity


def text(row: Row, key: str) -> str:
    value = row.get(key)
    if not value:
        raise invalid_data()
    return value


def optional_date(row: Row, key: str) -> str | None:
    return record_date(row, key).isoformat() if row.get(key) else None


def checked(rows: list[Row], code: str, start: date, end: date, keys: tuple[str, ...] = ()):
    seen = set()
    for row in rows:
        day = validate_identity(row, code, start, end)
        identity = (day, *(text(row, key) for key in keys))
        if identity in seen:
            raise invalid_data()
        seen.add(identity)
        yield row


def valuations(rows: list[Row], code: str, start: date, end: date) -> list[Row]:
    result = []
    for row in checked(rows, code, start, end):
        point: Row = {"date": row["date"]}
        for source, key in (("PER", "pe"), ("PBR", "pb"), ("dividend_yield", "yield")):
            value = (
                number(row, source, signed=True)
                if row.get(source) not in (None, "", "--")
                else None
            )
            point[key] = (
                display(value)
                if value is not None and (value >= 0 if key == "yield" else value > 0)
                else None
            )
        result.append(point)
    return sorted(result, key=lambda r: r["date"] or "")


def dividends(rows: list[Row], code: str, start: date, end: date) -> list[Row]:
    result = []
    seen = set()
    for row in rows:
        validate_identity(row, code, start, end)
        identity = tuple(
            row.get(k)
            for k in ("date", "year", "CashExDividendTradingDate", "StockExDividendTradingDate")
        )
        if identity in seen:
            raise invalid_data()
        seen.add(identity)
        result.append(
            {
                "date": row["date"],
                "year": text(row, "year"),
                "cash": display(
                    number(row, "CashEarningsDistribution") + number(row, "CashStatutorySurplus")
                ),
                "stock": display(
                    number(row, "StockEarningsDistribution") + number(row, "StockStatutorySurplus")
                ),
                "cash_ex_date": optional_date(row, "CashExDividendTradingDate"),
                "stock_ex_date": optional_date(row, "StockExDividendTradingDate"),
                "payment_date": optional_date(row, "CashDividendPaymentDate"),
                "announcement_date": optional_date(row, "AnnouncementDate"),
            }
        )
    return sorted(result, key=lambda r: (r["date"] or "", r["year"] or ""))


def financial_rows(rows: list[Row], code: str, start: date, end: date, kind: str) -> list[Row]:
    indexed = {}
    for row in checked(rows, code, start, end, ("type",)):
        day = record_date(row)
        if (day.month, day.day) not in ((3, 31), (6, 30), (9, 30), (12, 31)):
            raise invalid_data()
        indexed[(day, text(row, "type"))] = (row, number(row, "value", signed=True))
    result = []
    for (day, subject), (row, value) in sorted(indexed.items()):
        unit = "元／股" if subject == "EPS" else "%" if subject.endswith("_per") else "元"
        quarter = None
        if kind == "cashflow" and unit == "元":
            previous_day = date(day.year, day.month - 2, 1) - timedelta(days=1)
            previous = indexed.get((previous_day, subject))
            quarter = value if day.month == 3 else value - previous[1] if previous else None
        result.append(
            {
                "date": day.isoformat(),
                "subject": subject,
                "label": text(row, "origin_name"),
                "value": display(value),
                "unit": unit,
                "basis": "期末存量"
                if kind == "balance"
                else "年初累計"
                if kind == "cashflow"
                else "來源單期",
                "quarter_value": display(quarter) if quarter is not None else None,
            }
        )
    return result


def earnings(rows: list[Row]) -> list[Row]:
    by_date: dict[str, dict[str, Row]] = defaultdict(dict)
    for row in rows:
        by_date[text(row, "date")][text(row, "subject")] = row
    result = []
    for day, subjects in sorted(by_date.items()):
        point: Row = {"date": day}
        for source, target in (
            ("EPS", "eps"),
            ("Revenue", "revenue"),
            ("IncomeAfterTaxes", "net_income"),
        ):
            point[target] = subjects.get(source, {}).get("value")
        with localcontext() as context:
            context.prec = 64
            revenue = Decimal(point["revenue"]) if point["revenue"] is not None else None
            for source, target in (
                ("GrossProfit", "gross_margin"),
                ("OperatingIncome", "operating_margin"),
            ):
                value = subjects.get(source, {}).get("value")
                point[target] = (
                    rounded(Decimal(value) / revenue * 100)
                    if (value is not None and revenue is not None and revenue > 0)
                    else None
                )
        result.append(point)
    return result


def price_rows(rows: list[Row], code: str, start: date, end: date) -> list[Row]:
    result = []
    for row in checked(rows, code, start, end):
        prices = [number(row, field) for field in ("open", "max", "min", "close")]
        if prices[1] < prices[2] or not prices[2] <= prices[3] <= prices[1]:
            raise invalid_data()
        result.append({"date": row["date"], "close": display(prices[3]) if prices[3] > 0 else None})
    return sorted(result, key=lambda r: r["date"] or "")


def ownership(rows: list[Row], code: str, start: date, end: date) -> list[Row]:
    result = []
    for row in checked(rows, code, start, end, ("HoldingSharesLevel",)):
        percent = number(row, "percent")
        if percent > 100:
            raise invalid_data()
        result.append(
            {
                "date": row["date"],
                "level": text(row, "HoldingSharesLevel"),
                "people": display(number(row, "people", integer=True)),
                "shares": display(number(row, "unit", integer=True)),
                "percent": display(percent),
            }
        )
    return sorted(result, key=lambda r: (r["date"] or "", r["level"] or ""))


def brokers(rows: list[Row], code: str, start: date, end: date) -> list[Row]:
    groups: dict[str, Row] = {}
    with localcontext() as context:
        context.prec = 64
        for row in checked(rows, code, start, end, ("securities_trader_id", "price")):
            broker_id, name = text(row, "securities_trader_id"), text(row, "securities_trader")
            if broker_id not in groups:
                groups[broker_id] = {
                    "date": row["date"],
                    "broker_id": broker_id,
                    "broker": name,
                    "buy": "0",
                    "sell": "0",
                }
            target = groups[broker_id]
            if target["broker"] != name:
                raise invalid_data()
            for key in ("buy", "sell"):
                target[key] = display(Decimal(target[key] or "0") + number(row, key, integer=True))
            number(row, "price")
        for target in groups.values():
            target["net"] = display(Decimal(target["buy"] or "0") - Decimal(target["sell"] or "0"))
    return sorted(groups.values(), key=lambda r: (-Decimal(r["net"] or "0"), r["broker_id"] or ""))


def etf_rows(rows: list[Row], code: str, start: date, end: date, change: bool) -> list[Row]:
    result = []
    seen = set()
    for row in rows:
        validate_identity(row, code, start, end)
        name = text(row, "component_stock_name")
        asset_type = row.get("asset_type") if change else text(row, "asset_type")
        component = row.get("component_stock_id") or None
        if not component and (change or asset_type in ("stock", "etf", "bond")):
            raise invalid_data()
        identity = (row["date"], component, name, asset_type)
        if identity in seen:
            raise invalid_data()
        seen.add(identity)
        point: Row = {
            "date": row["date"],
            "component": component,
            "name": name,
            "asset_type": asset_type,
        }
        if change:
            buy, sell = number(row, "buy", integer=True), number(row, "sell", integer=True)
            point.update(buy=display(buy), sell=display(sell), net=display(buy - sell))
        else:
            point.update(
                holding_key=f"{asset_type}|{component or name}|{text(row, 'currency')}",
                shares=display(number(row, "shares", integer=True, signed=True)),
                weight=display(number(row, "weight", signed=True)),
                # A source zero market value means undisclosed, not a worthless holding.
                market_value=display(number(row, "market_value", signed=True))
                if number(row, "market_value", signed=True) != 0
                else None,
                currency=text(row, "currency"),
            )
        result.append(point)
    return sorted(
        result, key=lambda r: (r["date"] or "", r["component"] or "", r["asset_type"] or "")
    )


def event_rows(rows: list[Row], code: str, start: date, end: date) -> list[Row]:
    result = []
    for row in checked(rows, code, start, end):
        result.append(
            {
                "date": row["date"],
                "kind": text(row, "stock_or_cache_dividend"),
                "before": display(number(row, "before_price")),
                "reference": display(number(row, "reference_price")),
                "dividend": display(number(row, "stock_and_cache_dividend")),
            }
        )
    return sorted(result, key=lambda r: r["date"] or "")


def action_dates(rows: list[Row], code: str, start: date, end: date) -> list[Row]:
    # These event APIs may return a whole market snapshot; retain only this code/range.
    result: list[Row] = []
    for row in rows:
        text(row, "stock_id")
        day = record_date(row)
        if row.get("stock_id") != code:
            continue
        if start <= day <= end:
            result.append({"date": day.isoformat()})
    return result


def fill_progress(events: list[Row], prices: list[Row], actions: list[Row]) -> list[Row]:
    result = []
    with localcontext() as context:
        context.prec = 64
        for index, event in enumerate(events):
            day = text(event, "date")
            boundaries = [text(e, "date") for e in actions if text(e, "date") > day]
            if index + 1 < len(events):
                boundaries.append(text(events[index + 1], "date"))
            boundary = min(boundaries) if boundaries else None
            window = [
                p
                for p in prices
                if text(p, "date") >= day
                and (boundary is None or text(p, "date") < boundary)
                and p.get("close") is not None
            ]
            point: Row = {
                **event,
                "fill_date": None,
                "trading_days": None,
                "progress": None,
                "observed_until": window[-1]["date"] if window else None,
                "boundary": boundary,
            }
            before, reference = Decimal(text(event, "before")), Decimal(text(event, "reference"))
            if event["kind"] not in ("息", "除息") or before <= reference or reference <= 0:
                point["status"] = "不適用：含除權或價格基準不可比較"
            elif any(text(e, "date") == day for e in actions):
                point["status"] = "不適用：當日另有價格調整事件"
            elif not window:
                point["status"] = "觀察區間無有效收盤價"
            else:
                point["progress"] = rounded(
                    (Decimal(text(window[-1], "close")) - reference) / (before - reference) * 100
                )
                hit = next(
                    ((i, p) for i, p in enumerate(window) if Decimal(text(p, "close")) >= before),
                    None,
                )
                point["status"] = "已收盤填息" if hit else "觀察區間未收盤填息"
                if hit:
                    point["trading_days"], point["fill_date"] = str(hit[0] + 1), hit[1]["date"]
            result.append(point)
    return result
