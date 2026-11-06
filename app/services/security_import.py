"""Trusted operator CLI for identity metadata; fixed official sources, no quote fetching.

Default is dry-run. Apply a reviewed JSON bundle or fresh TWSE metadata with --apply.
No file URL or user-controlled source endpoint is fetched.
"""

import argparse
import asyncio
import json
import re
from datetime import date
from pathlib import Path

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.schemas.analysis import taipei_today
from app.schemas.security_identity import (
    IdentityEvidence,
    IdentityImportBundle,
    ListingInput,
    SecurityImport,
)
from app.services.errors import ServiceError
from app.services.security_identity import SecurityIdentityService
from app.services.twse import decode_rows

TWSE_SOURCE = "https://openapi.twse.com.tw/v1"
TWSE_PATHS = ("/opendata/t187ap03_L", "/company/newlisting", "/company/suspendListingCsvAndHtml")
MAX_METADATA_BYTES = 8 * 1024 * 1024


def official_date(value: str | None) -> date | None:
    if not value or not value.strip():
        return None
    digits = re.sub(r"[/.-]", "", value.strip())
    try:
        if len(digits) == 8 and digits.isdecimal():
            day = date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
        elif len(digits) == 7 and digits.isdecimal():
            day = date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:]))
        else:
            return None
        return day if day >= date(1900, 1, 1) else None
    except ValueError:
        return None


def _evidence(path: str, effective: date, observed: date) -> IdentityEvidence:
    return IdentityEvidence.model_validate(
        {
            "source_name": {
                "/opendata/t187ap03_L": "TWSE 上市公司基本資料",
                "/company/newlisting": "TWSE 最近上市公司",
                "/company/suspendListingCsvAndHtml": "TWSE 終止上市公司",
            }[path],
            "source_url": TWSE_SOURCE + path,
            "observed_on": observed,
            "effective_date": effective,
        }
    )


def build_twse_bundle(
    basic: list[dict],
    newly_listed: list[dict],
    delisted: list[dict],
    observed_on: date | None = None,
) -> tuple[IdentityImportBundle, dict]:
    """Only known listing starts/termination dates; never pair issuers by UBN or infer gaps."""
    observed = observed_on or taipei_today()
    skipped = {
        "unsupported_code": 0,
        "missing_or_invalid_date": 0,
        "unpaired_history": 0,
        "conflicting_history": 0,
    }
    terminations: dict[str, list[tuple[date, str]]] = {}
    for row in delisted:
        code, name = str(row.get("Code") or "").strip(), str(row.get("Company") or "").strip()
        day = official_date(row.get("DelistingDate"))
        if not re.fullmatch(r"[0-9A-Z]{4,6}", code) or not name or day is None:
            skipped["missing_or_invalid_date"] += 1
            continue
        terminations.setdefault(code, []).append((day, name))
    records: dict[str, SecurityImport] = {}
    current: dict[str, SecurityImport] = {}
    for row in basic:
        code, name = str(row.get("公司代號") or "").strip(), str(row.get("公司簡稱") or "").strip()
        if not re.fullmatch(r"[0-9A-Z]{4,6}", code):
            skipped["unsupported_code"] += 1
            continue
        start = official_date(row.get("上市日期"))
        if start is None or not name or start > observed:
            skipped["missing_or_invalid_date"] += 1
            continue
        if code in current:
            raise ServiceError("SECURITY_SOURCE_AMBIGUOUS", "上市公司來源有重複代碼，未匯入。", 502)
        # Current basic metadata can still include an already announced termination.
        ends = [
            day
            for day, event_name in terminations.get(code, [])
            if day > start and event_name == name
        ]
        end = min(ends) if ends else None
        ubn = str(row.get("營利事業統一編號") or "").strip()
        record = SecurityImport(
            registry_key=f"twse:{code}:{start.isoformat()}",
            name=name,
            instrument_type="other",
            issuer_ubn=ubn if re.fullmatch(r"[0-9]{8}", ubn) else None,
            listings=[
                ListingInput(
                    market="TWSE",
                    code=code,
                    name=name,
                    valid_from=start,
                    valid_to=end,
                    start_evidence=_evidence(TWSE_PATHS[0], start, observed),
                    end_evidence=_evidence(TWSE_PATHS[2], end, observed) if end else None,
                )
            ],
        )
        records[record.registry_key] = record
        current[code] = record
    starts: dict[str, list[date]] = {}
    parsed_new = []
    for row in newly_listed:
        code, name = str(row.get("Code") or "").strip(), str(row.get("Company") or "").strip()
        # ListingDate is approval-related; only ApprovedListingDate is actual first trading.
        start = official_date(row.get("ApprovedListingDate"))
        if (
            not re.fullmatch(r"[0-9A-Z]{4,6}", code)
            or not name
            or start is None
            or start > observed
        ):
            skipped["missing_or_invalid_date"] += 1
            continue
        parsed_new.append((code, name, start))
        starts.setdefault(code, []).append(start)
    for code, name, start in parsed_new:
        key = f"twse:{code}:{start.isoformat()}"
        if key in records:
            continue
        later_starts = [day for day in starts[code] if day > start]
        if code in current and current[code].listings[0].valid_from > start:
            later_starts.append(current[code].listings[0].valid_from)
        next_start = min(later_starts) if later_starts else None
        candidates = [
            day
            for day, event_name in terminations.get(code, [])
            if day > start and event_name == name and (next_start is None or day <= next_start)
        ]
        if len(candidates) != 1:
            skipped["unpaired_history"] += 1
            continue
        end = candidates[0]
        if code in current:
            primary = current[code].listings[0]
            if (primary.valid_to is None or start < primary.valid_to) and primary.valid_from < end:
                skipped["conflicting_history"] += 1
                continue
        records[key] = SecurityImport(
            registry_key=key,
            name=name,
            instrument_type="other",
            listings=[
                ListingInput(
                    market="TWSE",
                    code=code,
                    name=name,
                    valid_from=start,
                    valid_to=end,
                    start_evidence=_evidence(TWSE_PATHS[1], start, observed),
                    end_evidence=_evidence(TWSE_PATHS[2], end, observed),
                )
            ],
        )
    if not records:
        raise ServiceError("SECURITY_SOURCE_EMPTY", "來源沒有可驗證的掛牌起訖資料。", 502)
    bundle = IdentityImportBundle(securities=list(records.values()))
    return bundle, {
        "input_rows": {
            "basic": len(basic),
            "newlisting": len(newly_listed),
            "delisted": len(delisted),
        },
        "verified_securities": len(records),
        "skipped": skipped,
        "note": (
            "僅建立可由官方生效日期證明的 TWSE 區間。"
            "未配對下市、TPEX/興櫃起訖及 ISIN 不推定；"
            "other 表示尚未由證券分類證據確認股別。"
        ),
    }


async def fetch_twse_metadata(client: httpx.AsyncClient) -> tuple[IdentityImportBundle, dict]:
    datasets = []
    for path in TWSE_PATHS:
        try:
            async with client.stream(
                "GET",
                TWSE_SOURCE + path,
                headers={"Accept": "application/json", "Cache-Control": "no-cache"},
                timeout=20,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise ServiceError(
                        "SECURITY_SOURCE_UNAVAILABLE", "無法取得官方證券身分來源。", 503
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_METADATA_BYTES:
                        raise ServiceError(
                            "SECURITY_SOURCE_INVALID", "官方身分來源超出大小限制。", 502
                        )
            rows = decode_rows(bytes(data))
            if len(rows) > 10000:
                raise ServiceError("SECURITY_SOURCE_INVALID", "官方身分來源超出筆數限制。", 502)
            datasets.append(rows)
        except (httpx.HTTPError, TimeoutError) as error:
            raise ServiceError(
                "SECURITY_SOURCE_UNAVAILABLE", "官方身分來源查詢失敗；不自動重試。", 503
            ) from error
    return build_twse_bundle(datasets[0], datasets[1], datasets[2])


async def run(args: argparse.Namespace) -> dict:
    if args.twse:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            bundle, source_summary = await fetch_twse_metadata(client)
    else:
        path = Path(args.file)
        if path.stat().st_size > MAX_METADATA_BYTES:
            raise ValueError("Identity import file exceeds 8 MiB")
        bundle = IdentityImportBundle.model_validate_json(path.read_bytes())
        source_summary = {"source": "trusted_operator_file", "securities": len(bundle.securities)}
    engine = create_async_engine(get_settings().sqlalchemy_url())
    try:
        report = await SecurityIdentityService(
            async_sessionmaker(engine, expire_on_commit=False)
        ).import_bundle(bundle, apply=args.apply)
        # Summary contains only identity metadata and counts, never prices or credentials.
        return {**{k: v for k, v in report.items() if k != "securities"}, "source": source_summary}
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import evidence-dated security metadata; default is dry-run"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=str)
    source.add_argument("--twse", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))
    except ServiceError as error:
        print(json.dumps({"error": error.code, "message": error.message}, ensure_ascii=False))
        raise SystemExit(1) from None
    except (ValueError, OSError, SQLAlchemyError):
        print(
            json.dumps(
                {"error": "SECURITY_IMPORT_INVALID", "message": "身分匯入檔案或設定不正確。"},
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
