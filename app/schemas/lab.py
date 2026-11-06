"""Bounded user-selected research requests; all market data remains request-local."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas.analysis import taipei_today
from app.schemas.common import APIModel
from app.schemas.instruments import InstrumentRange, InstrumentRef

Rate = Annotated[Decimal, Field(ge=0, le=1, max_digits=16, decimal_places=8)]


class FactorRules(APIModel):
    min_revenue_yoy: Decimal | None = Field(default=None, ge=-100000, le=100000)
    min_gross_margin: Decimal | None = Field(default=None, ge=-100000, le=100000)
    min_operating_margin: Decimal | None = Field(default=None, ge=-100000, le=100000)
    max_debt_ratio: Decimal | None = Field(default=None, ge=0, le=1000)
    min_foreign_streak: int | None = Field(default=None, ge=1, le=20)
    min_trust_streak: int | None = Field(default=None, ge=1, le=20)


class FactorQuery(APIModel):
    instruments: list[InstrumentRef] = Field(min_length=1, max_length=10)
    as_of: date = Field(default_factory=taipei_today)
    rules: FactorRules = Field(default_factory=FactorRules)
    sort: Literal[
        "code",
        "revenue_yoy",
        "gross_margin",
        "operating_margin",
        "debt_ratio",
        "foreign_streak",
        "trust_streak",
    ] = "code"
    descending: bool = False

    @model_validator(mode="after")
    def valid_scope(self) -> "FactorQuery":
        if self.as_of > taipei_today() or self.as_of < date(2001, 1, 1):
            raise ValueError("Use a past date since 2001")
        if len({(i.market, i.code) for i in self.instruments}) != len(self.instruments):
            raise ValueError("Duplicate instruments")
        return self


class BacktestQuery(InstrumentRange):
    instrument: InstrumentRef
    strategy: Literal["buy_hold", "sma"] = "sma"
    basis: Literal["unadjusted", "source_adjusted"] = "unadjusted"
    fast: int = Field(default=5, ge=2, le=60)
    slow: int = Field(default=20, ge=3, le=120)
    initial_cash: Decimal = Field(
        default=Decimal("1000000"), gt=0, le=Decimal("1e12"), decimal_places=2
    )
    commission_rate: Rate = Decimal("0.001425")
    sell_tax_rate: Rate = Decimal("0.003")
    slippage_rate: Rate = Decimal("0")
    minimum_fee: Decimal = Field(default=Decimal("20"), ge=0, le=10000, decimal_places=2)
    lot_size: Literal[1, 1000] = 1

    @model_validator(mode="after")
    def valid_strategy(self) -> "BacktestQuery":
        if self.fast >= self.slow or self.instrument.market == "EMERGING":
            raise ValueError(
                "Use fast < slow; emerging-market opening references are not execution prices"
            )
        return self
