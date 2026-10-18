"""On-demand TWSE retrieval. No database, cache, retry or import side effects."""

import asyncio
import json
from datetime import UTC, datetime

import httpx

from app.schemas.twse import RetrievalMeta, TradingDataset, TradingResponse
from app.services.errors import ServiceError

MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 150_000
MAX_COLUMNS = 300


def invalid_data() -> ServiceError:
    return ServiceError("SOURCE_DATA_INVALID", "TWSE 回應的資料格式無法辨識。", 502)


def decode_rows(content: bytes) -> list[dict[str, str | None]]:
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate columns")
        return result

    def reject_constant(value):
        raise ValueError("Invalid number")

    try:
        rows = json.loads(
            content.decode("utf-8-sig"),
            parse_int=str,
            parse_float=str,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise ValueError("Not a bounded array")
        for row in rows:
            if not isinstance(row, dict) or not row or len(row) > MAX_COLUMNS:
                raise ValueError("Not a bounded record")
            if any(not isinstance(k, str) or not k or len(k) > 300 for k in row):
                raise ValueError("Invalid column")
            if any(v is not None and not isinstance(v, str) for v in row.values()):
                raise ValueError("Not a flat record")
        return rows
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise invalid_data() from error


class TwseService:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self, dataset: TradingDataset) -> TradingResponse:
        # A fresh HTTP request for every call, even for consecutive identical requests.
        try:
            async with (
                asyncio.timeout(30),
                self.client.stream(
                    "GET",
                    dataset.source_url,
                    timeout=20,
                    follow_redirects=False,
                    headers={"Accept": "application/json", "Cache-Control": "no-cache"},
                ) as response,
            ):
                if response.status_code == 429:
                    raise ServiceError(
                        "SOURCE_RATE_LIMITED", "TWSE 暫時限制請求，請稍後手動重試。", 503
                    )
                if response.status_code != 200:
                    raise ServiceError("SOURCE_UNAVAILABLE", "目前無法取得 TWSE 資料。", 503)
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_BYTES:
                        raise invalid_data()
            rows = decode_rows(bytes(chunks))
        except (httpx.HTTPError, TimeoutError) as error:
            # Never expose upstream/proxy error text to the browser.
            raise ServiceError("SOURCE_UNAVAILABLE", "目前無法取得 TWSE 資料。", 503) from error
        columns = list(dict.fromkeys(key for row in rows for key in row))
        if len(columns) > MAX_COLUMNS:
            raise invalid_data()
        return TradingResponse(
            dataset=dataset,
            columns=columns,
            data=rows,
            meta=RetrievalMeta(
                source_url=dataset.source_url,
                retrieved_at=datetime.now(UTC),
                row_count=len(rows),
            ),
        )
