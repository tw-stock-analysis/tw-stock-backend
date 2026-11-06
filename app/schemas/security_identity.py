"""Evidence-dated identity metadata; never contains prices or research results."""

from datetime import date, datetime
from typing import Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.analysis import taipei_today
from app.schemas.common import APIModel, Pagination
from app.schemas.instruments import Code

Venue = Literal["TWSE", "TPEX", "EMERGING"]
SecurityKind = Literal["ordinary_share", "etf", "other"]


class IdentityEvidence(APIModel):
    source_name: str = Field(min_length=1, max_length=120)
    source_url: str = Field(pattern=r"^https://[^\s]+$", max_length=2048)
    observed_on: date
    effective_date: date

    @field_validator("source_url")
    @classmethod
    def public_evidence_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Use an HTTPS evidence URL without embedded credentials")
        if any(
            key.lower() in {"token", "api_key", "access_token", "apikey", "authorization"}
            for key, _ in parse_qsl(parsed.query)
        ):
            raise ValueError("Evidence URLs must not expose API credentials")
        return value

    @model_validator(mode="after")
    def bounded_dates(self) -> "IdentityEvidence":
        if not date(1900, 1, 1) <= self.observed_on <= taipei_today() or self.effective_date < date(
            1900, 1, 1
        ):
            raise ValueError(
                "Use a real observation date and a documented effective date since 1900"
            )
        return self


class ListingInput(APIModel):
    market: Venue
    code: Code
    name: str = Field(min_length=1, max_length=200)
    valid_from: date
    valid_to: date | None = None
    start_evidence: IdentityEvidence
    end_evidence: IdentityEvidence | None = None

    @model_validator(mode="after")
    def evidence_matches_interval(self) -> "ListingInput":
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("Listing intervals are [valid_from,valid_to) and must be nonempty")
        if self.start_evidence.effective_date != self.valid_from:
            raise ValueError("Start evidence must document valid_from")
        if (self.valid_to is None) != (self.end_evidence is None):
            raise ValueError(
                "A closed interval needs end evidence; an open interval cannot have it"
            )
        if self.end_evidence is not None and self.end_evidence.effective_date != self.valid_to:
            raise ValueError("End evidence must document valid_to, the termination-effective date")
        return self


class IdentifierInput(APIModel):
    kind: Literal["isin", "name"]
    value: str = Field(min_length=1, max_length=200)
    valid_from: date
    valid_to: date | None = None
    evidence: IdentityEvidence
    end_evidence: IdentityEvidence | None = None

    @model_validator(mode="after")
    def identifier_valid(self) -> "IdentifierInput":
        self.value = self.value.strip()
        if not self.value or self.valid_from != self.evidence.effective_date:
            raise ValueError("Identifier value and dated evidence are required")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("Identifier interval must be nonempty")
        if (self.valid_to is None) != (self.end_evidence is None):
            raise ValueError("Closed identifier intervals require documented end evidence")
        if self.end_evidence is not None and self.end_evidence.effective_date != self.valid_to:
            raise ValueError("Identifier end evidence must document valid_to")
        if self.kind == "isin":
            import re

            self.value = self.value.upper()
            if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", self.value):
                raise ValueError("Invalid ISIN format")
            expanded = "".join(str(ord(c) - 55) if c.isalpha() else c for c in self.value)
            checksum = sum(
                sum(divmod(int(c) * (2 if i % 2 else 1), 10))
                for i, c in enumerate(reversed(expanded))
            )
            if checksum % 10:
                raise ValueError("Invalid ISIN check digit")
        return self


class SecurityImport(APIModel):
    registry_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9:_.\-/]{0,159}$")
    id: UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    instrument_type: SecurityKind = "other"
    issuer_ubn: str | None = Field(default=None, pattern=r"^[0-9]{8}$")
    listings: list[ListingInput] = Field(min_length=1, max_length=100)
    identifiers: list[IdentifierInput] = Field(default_factory=list, max_length=100)

    @field_validator("name")
    @classmethod
    def name_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Name cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def distinct_intervals(self) -> "SecurityImport":
        keys = [(r.market, r.code, r.valid_from) for r in self.listings]
        aliases = [(r.kind, r.value, r.valid_from) for r in self.identifiers]
        if len(set(keys)) != len(keys) or len(set(aliases)) != len(aliases):
            raise ValueError("Duplicate intervals in one security import")
        return self


class IdentityImportBundle(APIModel):
    schema_version: Literal[1] = 1
    securities: list[SecurityImport] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def distinct_keys(self) -> "IdentityImportBundle":
        if len({r.registry_key for r in self.securities}) != len(self.securities):
            raise ValueError("Duplicate registry keys")
        supplied_ids = [r.id for r in self.securities if r.id is not None]
        if len(set(supplied_ids)) != len(supplied_ids):
            raise ValueError("A stable identity must be described once per bundle")
        return self


class ListingView(ListingInput):
    id: UUID


class IdentifierView(IdentifierInput):
    id: UUID


class IdentityResolution(APIModel):
    status: Literal["verified", "unknown", "ambiguous"]
    as_of: date
    market: Venue | None = None
    code: str | None = None
    security_id: UUID | None = None
    matches: list[UUID] = Field(default_factory=list)
    reason: str


class SecurityView(APIModel):
    id: UUID
    registry_key: str
    name: str
    instrument_type: SecurityKind
    issuer_ubn: str | None
    created_at: datetime
    updated_at: datetime
    listings: list[ListingView]
    identifiers: list[IdentifierView]
    market_at_as_of: IdentityResolution
    note: str = (
        "內部 UUID 表示已登錄的同一證券；統編只識別發行人，不能證明證券連續性。"
        "掛牌區間含生效日、不含終止生效日；未登錄期間維持未知。名稱為登錄顯示名稱，"
        "歷史名稱僅以具日期的 name 別名為準。"
    )


class SecuritySearch(APIModel):
    code: Code | None = None
    market: Venue | None = None
    as_of: date | None = None
    include_inactive: bool = False
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=20, ge=1, le=100)


class SecuritySearchResponse(APIModel):
    data: list[SecurityView]
    pagination: Pagination
