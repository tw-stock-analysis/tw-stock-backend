from fastapi import APIRouter, Request

from app.api.deps import RequestSourceDependency
from app.schemas.lab import BacktestQuery, FactorQuery
from app.schemas.market_screen import MarketScreenQuery, MarketScreenResponse
from app.schemas.portfolio_backtest import PortfolioBacktestQuery
from app.services.backtest import backtest
from app.services.errors import ServiceError
from app.services.factors import screen
from app.services.market_screen import market_screen
from app.services.portfolio_backtest import portfolio_backtest
from app.services.security_identity import SecurityIdentityService

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


@router.post("/market-screen", response_model=MarketScreenResponse)
async def screen_market(body: MarketScreenQuery, source: RequestSourceDependency):
    return await market_screen(body, source)


@router.post("/portfolio-backtest")
async def rebalance_portfolio(
    body: PortfolioBacktestQuery, request: Request, source: RequestSourceDependency
):
    verifier = SecurityIdentityService(request.app.state.session_factory).verify_range
    return await portfolio_backtest(body, source, verifier)
