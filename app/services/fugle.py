"""Free-tier Fugle REST adapter; bounded cache, one shared budget and no public key exposure."""

import asyncio
import re
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.schemas.realtime import RealtimeMeta, RealtimeResponse
from app.services.errors import ServiceError
from app.services.providers.open_data import MAX_BYTES, exact_json, invalid_source

BASE_URL = "https://api.fugle.tw/marketdata/v1.0/stock/"
RESOURCES = {
    "tickers": "intraday/tickers",
    "quote": "intraday/quote/",
    "ticker": "intraday/ticker/",
    "trades": "intraday/trades/",
    "candles": "intraday/candles/",
    "volumes": "intraday/volumes/",
    "history": "historical/candles/",
    "stats": "historical/stats/",
}
MAX_CACHE_BYTES = 32 * 1024 * 1024


class FugleService:
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.settings = settings
        self.lock = asyncio.Lock()
        self.calls: deque[float] = deque()
        self.cache: OrderedDict[tuple, tuple[float, RealtimeResponse, int]] = OrderedDict()
        self.cooldown_until = 0.0

    def check_configuration(self) -> SecretStr:
        if not self.settings.fugle_api_key or not self.settings.fugle_api_key.get_secret_value():
            raise ServiceError(
                "SOURCE_NOT_CONFIGURED", "Configure FUGLE_API_KEY on the backend.", 503
            )
        if self.settings.app_env == "production" and not self.settings.fugle_display_authorized:
            raise ServiceError(
                "SOURCE_DISPLAY_NOT_AUTHORIZED",
                "Production market-data display requires an appropriate source agreement.",
                503,
            )
        return self.settings.fugle_api_key

    async def fetch(
        self, resource: str, symbol: str = "", params: dict | None = None
    ) -> RealtimeResponse:
        api_key = self.check_configuration()
        if resource != "tickers" and not re.fullmatch(r"[A-Z0-9]{2,12}", symbol):
            raise ServiceError("INVALID_SYMBOL", "Invalid market-data symbol.", 422)
        if resource not in RESOURCES:
            raise ServiceError("RESOURCE_NOT_FOUND", "Unknown market-data resource.", 404)
        params = params or {}
        key = (resource, symbol, tuple(sorted(params.items())))
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=3)
        except TimeoutError as error:
            raise ServiceError(
                "SOURCE_BUSY", "Market-data service is busy. Retry later.", 503
            ) from error
        try:
            now = time.monotonic()
            cached = self.cache.get(key)
            if cached and now - cached[0] < (5 if resource not in ("history", "stats") else 300):
                self.cache.move_to_end(key)
                return cached[1].model_copy(deep=True)
            while self.calls and now - self.calls[0] >= 60:
                self.calls.popleft()
            if len(self.calls) >= 60 or now < self.cooldown_until:
                raise ServiceError(
                    "SOURCE_RATE_LIMITED", "Source request budget exhausted. Retry later.", 503
                )
            self.calls.append(now)
            url = BASE_URL + RESOURCES[resource] + symbol
            try:
                async with (
                    asyncio.timeout(20),
                    self.client.stream(
                        "GET",
                        url,
                        params=params,
                        timeout=15,
                        follow_redirects=False,
                        headers={"X-API-KEY": api_key.get_secret_value()},
                    ) as response,
                ):
                    if response.status_code == 429:
                        self.cooldown_until = time.monotonic() + 60
                    response.raise_for_status()
                    if response.is_redirect:
                        raise invalid_source()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise invalid_source()
                payload = exact_json(bytes(body))
                if not isinstance(payload, dict):
                    raise invalid_source()
                if resource == "tickers":
                    if not isinstance(payload.get("data"), list) or any(
                        not isinstance(row, dict) or not isinstance(row.get("symbol"), str)
                        for row in payload["data"]
                    ):
                        raise invalid_source()
                elif payload.get("symbol") != symbol:
                    raise invalid_source()
                result = RealtimeResponse(
                    data=payload,
                    meta=RealtimeMeta(
                        source_url=url,
                        retrieved_at=datetime.now(UTC),
                        usage="display_authorized"
                        if self.settings.fugle_display_authorized
                        else "private_development",
                        is_trial=payload.get("isTrial"),
                    ),
                )
            except (httpx.HTTPError, TimeoutError) as error:
                raise ServiceError(
                    "SOURCE_UNAVAILABLE", "Cannot retrieve Fugle market data.", 503
                ) from error
            except (ValueError, UnicodeError, TypeError) as error:
                raise invalid_source() from error
            self.cache[key] = (time.monotonic(), result, len(body))
            self.cache.move_to_end(key)
            while (
                len(self.cache) > 256
                or sum(entry[2] for entry in self.cache.values()) > MAX_CACHE_BYTES
            ):
                self.cache.popitem(last=False)
            return result.model_copy(deep=True)
        finally:
            self.lock.release()
