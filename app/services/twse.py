"""TWSE's documented latest snapshots, not intraday or arbitrary-date APIs."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import httpx

from app.services.errors import ServiceError

MASTER_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
PRICES_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
DATASETS = {"twse_instruments": MASTER_URL, "twse_daily_prices": PRICES_URL}
MISSING = {"", "--", "---", "-", "N/A"}


class InvalidSourceData(ServiceError):
    def __init__(self):
        super().__init__("SOURCE_DATA_INVALID", "Official data failed validation.", 503)


@dataclass(frozen=True)
class OfficialSnapshot:
    instruments: list[dict]
    prices: list[dict]
    master_date: date
    prices_date: date
    master_version: str
    prices_version: str
    retrieved_at: datetime


def source_date(value: object) -> date:
    if not isinstance(value, str):
        raise InvalidSourceData()
    digits = re.sub(r"[-/]", "", value.strip())
    if not digits.isdigit() or len(digits) not in (7, 8):
        raise InvalidSourceData()
    year = int(digits[:-4]) + (1911 if len(digits) == 7 else 0)
    try:
        result = date(year, int(digits[-4:-2]), int(digits[-2:]))
    except ValueError as error:
        raise InvalidSourceData() from error
    if result > datetime.now(ZoneInfo("Asia/Taipei")).date():
        raise InvalidSourceData()
    return result


def number(
    value: object, *, signed: bool = False, scale: int = 6, integer_digits: int = 18
) -> Decimal | None:
    if not isinstance(value, str):
        raise InvalidSourceData()
    value = value.strip()
    if value in MISSING:
        return None
    if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", value):
        raise InvalidSourceData()
    try:
        result = Decimal(value.replace(",", ""))
    except InvalidOperation as error:
        raise InvalidSourceData() from error
    if (
        not result.is_finite()
        or (not signed and result < 0)
        or result.as_tuple().exponent < -scale
        or abs(result) >= Decimal(10) ** integer_digits
    ):
        raise InvalidSourceData()
    return result


def digest(rows: list[dict]) -> str:
    # Order changes alone do not create a new data version.
    canonical = sorted(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
    return hashlib.sha256("\n".join(canonical).encode()).hexdigest()


def parse_snapshot(master: object, prices: object) -> OfficialSnapshot:
    if not isinstance(master, list) or not master or not isinstance(prices, list) or not prices:
        raise InvalidSourceData()
    try:
        instruments = []
        master_dates = set()
        codes = set()
        for row in master:
            code = row["公司代號"].strip()
            name = row["公司簡稱"].strip()
            if name.upper().endswith("-DR"):
                continue
            if not re.fullmatch(r"\d{4}", code) or code in codes:
                raise InvalidSourceData()
            company = row["公司名稱"].strip()
            if not name or len(name) > 100 or not company or len(company) > 200:
                raise InvalidSourceData()
            industry = row["產業別"].strip()
            if len(industry) > 8:
                raise InvalidSourceData()
            codes.add(code)
            master_dates.add(source_date(row["出表日期"]))
            instruments.append(
                {
                    "ticker": code,
                    "name": name,
                    "company_name": company,
                    "industry_code": industry or None,
                    "raw": row,
                }
            )

        daily = []
        price_codes = set()
        price_dates = set()
        for row in prices:
            code = row["Code"].strip()
            # The price feed also includes funds etc.; only import company master members.
            if code not in codes:
                continue
            if code in price_codes:
                raise InvalidSourceData()
            price_codes.add(code)
            trade_date = source_date(row["Date"])
            price_dates.add(trade_date)
            values = {
                name: number(row[key])
                for name, key in (
                    ("open", "OpeningPrice"),
                    ("high", "HighestPrice"),
                    ("low", "LowestPrice"),
                    ("close", "ClosingPrice"),
                )
            }
            high, low = values["high"], values["low"]
            if high is not None and low is not None:
                if high < low or any(
                    v is not None and not low <= v <= high for v in values.values()
                ):
                    raise InvalidSourceData()
            count = number(row["Transaction"], scale=0, integer_digits=9)
            change_value = row["Change"].strip()
            if change_value.startswith("X"):
                # Official marker for a non-comparable change; not a numeric zero.
                number(change_value[1:], signed=True)
                change = None
            else:
                change = number(change_value, signed=True)
            daily.append(
                {
                    "ticker": code,
                    "trade_date": trade_date,
                    **values,
                    "volume_shares": number(row["TradeVolume"], scale=0, integer_digits=24),
                    "turnover": number(row["TradeValue"], scale=0, integer_digits=30),
                    "transactions": int(count) if count is not None else None,
                    "change": change,
                    "raw": row,
                }
            )
        if len(master_dates) != 1 or len(price_dates) != 1 or not daily:
            raise InvalidSourceData()
    except (KeyError, TypeError, AttributeError) as error:
        raise InvalidSourceData() from error
    return OfficialSnapshot(
        instruments,
        daily,
        master_dates.pop(),
        price_dates.pop(),
        digest(master),
        digest(prices),
        datetime.now(UTC),
    )


class TWSEProvider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self) -> OfficialSnapshot:
        try:
            # Low concurrency and explicit timeout; never retry aggressively or bypass proxies.
            master = await self.client.get(MASTER_URL, timeout=15)
            master.raise_for_status()
            prices = await self.client.get(PRICES_URL, timeout=15)
            prices.raise_for_status()
            return parse_snapshot(master.json(), prices.json())
        except httpx.HTTPError as error:
            raise ServiceError(
                "SOURCE_UNAVAILABLE",
                "Cannot retrieve official TWSE data.",
                503,
            ) from error
        except ValueError as error:
            raise InvalidSourceData() from error
