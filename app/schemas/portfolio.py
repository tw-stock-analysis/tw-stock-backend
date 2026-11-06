from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas.analysis import taipei_today
from app.schemas.instruments import InstrumentRef

Quantity = Annotated[Decimal, Field(gt=0, le=Decimal("1e12"), max_digits=24, decimal_places=8)]
Money = Annotated[Decimal, Field(ge=0, le=Decimal("1e18"), max_digits=28, decimal_places=8)]


class LedgerEntry(InstrumentRef):
    kind: Literal["buy", "sell", "dividend", "split"]
    date: date
    quantity: Quantity | None = None
    price: Money | None = None
    fees: Money = Decimal(0)
    taxes: Money = Decimal(0)
    amount: Money | None = None
    ratio: Quantity | None = None

    @model_validator(mode="after")
    def valid_entry(self) -> "LedgerEntry":
        if self.date > taipei_today() or self.date < date(1994, 10, 1):
            raise ValueError("Use a historical ledger date")
        if self.kind in ("buy", "sell"):
            if (
                self.quantity is None
                or self.price is None
                or self.price <= 0
                or self.amount is not None
                or self.ratio is not None
            ):
                raise ValueError("A trade needs quantity and positive price only")
        elif self.kind == "dividend":
            if (
                self.amount is None
                or self.quantity is not None
                or self.price is not None
                or self.ratio is not None
                or self.fees
                or self.taxes
            ):
                raise ValueError("A dividend needs its received cash amount only")
        elif (
            self.ratio is None
            or self.quantity is not None
            or self.price is not None
            or self.amount is not None
            or self.fees
            or self.taxes
        ):
            raise ValueError("A split needs its quantity multiplier only")
        return self
