"""Stop multi-call research at request cancellation; share the original worker source lock."""

from collections.abc import Awaitable, Callable
from datetime import date

from app.schemas.analysis import FinMindMeta
from app.services.errors import ServiceError
from app.services.finmind import FinMindService, Row


class RequestSource(FinMindService):
    def __init__(self, source: FinMindService, disconnected: Callable[[], Awaitable[bool]]):
        self.source, self.disconnected = source, disconnected

    async def check(self) -> None:
        if await self.disconnected():
            raise ServiceError("REQUEST_CANCELLED", "查詢已取消。", 499)

    async def fetch(
        self, dataset: str, code: str, start: date, end: date
    ) -> tuple[list[Row], FinMindMeta]:
        await self.check()
        result = await self.source.fetch(dataset, code, start, end)
        await self.check()
        return result

    async def catalogue(self, dataset: str) -> tuple[list[Row], FinMindMeta]:
        await self.check()
        result = await self.source.catalogue(dataset)
        await self.check()
        return result
