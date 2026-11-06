from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.common import APIModel
from app.schemas.instruments import InstrumentRef
from app.schemas.lab import FactorQuery
from app.schemas.portfolio import LedgerEntry

DocumentKind = Literal["screen", "note", "chart", "comparison", "portfolio"]


class DocumentBase(APIModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank")
        return value


class ScreenDocument(DocumentBase):
    kind: Literal["screen"]
    query: FactorQuery


class NoteDocument(DocumentBase):
    kind: Literal["note"]
    instrument: InstrumentRef
    body: str = Field(max_length=10000)


class ChartDocument(DocumentBase):
    kind: Literal["chart"]
    instrument: InstrumentRef
    overlay: Literal["ma", "bollinger"] = "ma"
    indicator: Literal["rsi", "macd"] = "rsi"
    days: int = Field(default=180, ge=1, le=366)


class ComparisonDocument(DocumentBase):
    kind: Literal["comparison"]
    instruments: list[InstrumentRef] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def distinct(self) -> "ComparisonDocument":
        if len({(i.market, i.code) for i in self.instruments}) != len(self.instruments):
            raise ValueError("Duplicate instruments")
        return self


class PortfolioDocument(DocumentBase):
    kind: Literal["portfolio"]
    ledger: list[LedgerEntry] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def finite_holdings(self) -> "PortfolioDocument":
        if len({(i.market, i.code) for i in self.ledger}) > 10:
            raise ValueError("At most ten distinct instruments per portfolio")
        return self


Document = Annotated[
    ScreenDocument | NoteDocument | ChartDocument | ComparisonDocument | PortfolioDocument,
    Field(discriminator="kind"),
]


class WorkspaceCreate(APIModel):
    document: Document


class WorkspaceUpdate(WorkspaceCreate):
    version: int = Field(ge=1)


class WorkspaceItemResponse(APIModel):
    id: UUID
    kind: DocumentKind
    name: str
    version: int
    document: Document
    created_at: datetime
    updated_at: datetime
