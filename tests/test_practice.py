"""Private simulation accounting, concurrent writes and research-only source handling."""

import asyncio
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete

from app.models import PaperAccount, User
from app.schemas.practice import AccountCreate, OrderCreate, PracticeOrder, ReflectionUpdate
from app.services.errors import ServiceError
from app.services.portfolio import allocation
from app.services.practice import PracticeService, balances


def order(**values):
    return PracticeOrder(
        id=uuid4(),
        side="buy",
        code="2330",
        date=date(2026, 7, 1),
        quantity=1,
        price="100",
        reason="觀察營收與現金流",
        **values,
    )


def test_exact_cash_cost_realized_fees_and_no_double_count():
    buy = order()
    buy.quantity, buy.price, buy.fees = 10, Decimal("10.12345678"), Decimal("1")
    sell = buy.model_copy(
        update={
            "id": uuid4(),
            "side": "sell",
            "quantity": 5,
            "price": Decimal("12"),
            "fees": Decimal("1"),
            "taxes": Decimal("2"),
        }
    )
    result = balances(Decimal("1000"), [buy, sell])
    assert result["cash"] == "954.76543220"
    assert result["realized"] == "5.88271610"
    assert result["fees_and_taxes"] == "4"
    assert result["positions"][0]["cost"] == "51.11728390"


@pytest.mark.parametrize("mode", ["cash", "sell", "date", "sell_fees"])
def test_invalid_sequences_cannot_generate_cash_or_holdings(mode):
    first = order()
    if mode == "cash":
        orders = [first.model_copy(update={"quantity": 11})]
    elif mode == "sell":
        orders = [first.model_copy(update={"side": "sell"})]
    elif mode == "sell_fees":
        orders = [
            first,
            first.model_copy(update={"id": uuid4(), "side": "sell", "fees": Decimal("2000")}),
        ]
    else:
        orders = [first, first.model_copy(update={"id": uuid4(), "date": date(2026, 6, 30)})]
    with pytest.raises(ServiceError):
        balances(Decimal("1000"), orders)


@pytest.mark.parametrize(
    "changes",
    [
        {"price": "NaN"},
        {"quantity": True},
        {"quantity": "1.5"},
        {"price": "0"},
        {"fees": "-1"},
        {"reason": " "},
        {"date": "2099-01-01"},
    ],
)
def test_invalid_order_values_are_rejected(changes):
    payload = order().model_dump() | changes
    with pytest.raises(ValidationError):
        PracticeOrder.model_validate(payload)


async def test_owner_scoping_idempotency_conflicts_and_reflection(service_db):
    owner, other = await service_db.user(), await service_db.user()
    service = PracticeService(service_db.factory)
    account = await service.create(owner.id, AccountCreate(name="練習", initial_cash="1000"))
    assert await service.list(other.id) == []
    request = OrderCreate(version=1, order=order())
    saved = await service.order(owner.id, account["id"], request)
    assert saved["cash"] == "900.00" and saved["version"] == 2
    repeated = await service.order(owner.id, account["id"], request)
    assert repeated["version"] == 2 and len(repeated["orders"]) == 1
    with pytest.raises(ServiceError) as conflict:
        await service.order(
            owner.id,
            account["id"],
            request.model_copy(
                update={"order": request.order.model_copy(update={"price": Decimal(101)})}
            ),
        )
    assert conflict.value.code == "PRACTICE_ORDER_ID_CONFLICT"
    reflected = await service.reflect(
        owner.id,
        account["id"],
        request.order.id,
        ReflectionUpdate(version=2, reflection="回顧原本的營收假設"),
    )
    assert (
        reflected["orders"][0].reflection == "回顧原本的營收假設"
        and reflected["cash"] == saved["cash"]
    )
    assert (await service.order(owner.id, account["id"], request))["version"] == 3
    for operation in (
        service.get(other.id, account["id"]),
        service.order(other.id, account["id"], request),
        service.reflect(
            other.id,
            account["id"],
            request.order.id,
            ReflectionUpdate(version=3, reflection="不可以改"),
        ),
        service.delete(other.id, account["id"], 3),
    ):
        with pytest.raises(ServiceError) as error:
            await operation
        assert error.value.status_code == 404
    assert (await service.progress(owner.id)) == {
        "observe": False,
        "note": False,
        "paper_buy": True,
        "reflection": True,
    }
    assert not any((await service.progress(other.id)).values())
    with pytest.raises(ServiceError) as conflict:
        await service.delete(owner.id, account["id"], 1)
    assert conflict.value.status_code == 409
    async with service_db.factory.begin() as session:
        await session.execute(delete(User).where(User.id == owner.id))
    async with service_db.factory() as session:
        assert await session.get(PaperAccount, account["id"]) is None


async def test_concurrent_buy_one_winner_and_no_overspending(service_db):
    owner = await service_db.user()
    service = PracticeService(service_db.factory)
    account = await service.create(owner.id, AccountCreate(name="競爭", initial_cash="100"))
    results = await asyncio.gather(
        *(
            service.order(owner.id, account["id"], OrderCreate(version=1, order=order()))
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert (
        len(
            [
                value
                for value in results
                if isinstance(value, ServiceError) and value.status_code == 409
            ]
        )
        == 1
    )
    saved = await service.get(owner.id, account["id"])
    assert saved["cash"] == "0.00" and len(saved["orders"]) == 1


async def test_capacity_name_normalization_and_rejected_trade_rolls_back(service_db):
    owner = await service_db.user()
    service = PracticeService(service_db.factory)
    account = await service.create(owner.id, AccountCreate(name=" Example ", initial_cash="1"))
    with pytest.raises(ServiceError) as duplicate:
        await service.create(owner.id, AccountCreate(name="example"))
    assert duplicate.value.code == "PRACTICE_NAME_EXISTS"
    with pytest.raises(ServiceError):
        await service.order(owner.id, account["id"], OrderCreate(version=1, order=order()))
    assert (await service.get(owner.id, account["id"]))["version"] == 1
    for number in range(4):
        await service.create(owner.id, AccountCreate(name=f"情境 {number}"))
    with pytest.raises(ServiceError) as error:
        await service.create(owner.id, AccountCreate(name="超過容量"))
    assert error.value.code == "PRACTICE_ACCOUNT_LIMIT"


def test_allocation_never_hides_missing_positions_in_percentages():
    positions = [
        {
            "market": "TWSE",
            "code": "2330",
            "quantity": "2",
            "market_value": "200",
            "industry": "半導體",
        },
        {
            "market": "TWSE",
            "code": "2317",
            "quantity": "1",
            "market_value": "100",
            "industry": None,
        },
    ]
    result = allocation(positions, True)
    assert result["total_market_value"] == "300"
    assert result["by_instrument"][0]["percent"] == "66.6667"
    assert {r["label"] for r in result["by_industry"]} == {"半導體", "產業未確認"}
    assert allocation(positions, False)["by_instrument"] == []
    assert allocation([], True)["status"] == "empty"
