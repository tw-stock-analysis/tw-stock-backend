"""Fresh FinMind HTTP calls only: fixed datasets, no cache, database or retries."""

import asyncio
import json
from datetime import UTC, date, datetime

import httpx

from app.schemas.analysis import FinMindMeta
from app.services.errors import ServiceError

SOURCE_URL = "https://api.finmindtrade.com/api/v4/data"
DATASETS = frozenset(
    {
        "TaiwanStockPrice",
        "TaiwanStockMonthRevenue",
        "TaiwanStockInstitutionalInvestorsBuySell",
        "TaiwanStockPER",
        "TaiwanStockDividend",
        "TaiwanStockDividendResult",
        "TaiwanStockFinancialStatements",
        "TaiwanStockBalanceSheet",
        "TaiwanStockCashFlowsStatement",
        "TaiwanStockPriceAdj",
        "TaiwanStockHoldingSharesPer",
        "TaiwanStockTradingDailyReport",
        "TaiwanStockActiveETFHolding",
        "TaiwanStockActiveETFHoldingChange",
        "TaiwanStockSplitPrice",
        "TaiwanStockCapitalReductionReferencePrice",
        "TaiwanStockParValueChange",
    }
)
BROKER_URL = "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report"
CATALOGUES = frozenset({"TaiwanStockInfo", "TaiwanStockDelisting"})
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 15_000
Row = dict[str, str | None]


def invalid_data() -> ServiceError:
    return ServiceError("FINMIND_DATA_INVALID", "FinMind 回應的資料格式無法辨識。", 502)


def source_error(status: int) -> ServiceError:
    if status in (402, 429):
        return ServiceError("FINMIND_RATE_LIMITED", "FinMind 額度已達上限，請稍後手動重查。", 503)
    if status in (401, 403):
        return ServiceError(
            "FINMIND_ACCESS_DENIED", "目前無法存取 FinMind，請確認來源權限與後端金鑰設定。", 503
        )
    return ServiceError("FINMIND_UNAVAILABLE", "目前無法取得 FinMind 資料。", 503)


def permission_message(value: object) -> bool:
    return isinstance(value, str) and any(
        phrase in value.lower()
        for phrase in ("please update your user level", "please upgrade your user level")
    )


def decode_rows(content: bytes) -> list[Row]:
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate keys")
        return result

    def reject_constant(value):
        raise ValueError("Non-finite JSON number")

    try:
        body = json.loads(
            content.decode("utf-8-sig"),
            parse_int=str,
            parse_float=str,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
        if not isinstance(body, dict):
            raise ValueError("Not an envelope")
        status = body.get("status")
        if status == "400" and permission_message(body.get("msg")):
            raise source_error(403)
        if status in ("401", "402", "403", "429"):
            raise source_error(int(status))
        if status != "200":
            raise ValueError("Unknown API status")
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise ValueError("Not a bounded array")
        for row in rows:
            if (
                not isinstance(row, dict)
                or not row
                or len(row) > 80
                or any(not isinstance(k, str) or not k or len(k) > 200 for k in row)
                or any(
                    v is not None and (not isinstance(v, str) or len(v) > 1000)
                    for v in row.values()
                )
            ):
                raise ValueError("Not a bounded flat record")
        return rows
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise invalid_data() from error


class FinMindService:
    def __init__(self, client: httpx.AsyncClient, token: str = ""):
        self.client = client
        self._token = token
        # Serializes this worker's FinMind calls, including different browser requests.
        self._lock = asyncio.Lock()

    async def fetch(
        self, dataset: str, code: str, start: date, end: date
    ) -> tuple[list[Row], FinMindMeta]:
        if dataset not in DATASETS:
            raise ValueError("Dataset is not allowlisted")
        broker = dataset == "TaiwanStockTradingDailyReport"
        if broker and start != end:
            raise ValueError("Broker reports accept exactly one date")
        url = BROKER_URL if broker else SOURCE_URL
        params = (
            {"data_id": code, "date": end.isoformat()}
            if broker
            else {
                "dataset": dataset,
                "data_id": code,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            }
        )
        if dataset == "TaiwanStockParValueChange":
            # This source only accepts a market-wide query; filter code in its transform.
            params.pop("data_id")
        return await self._request(dataset, params, start, end, url)

    async def catalogue(self, dataset: str) -> tuple[list[Row], FinMindMeta]:
        if dataset not in CATALOGUES:
            raise ValueError("Catalogue is not allowlisted")
        today = datetime.now(UTC).date()
        rows, meta = await self._request(dataset, {"dataset": dataset}, date(1994, 10, 1), today)
        # Catalogue calls do not send a temporal filter or establish historical coverage.
        meta.query_start, meta.query_end, meta.request_scope = None, None, "catalogue"
        return rows, meta

    async def _request(
        self,
        dataset: str,
        params: dict[str, str],
        start: date,
        end: date,
        url: str = SOURCE_URL,
    ) -> tuple[list[Row], FinMindMeta]:
        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with (
                asyncio.timeout(30),
                self._lock,
                self.client.stream(
                    "GET",
                    url,
                    params=params,
                    headers=headers,
                    timeout=20,
                    follow_redirects=False,
                ) as response,
            ):
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        raise invalid_data()
                if response.status_code != 200:
                    if response.status_code == 400:
                        try:
                            error_body = json.loads(content)
                        except (ValueError, UnicodeError, RecursionError):
                            error_body = None
                        if isinstance(error_body, dict) and permission_message(
                            error_body.get("msg")
                        ):
                            raise source_error(403)
                    raise source_error(response.status_code)
            rows = decode_rows(bytes(content))
        except (httpx.HTTPError, TimeoutError) as error:
            # Source/proxy messages and request objects may contain credentials.
            raise source_error(503) from error
        return rows, FinMindMeta(
            dataset=dataset,
            source_url=url,
            retrieved_at=datetime.now(UTC),
            query_start=start,
            query_end=end,
            row_count=len(rows),
            units={},
        )
