"""Read-only live verification: uv run python -m app.services.twse_check.

Reports contracts and row counts, never imports or persists market records.
"""

import argparse
import asyncio
import json

import httpx

from app.services.errors import ServiceError
from app.services.twse import MAX_BYTES, TwseService
from app.services.twse_catalog import BASE_URL, BY_ID, DATASETS


async def check(keys: list[str]) -> int:
    selected = [BY_ID[key] for key in keys] if keys else list(DATASETS)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        # One preflight prevents 44 blocked calls when the environment denies the host.
        try:
            async with (
                asyncio.timeout(30),
                client.stream("GET", BASE_URL + "/swagger.json", timeout=20) as response,
            ):
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        raise ValueError("Swagger too large")
            spec = json.loads(content)
            paths = spec["paths"]
            if not isinstance(paths, dict):
                raise ValueError("Invalid Swagger")
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError) as error:
            print(
                json.dumps(
                    {
                        "phase": "swagger",
                        "status": "failed",
                        "error_type": type(error).__name__,
                        "verified_datasets": 0,
                        "requested_datasets": len(selected),
                    }
                )
            )
            return 1
        failures = 0
        service = TwseService(client)
        for index, dataset in enumerate(selected):
            if index:
                await asyncio.sleep(1)
            result: dict[str, object] = {"id": dataset.id, "path": dataset.path}
            operation = paths.get(dataset.path)
            if not isinstance(operation, dict) or "get" not in operation:
                result.update(status="failed", code="SOURCE_CONTRACT_CHANGED")
                failures += 1
            else:
                try:
                    data = await service.fetch(dataset)
                    result.update(status="ok", row_count=data.meta.row_count, columns=data.columns)
                except ServiceError as error:
                    result.update(status="failed", code=error.code)
                    failures += 1
            print(json.dumps(result, ensure_ascii=False), flush=True)
        print(
            json.dumps(
                {"checked": len(selected), "passed": len(selected) - failures, "failed": failures}
            )
        )
        return int(failures > 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", action="append", choices=sorted(BY_ID), default=[])
    args = parser.parse_args()
    raise SystemExit(asyncio.run(check(args.dataset)))


if __name__ == "__main__":
    main()
