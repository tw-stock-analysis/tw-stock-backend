from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        try:
            yield session
        finally:
            # Services commit successful writes before returning a response.
            # Reads and failed writes must not leave a transaction open.
            if session.in_transaction():
                await session.rollback()
