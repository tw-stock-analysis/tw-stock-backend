"""Read-only verification: uv run python -m app.services.finmind_check --code 2330."""

import argparse
import asyncio
import json

import httpx

from app.core.config import get_settings
from app.schemas.analysis import AnalysisQuery
from app.services.errors import ServiceError
from app.services.finmind import FinMindService
from app.services.stock_analysis import StockAnalysisService
from app.services.stocks import StockService
from app.services.twse import TwseService


async def check(code: str, query: AnalysisQuery) -> int:
    async with httpx.AsyncClient(follow_redirects=False) as client:
        service = StockAnalysisService(
            StockService(TwseService(client)),
            FinMindService(client, get_settings().finmind_api_token.get_secret_value()),
        )
        try:
            result = await service.analyse(code, query)
        except ServiceError as error:
            print(json.dumps({"phase": "twse_identity", "status": "failed", "code": error.code}))
            return 1
        for name in ("history", "revenue", "institutions"):
            section = getattr(result, name)
            print(
                json.dumps(
                    {
                        "section": name,
                        "status": section.status,
                        "rows": len(section.data) if section.data is not None else None,
                        "error_code": section.error.code if section.error else None,
                        "retrieved_at": section.meta.retrieved_at.isoformat()
                        if section.meta
                        else None,
                    }
                )
            )
        print(json.dumps({"code": code, "complete": result.complete}))
        return int(not result.complete)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", default="2330", type=str)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    args = parser.parse_args()
    if (
        not 4 <= len(args.code) <= 6
        or not args.code.isascii()
        or not args.code.isalnum()
        or args.code != args.code.upper()
    ):
        parser.error("Use a 4-6 character TWSE code")
    values = {
        key: value
        for key, value in {"start_date": args.start_date, "end_date": args.end_date}.items()
        if value is not None
    }
    query = AnalysisQuery.model_validate(values)
    raise SystemExit(asyncio.run(check(args.code, query)))


if __name__ == "__main__":
    main()
