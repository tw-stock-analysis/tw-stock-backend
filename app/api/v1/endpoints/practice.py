from datetime import date
from decimal import Decimal, localcontext
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.auth_deps import CurrentSessionDependency
from app.api.deps import RequestSourceDependency, StockServiceDependency
from app.schemas.analysis import taipei_today
from app.schemas.instruments import Code
from app.schemas.practice import (
    AccountCreate,
    LearningProgress,
    OrderCreate,
    PaperAccountView,
    PracticeOrder,
    ReflectionUpdate,
)
from app.services.errors import ServiceError
from app.services.portfolio import value_portfolio
from app.services.practice import PracticeService, ledger
from app.services.research_transforms import price_rows

router = APIRouter(prefix="/practice", tags=["private investment practice"])


def service(request: Request):
    return PracticeService(request.app.state.session_factory)


PracticeDependency = Annotated[PracticeService, Depends(service)]


@router.get("/accounts", response_model=list[PaperAccountView])
async def accounts(current: CurrentSessionDependency, practice: PracticeDependency):
    return await practice.list(current.user_id)


@router.post("/accounts", status_code=201, response_model=PaperAccountView)
async def create(
    body: AccountCreate, current: CurrentSessionDependency, practice: PracticeDependency
):
    return await practice.create(current.user_id, body)


@router.get("/progress", response_model=LearningProgress)
async def progress(current: CurrentSessionDependency, practice: PracticeDependency):
    return await practice.progress(current.user_id)


@router.get("/reference")
async def reference(
    code: Code,
    day: date,
    current: CurrentSessionDependency,
    source: RequestSourceDependency,
    stocks: StockServiceDependency,
):
    if not date(1994, 10, 1) <= day <= taipei_today():
        raise ServiceError("DATE_INVALID", "請選擇有效歷史交易日。", 422)
    quote = await stocks.quote(code)
    rows, meta = await source.fetch("TaiwanStockPrice", code, day, day)
    points = price_rows(rows, code, day, day)
    point = next(
        (p for p in points if p["date"] == day.isoformat() and p["close"] is not None), None
    )
    if not point:
        raise ServiceError(
            "PRACTICE_PRICE_MISSING",
            "該日沒有有效收盤價，請選其他交易日，不會沿用前一日價格。",
            422,
        )
    return {
        "code": code,
        "name": quote.data.name,
        "date": day,
        "close": point["close"],
        "meta": meta,
        "note": "歷史原始收盤參考，未還原。使用者確認指定練習價後才記帳，非真實撮合。",
    }


@router.post("/accounts/{account_id}/orders", response_model=PaperAccountView)
async def order(
    account_id: UUID,
    body: OrderCreate,
    current: CurrentSessionDependency,
    practice: PracticeDependency,
):
    return await practice.order(current.user_id, account_id, body)


@router.put("/accounts/{account_id}/orders/{order_id}/reflection", response_model=PaperAccountView)
async def reflect(
    account_id: UUID,
    order_id: UUID,
    body: ReflectionUpdate,
    current: CurrentSessionDependency,
    practice: PracticeDependency,
):
    return await practice.reflect(current.user_id, account_id, order_id, body)


@router.delete("/accounts/{account_id}", status_code=204)
async def delete(
    account_id: UUID,
    version: Annotated[int, Query(ge=1)],
    current: CurrentSessionDependency,
    practice: PracticeDependency,
):
    await practice.delete(current.user_id, account_id, version)
    return Response(status_code=204)


@router.get("/accounts/{account_id}/valuation")
async def valuation(
    account_id: UUID,
    current: CurrentSessionDependency,
    practice: PracticeDependency,
    source: RequestSourceDependency,
):
    snapshot = await practice.get(current.user_id, account_id)
    result = await value_portfolio(
        ledger([PracticeOrder.model_validate(o) for o in snapshot["orders"]]),
        source,
        taipei_today(),
    )
    fresh = await practice.get(current.user_id, account_id)
    if fresh["version"] != snapshot["version"]:
        raise ServiceError("PRACTICE_VERSION_CONFLICT", "估值期間練習帳戶已更新，請重新取得。", 409)
    with localcontext() as context:
        context.prec = 64
        market_value = (
            sum((Decimal(p["market_value"]) for p in result["positions"]), Decimal(0))
            if result["complete"]
            else None
        )
        return {
            **result,
            "cash": snapshot["cash"],
            "equity": format(Decimal(snapshot["cash"]) + market_value, "f")
            if market_value is not None
            else None,
            "initial_cash": snapshot["initial_cash"],
            "version": snapshot["version"],
            "account_id": account_id,
        }
