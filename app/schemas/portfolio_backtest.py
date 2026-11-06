"""Bounded multi-security simulations; saved market data is never accepted."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas.common import APIModel
from app.schemas.instruments import Code, InstrumentRange
from app.schemas.lab import Rate


class PortfolioBacktestAsset(APIModel):
    market: Literal["TWSE", "TPEX", "DELISTED"] = "TWSE"
    code: Code
    weight: Annotated[Decimal, Field(ge=0, le=1, max_digits=16, decimal_places=8)] | None = None


class PortfolioBacktestQuery(InstrumentRange):
    assets: list[PortfolioBacktestAsset] = Field(min_length=2, max_length=20)
    strategy: Literal["fixed_weights", "momentum", "low_volatility"] = "fixed_weights"
    rebalance: Literal["monthly", "quarterly"] = "monthly"
    lookback: int = Field(default=60, ge=20, le=252)
    top_n: int = Field(default=2, ge=1, le=20)
    basis: Literal["unadjusted", "source_adjusted"] = "unadjusted"
    initial_cash: Decimal = Field(
        default=Decimal("1000000"), gt=0, le=Decimal("1e12"), decimal_places=2
    )
    commission_rate: Rate = Decimal("0.001425")
    sell_tax_rate: Rate = Decimal("0.003")
    slippage_rate: Rate = Decimal("0")
    minimum_fee: Decimal = Field(default=Decimal("20"), ge=0, le=10000, decimal_places=2)
    lot_size: Literal[1, 1000] = 1
    allow_unverified_identity: bool = False

    @model_validator(mode="after")
    def valid_allocation(self) -> "PortfolioBacktestQuery":
        # FinMind identifies price histories by stock_id, not by market. Two venue
        # references for a transferred security must not become duplicate positions.
        if len({asset.code for asset in self.assets}) != len(self.assets):
            raise ValueError("Duplicate stock codes, including references across markets")
        if self.slippage_rate >= 1:
            raise ValueError("Slippage must be below one")
        if self.strategy == "fixed_weights":
            if any(asset.weight is None for asset in self.assets):
                raise ValueError("Every fixed-weight asset requires a weight")
            total = sum((asset.weight or Decimal(0) for asset in self.assets), Decimal(0))
            if not 0 < total <= 1:
                raise ValueError("Allocation weights must total more than zero and at most one")
        elif any(asset.weight is not None for asset in self.assets):
            raise ValueError("Factor strategies derive weights; omit manual weights")
        if self.top_n > len(self.assets):
            raise ValueError("top_n cannot exceed the asset universe")
        return self
