from fastapi import APIRouter

from app.api.deps import RequestSourceDependency
from app.schemas.lab import BacktestQuery, FactorQuery
from app.services.backtest import backtest
from app.services.errors import ServiceError
from app.services.factors import screen

router = APIRouter(prefix="/research", tags=["research lab"])


@router.post("/factors")
async def factors(body: FactorQuery, source: RequestSourceDependency):
    return await screen(body, source)


@router.post("/backtest")
async def run_backtest(body: BacktestQuery, source: RequestSourceDependency):
    try:
        return await backtest(body, source)
    except TimeoutError as error:
        raise ServiceError(
            "RESEARCH_TIMEOUT", "本次來源查詢超時，請縮小期間後手動重試。", 503
        ) from error
