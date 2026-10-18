"""Bounded JSON/CSV retrieval with exact native values and schema discovery."""

import asyncio
import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx

from app.schemas.open_data import DataProvider, SourceRecord
from app.services.data_catalog import Dataset
from app.services.errors import ServiceError

MAX_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 150000


def invalid_source() -> ServiceError:
    return ServiceError("SOURCE_DATA_INVALID", "Source data failed validation.", 503)


def exact_json(content: bytes) -> object:
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate source columns")
        return result

    def reject_constant(value):
        raise ValueError("Nonfinite source number")

    return json.loads(
        content.decode("utf-8-sig"),
        parse_float=str,
        parse_int=str,
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )


def parse_date(value: str | None) -> date | None:
    if not value or value.strip() in ("--", "-", "N/A"):
        return None
    value = value.strip()
    # A year or reporting month is not a full date. Keep it in source fields.
    if re.fullmatch(r"\d{3,4}[-/]\d{1,2}", value) or re.fullmatch(r"\d{3,6}", value):
        return None
    if match := re.fullmatch(r"(\d{3,4})[-/年](\d{1,2})[-/月](\d{1,2})日?", value):
        year, month, day = map(int, match.groups())
    elif re.fullmatch(r"\d{7,8}", value):
        year, month, day = int(value[:-4]), int(value[-4:-2]), int(value[-2:])
    else:
        raise invalid_source()
    try:
        return date(year + (1911 if year < 1911 else 0), month, day)
    except ValueError as error:
        raise invalid_source() from error


@dataclass(frozen=True)
class SourceSnapshot:
    dataset: str
    source_url: str
    records: list[SourceRecord]
    data_as_of: date | None
    retrieved_at: datetime
    data_version: str


def decode_records(dataset: Dataset, content: bytes) -> SourceSnapshot:
    rows: object
    try:
        if dataset.provider == DataProvider.TDCC:
            reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), strict=True)
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise invalid_source()
            rows = list(reader)
        else:
            # Decimal JSON numbers remain exact strings, including large financial figures.
            rows = exact_json(content)
        if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
            raise invalid_source()
        if not rows and dataset.frequency != "event":
            raise invalid_source()
        records = []
        for row in rows:
            if not isinstance(row, dict) or not row or len(row) > 300:
                raise invalid_source()
            if any(not isinstance(k, str) or not k or len(k) > 300 for k in row):
                raise invalid_source()
            # Reject nested error envelopes, CSV shape drift and nonfinite JSON values.
            if any(v is not None and not isinstance(v, str) for v in row.values()):
                raise invalid_source()
            fields = {k.strip(): v.strip() if isinstance(v, str) else None for k, v in row.items()}
            if len(fields) != len(row):
                raise invalid_source()
            symbol = next((fields[k] for k in dataset.symbol_fields if fields.get(k)), None)
            if dataset.require_symbol and (not symbol or len(symbol) > 80):
                raise invalid_source()
            date_field = next((k for k in dataset.date_fields if fields.get(k)), None)
            date_value = fields[date_field] if date_field else None
            records.append(
                SourceRecord(
                    symbol=symbol,
                    record_date=parse_date(date_value),
                    record_date_field=date_field,
                    fields=fields,
                )
            )
    except (ValueError, UnicodeError, csv.Error, TypeError) as error:
        raise invalid_source() from error
    canonical = sorted(record.model_dump_json() for record in records)
    version = hashlib.sha256("\n".join(canonical).encode()).hexdigest()
    dates = [record.record_date for record in records if record.record_date]
    return SourceSnapshot(
        dataset.key,
        dataset.url,
        records,
        max(dates) if dates else None,
        datetime.now(UTC),
        version,
    )


class OpenDataProvider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._tpex_paths: set[str] | None = None

    async def _get(self, url: str) -> bytes:
        try:
            async with (
                asyncio.timeout(30),
                self.client.stream("GET", url, timeout=20, follow_redirects=False) as response,
            ):
                response.raise_for_status()
                if response.is_redirect:
                    raise invalid_source()
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_BYTES:
                        raise invalid_source()
                return bytes(chunks)
        except (httpx.HTTPError, TimeoutError) as error:
            raise ServiceError(
                "SOURCE_UNAVAILABLE", "Cannot retrieve public source data.", 503
            ) from error

    async def fetch(self, dataset: Dataset) -> SourceSnapshot:
        if dataset.provider == DataProvider.TPEX:
            # TPEx paths have moved before. Require the live official contract to
            # advertise the route instead of falling back to guessed/scraped URLs.
            if self._tpex_paths is None:
                try:
                    spec = json.loads(
                        await self._get("https://www.tpex.org.tw/openapi/swagger.json")
                    )
                    self._tpex_paths = {
                        path
                        for path, methods in spec["paths"].items()
                        if isinstance(methods, dict) and "get" in methods
                    }
                except (ValueError, KeyError, TypeError, AttributeError) as error:
                    raise invalid_source() from error
            if "/" + dataset.path not in self._tpex_paths:
                raise ServiceError(
                    "SOURCE_CONTRACT_CHANGED",
                    "Dataset is absent from the official TPEx contract.",
                    503,
                )
        return decode_records(dataset, await self._get(dataset.url))
