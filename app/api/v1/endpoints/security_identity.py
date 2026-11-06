from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.schemas.analysis import taipei_today
from app.schemas.instruments import Code, InstrumentRef
from app.schemas.security_identity import (
    IdentityResolution,
    SecuritySearch,
    SecuritySearchResponse,
    SecurityView,
    Venue,
)
from app.services.security_identity import SecurityIdentityService

router = APIRouter(prefix="/securities", tags=["security identity"])


def service(request: Request) -> SecurityIdentityService:
    return SecurityIdentityService(request.app.state.session_factory)


IdentityDependency = Annotated[SecurityIdentityService, Depends(service)]


@router.get("", response_model=SecuritySearchResponse)
async def securities(query: Annotated[SecuritySearch, Query()], identities: IdentityDependency):
    return await identities.lookup(**query.model_dump())


@router.get("/resolve", response_model=IdentityResolution)
async def resolve(
    market: Venue, code: Code, identities: IdentityDependency, as_of: date | None = None
):
    return await identities.resolve(
        InstrumentRef(market=market, code=code), as_of or taipei_today()
    )


@router.get("/{security_id}", response_model=SecurityView)
async def security(security_id: UUID, identities: IdentityDependency, as_of: date | None = None):
    return await identities.get(security_id, as_of)
