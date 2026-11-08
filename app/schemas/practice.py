from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictInt, field_validator

from app.schemas.analysis import taipei_today
from app.schemas.common import APIModel
from app.schemas.instruments import Code
from app.schemas.portfolio import Money


class AccountCreate(APIModel):
    name: str = Field(min_length=1, max_length=100)
    initial_cash: Annotated[Decimal, Field(gt=0, le=Decimal("1e9"), decimal_places=2)] = Decimal(
        "1000000"
    )

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        if not value.strip():
            raise ValueError("Name cannot be blank")
        return value.strip()


class PracticeOrder(APIModel):
    id: UUID
    side: Literal["buy", "sell"]
    market: Literal["TWSE"] = "TWSE"
    code: Code
    date: date
    quantity: StrictInt = Field(gt=0, le=1_000_000_000)
    price: Annotated[Decimal, Field(gt=0, le=Decimal("1e9"), decimal_places=8)]
    fees: Money = Decimal(0)
    taxes: Money = Decimal(0)
    reason: str = Field(min_length=1, max_length=2000)
    emotion: Literal["calm", "optimistic", "anxious", "fomo", "uncertain"] = "uncertain"
    reflection: str = Field(default="", max_length=2000)

    @field_validator("date")
    @classmethod
    def past_day(cls, value):
        if not date(1994, 10, 1) <= value <= taipei_today():
            raise ValueError("Use a historical practice date")
        return value

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        if not value.strip():
            raise ValueError("Explain the practice decision")
        return value.strip()


class OrderCreate(APIModel):
    version: int = Field(ge=1)
    order: PracticeOrder

    @field_validator("order")
    @classmethod
    def no_initial_reflection(cls, value):
        if value.reflection:
            raise ValueError("Add reflection after the trade")
        return value


class ReflectionUpdate(APIModel):
    version: int = Field(ge=1)
    reflection: str = Field(max_length=2000)


class PaperAccountView(APIModel):
    id: UUID
    name: str
    initial_cash: str
    cash: str
    realized: str
    fees_and_taxes: str
    version: int
    orders: list[PracticeOrder]
    positions: list[dict[str, str]]
    created_at: datetime
    updated_at: datetime


class LearningProgress(APIModel):
    observe: bool
    note: bool
    paper_buy: bool
    reflection: bool
