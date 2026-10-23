"""Fresh FinMind HTTP calls only: fixed datasets, no cache, database or retries."""

import asyncio
import json
from datetime import UTC, date, datetime

import httpx

from app.schemas.analysis import FinMindMeta
from app.services.errors import ServiceError

SOURCE_URL = "https://api.finmindtrade.com/api/v4/data"
DATASETS = frozenset(
    {"TaiwanStockPrice", "TaiwanStockMonthRevenue", "TaiwanStockInstitutionalInvestorsBuySell"}
)
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
        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with (
                asyncio.timeout(30),
                self._lock,
                self.client.stream(
                    "GET",
                    SOURCE_URL,
                    params={
                        "dataset": dataset,
                        "data_id": code,
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                    },
                    headers=headers,
                    timeout=20,
                    follow_redirects=False,
                ) as response,
            ):
                if response.status_code != 200:
                    raise source_error(response.status_code)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        raise invalid_data()
            rows = decode_rows(bytes(content))
        except (httpx.HTTPError, TimeoutError) as error:
            # Source/proxy messages and request objects may contain credentials.
            raise source_error(503) from error
        return rows, FinMindMeta(
            dataset=dataset,
            retrieved_at=datetime.now(UTC),
            query_start=start,
            query_end=end,
            row_count=len(rows),
            units={},
        )
