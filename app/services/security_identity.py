"""Durable identity registry; only explicitly evidenced metadata is persisted."""

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.security_identity import SecurityIdentifier, SecurityIdentity, SecurityListing
from app.schemas.analysis import taipei_today
from app.schemas.common import Pagination
from app.schemas.instruments import InstrumentRef
from app.schemas.security_identity import (
    IdentifierView,
    IdentityImportBundle,
    IdentityResolution,
    ListingView,
    SecuritySearch,
    SecuritySearchResponse,
    SecurityView,
    Venue,
)
from app.services.account_state import SessionFactory, database_now
from app.services.errors import ServiceError


def active(listing: SecurityListing, day: date) -> bool:
    return listing.valid_from <= day and (listing.valid_to is None or day < listing.valid_to)


def overlap(
    a: SecurityListing | SecurityIdentifier, b: SecurityListing | SecurityIdentifier
) -> bool:
    return (a.valid_to is None or b.valid_from < a.valid_to) and (
        b.valid_to is None or a.valid_from < b.valid_to
    )


def checked_day(day: date | None) -> date:
    result = day or taipei_today()
    if not date(1900, 1, 1) <= result <= taipei_today():
        raise ServiceError(
            "SECURITY_DATE_INVALID", "請使用 1900 年以後且非未來的身分查詢日期。", 422
        )
    return result


def assert_no_conflicts(
    listings: list[SecurityListing], identifiers: list[SecurityIdentifier]
) -> None:
    by_code: dict[tuple[str, str], list[SecurityListing]] = defaultdict(list)
    by_identity: dict[UUID, list[SecurityListing]] = defaultdict(list)
    for row in listings:
        by_code[(row.market, row.code)].append(row)
        by_identity[row.security_id].append(row)
    for group in [*by_code.values(), *by_identity.values()]:
        ordered = sorted(group, key=lambda row: row.valid_from)
        for previous, current in zip(ordered, ordered[1:]):
            if overlap(previous, current):
                raise ServiceError(
                    "SECURITY_TIMELINE_CONFLICT",
                    "掛牌區間重疊；不會將代碼重用或不同證券自動合併。",
                    409,
                )
    by_isin: dict[str, list[SecurityIdentifier]] = defaultdict(list)
    by_security_isin: dict[UUID, list[SecurityIdentifier]] = defaultdict(list)
    for identifier in identifiers:
        if identifier.kind == "isin":
            by_isin[identifier.value].append(identifier)
            by_security_isin[identifier.security_id].append(identifier)
    for identifier_group in [*by_isin.values(), *by_security_isin.values()]:
        ordered_identifiers = sorted(identifier_group, key=lambda identifier: identifier.valid_from)
        for prior_identifier, current_identifier in zip(
            ordered_identifiers, ordered_identifiers[1:]
        ):
            if overlap(prior_identifier, current_identifier):
                raise ServiceError(
                    "SECURITY_IDENTIFIER_CONFLICT", "ISIN 證據區間衝突，請核對證券與有效日期。", 409
                )


class SecurityIdentityService:
    def __init__(self, factory: SessionFactory):
        self.factory = factory

    async def _view(
        self, session: AsyncSession, record: SecurityIdentity, day: date
    ) -> SecurityView:
        listings = list(
            await session.scalars(
                select(SecurityListing)
                .where(SecurityListing.security_id == record.id)
                .order_by(SecurityListing.valid_from, SecurityListing.id)
            )
        )
        identifiers = list(
            await session.scalars(
                select(SecurityIdentifier)
                .where(SecurityIdentifier.security_id == record.id)
                .order_by(SecurityIdentifier.valid_from, SecurityIdentifier.id)
            )
        )
        current = [row for row in listings if active(row, day)]
        if len(current) == 1:
            resolution = IdentityResolution(
                status="verified",
                as_of=day,
                market=cast(Venue, current[0].market),
                code=current[0].code,
                security_id=record.id,
                matches=[record.id],
                reason="該日位於已登錄的證據掛牌區間。",
            )
        else:
            resolution = IdentityResolution(
                status="ambiguous" if current else "unknown",
                as_of=day,
                matches=[record.id],
                reason="該日有互相衝突的掛牌區間。"
                if current
                else "該日沒有已驗證掛牌區間；不以目前市場別回填。",
            )
        return SecurityView.model_validate(
            {
                "id": record.id,
                "registry_key": record.registry_key,
                "name": record.name,
                "instrument_type": record.instrument_type,
                "issuer_ubn": record.issuer_ubn,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "listings": [ListingView.model_validate(r) for r in listings],
                "identifiers": [IdentifierView.model_validate(r) for r in identifiers],
                "market_at_as_of": resolution,
            }
        )

    async def lookup(
        self,
        code: str | None = None,
        market: Venue | None = None,
        as_of: date | None = None,
        page: int = 1,
        limit: int = 20,
        include_inactive: bool = False,
    ) -> SecuritySearchResponse:
        query = SecuritySearch(
            code=code,
            market=market,
            as_of=as_of,
            page=page,
            limit=limit,
            include_inactive=include_inactive,
        )
        day = checked_day(as_of)
        async with self.factory() as session:
            clauses = [SecurityListing.security_id == SecurityIdentity.id]
            if query.code:
                clauses.append(SecurityListing.code == query.code)
            if query.market:
                clauses.append(SecurityListing.market == query.market)
            if query.as_of and not query.include_inactive:
                clauses.extend(
                    [
                        SecurityListing.valid_from <= day,
                        or_(SecurityListing.valid_to.is_(None), SecurityListing.valid_to > day),
                    ]
                )
            condition = exists(select(SecurityListing.id).where(*clauses))
            total = (
                await session.scalar(
                    select(func.count()).select_from(SecurityIdentity).where(condition)
                )
                or 0
            )
            records = list(
                await session.scalars(
                    select(SecurityIdentity)
                    .where(condition)
                    .order_by(SecurityIdentity.registry_key)
                    .offset((page - 1) * limit)
                    .limit(limit)
                )
            )
            data = [await self._view(session, record, day) for record in records]
            return SecuritySearchResponse(
                data=data, pagination=Pagination(page=page, limit=limit, total=total)
            )

    async def get(self, security_id: UUID, as_of: date | None = None) -> SecurityView:
        day = checked_day(as_of)
        async with self.factory() as session:
            record = await session.get(SecurityIdentity, security_id)
            if record is None:
                raise ServiceError("SECURITY_NOT_FOUND", "找不到這個永久證券身分。", 404)
            return await self._view(session, record, day)

    async def resolve(self, ref: InstrumentRef, as_of: date) -> IdentityResolution:
        day = checked_day(as_of)
        async with self.factory() as session:
            rows = list(
                await session.scalars(
                    select(SecurityListing)
                    .where(
                        SecurityListing.market.in_(["TWSE", "TPEX"])
                        if ref.market == "DELISTED"
                        else SecurityListing.market == ref.market,
                        SecurityListing.code == ref.code,
                    )
                    .order_by(SecurityListing.valid_from)
                )
            )
            current = [r for r in rows if active(r, day)]
            ids = sorted({r.security_id for r in current or rows}, key=str)
            if len(current) == 1:
                return IdentityResolution(
                    status="verified",
                    as_of=day,
                    market=cast(Venue, current[0].market),
                    code=current[0].code,
                    security_id=current[0].security_id,
                    matches=ids,
                    reason="該日市場與代碼對應唯一的已登錄證券。",
                )
            return IdentityResolution(
                status="ambiguous" if current else "unknown",
                as_of=day,
                market=cast(Venue, ref.market)
                if ref.market in {"TWSE", "TPEX", "EMERGING"}
                else None,
                code=ref.code,
                matches=ids[:100],
                reason="同日市場代碼有多個衝突的掛牌區間。"
                if current
                else "該日沒有已驗證掛牌區間；請查閱時間軸或匯入官方證據。",
            )

    async def verify_range(self, ref: InstrumentRef, start: date, end: date) -> UUID:
        """Anchor at end; allow documented same-ticker TPEX/TWSE transitions only."""
        checked_day(start)
        checked_day(end)
        if start > end:
            raise ServiceError("SECURITY_DATE_INVALID", "身分驗證開始日不可晚於截止日。", 422)
        if ref.market not in {"TWSE", "TPEX", "DELISTED"}:
            raise ServiceError(
                "SECURITY_MARKET_UNSUPPORTED", "成交回測僅支援已驗證的上市／上櫃掛牌區間。", 422
            )
        resolution = await self.resolve(ref, end)
        if resolution.status != "verified" or resolution.security_id is None:
            raise ServiceError(
                "SECURITY_IDENTITY_AMBIGUOUS"
                if resolution.status == "ambiguous"
                else "SECURITY_IDENTITY_UNKNOWN",
                "無法確認回測截止日的唯一證券身分，請先匯入掛牌證據。",
                409 if resolution.status == "ambiguous" else 422,
            )
        async with self.factory() as session:
            rows = list(
                await session.scalars(
                    select(SecurityListing)
                    .where(
                        SecurityListing.security_id == resolution.security_id,
                        SecurityListing.valid_from <= end,
                        or_(SecurityListing.valid_to.is_(None), SecurityListing.valid_to > start),
                    )
                    .order_by(SecurityListing.valid_from)
                )
            )
            conflicting = list(
                await session.scalars(
                    select(SecurityListing).where(
                        SecurityListing.code == ref.code,
                        SecurityListing.valid_from <= end,
                        or_(SecurityListing.valid_to.is_(None), SecurityListing.valid_to > start),
                        SecurityListing.security_id != resolution.security_id,
                    )
                )
            )
            if conflicting or any(overlap(a, b) for a, b in zip(rows, rows[1:])):
                raise ServiceError(
                    "SECURITY_IDENTITY_AMBIGUOUS", "區間內有多個證券身分或衝突掛牌證據。", 409
                )
            cursor, stop = start, end + timedelta(days=1)
            for row in rows:
                if row.valid_from > cursor:
                    raise ServiceError(
                        "SECURITY_RANGE_GAP",
                        "回測期間有未驗證的證券身分空窗，不以目前市場回填。",
                        422,
                    )
                if row.market not in {"TWSE", "TPEX"}:
                    raise ServiceError(
                        "SECURITY_MARKET_UNSUPPORTED", "回測期間包含興櫃或不支援的成交市場。", 422
                    )
                if row.code != ref.code:
                    raise ServiceError(
                        "SECURITY_CODE_CHANGE_UNSUPPORTED",
                        "區間包含代碼變更，需依歷史代碼分段取價，不能沿用目前代碼。",
                        422,
                    )
                if row.valid_from < cursor and cursor != start:
                    raise ServiceError("SECURITY_IDENTITY_AMBIGUOUS", "回測期間掛牌證據重疊。", 409)
                cursor = min(stop, row.valid_to or stop)
                if cursor >= stop:
                    return resolution.security_id
            raise ServiceError("SECURITY_RANGE_GAP", "回測期間未由已驗證掛牌區間完整涵蓋。", 422)

    async def import_bundle(self, bundle: IdentityImportBundle, *, apply: bool = True) -> dict:
        """Serialize trusted metadata imports; never merge identities by code or issuer."""
        report: dict[str, Any] = {
            "applied": apply,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "securities": [],
        }
        async with self.factory() as session:
            transaction = await session.begin()
            try:
                # Does not require extensions or elevated DB privileges. Global metadata only.
                await session.execute(
                    text("LOCK TABLE app.security_identities IN SHARE ROW EXCLUSIVE MODE")
                )
                for incoming in bundle.securities:
                    record = await session.scalar(
                        select(SecurityIdentity).where(
                            SecurityIdentity.registry_key == incoming.registry_key
                        )
                    )
                    created = record is None
                    changed = created
                    if record is None:
                        if (
                            incoming.id is not None
                            and await session.get(SecurityIdentity, incoming.id) is not None
                        ):
                            raise ServiceError(
                                "SECURITY_REGISTRY_KEY_CONFLICT",
                                "此 UUID 已有不同登錄鍵；不會更名或合併永久身分。",
                                409,
                            )
                        record = SecurityIdentity(
                            id=incoming.id or uuid4(),
                            registry_key=incoming.registry_key,
                            name=incoming.name,
                            instrument_type=incoming.instrument_type,
                            issuer_ubn=incoming.issuer_ubn,
                        )
                        session.add(record)
                        await session.flush()
                    elif incoming.id is not None and incoming.id != record.id:
                        raise ServiceError(
                            "SECURITY_REGISTRY_KEY_CONFLICT", "登錄鍵與既有 UUID 不一致。", 409
                        )
                    if (
                        record.issuer_ubn
                        and incoming.issuer_ubn
                        and record.issuer_ubn != incoming.issuer_ubn
                    ):
                        raise ServiceError(
                            "SECURITY_ISSUER_CONFLICT",
                            "發行人統編不同，須核對證券連續性，不能依代碼自動合併。",
                            409,
                        )
                    for attribute in ("name", "instrument_type", "issuer_ubn"):
                        value = getattr(incoming, attribute)
                        if attribute == "issuer_ubn" and value is None:
                            continue
                        if getattr(record, attribute) != value:
                            setattr(record, attribute, value)
                            changed = True
                    existing = list(
                        await session.scalars(
                            select(SecurityListing).where(SecurityListing.security_id == record.id)
                        )
                    )
                    for item in incoming.listings:
                        target = next(
                            (
                                r
                                for r in existing
                                if (r.market, r.code, r.valid_from)
                                == (item.market, item.code, item.valid_from)
                            ),
                            None,
                        )
                        if target is None:
                            target = SecurityListing(
                                id=uuid4(),
                                security_id=record.id,
                                **item.model_dump(exclude={"start_evidence", "end_evidence"}),
                                start_evidence=item.start_evidence.model_dump(mode="json"),
                                end_evidence=item.end_evidence.model_dump(mode="json")
                                if item.end_evidence
                                else None,
                            )
                            session.add(target)
                            existing.append(target)
                            changed = True
                        else:
                            if target.valid_to is not None and target.valid_to != item.valid_to:
                                raise ServiceError(
                                    "SECURITY_CLOSED_INTERVAL_CONFLICT",
                                    "已終止區間不能自動改寫或重新開啟。",
                                    409,
                                )
                            attributes = {
                                "name": item.name,
                                "valid_to": item.valid_to,
                                "start_evidence": item.start_evidence.model_dump(mode="json"),
                                "end_evidence": item.end_evidence.model_dump(mode="json")
                                if item.end_evidence
                                else None,
                            }
                            for key, value in attributes.items():
                                if getattr(target, key) != value:
                                    setattr(target, key, value)
                                    changed = True
                    aliases = list(
                        await session.scalars(
                            select(SecurityIdentifier).where(
                                SecurityIdentifier.security_id == record.id
                            )
                        )
                    )
                    for identifier_input in incoming.identifiers:
                        alias = next(
                            (
                                r
                                for r in aliases
                                if (r.kind, r.value, r.valid_from)
                                == (
                                    identifier_input.kind,
                                    identifier_input.value,
                                    identifier_input.valid_from,
                                )
                            ),
                            None,
                        )
                        if alias is None:
                            alias = SecurityIdentifier(
                                id=uuid4(),
                                security_id=record.id,
                                **identifier_input.model_dump(exclude={"evidence", "end_evidence"}),
                                evidence=identifier_input.evidence.model_dump(mode="json"),
                                end_evidence=identifier_input.end_evidence.model_dump(mode="json")
                                if identifier_input.end_evidence
                                else None,
                            )
                            session.add(alias)
                            aliases.append(alias)
                            changed = True
                        else:
                            if (
                                alias.valid_to is not None
                                and alias.valid_to != identifier_input.valid_to
                            ):
                                raise ServiceError(
                                    "SECURITY_CLOSED_INTERVAL_CONFLICT",
                                    "既有識別碼終止日期不能自動改寫。",
                                    409,
                                )
                            for key, value in {
                                "valid_to": identifier_input.valid_to,
                                "evidence": identifier_input.evidence.model_dump(mode="json"),
                                "end_evidence": identifier_input.end_evidence.model_dump(
                                    mode="json"
                                )
                                if identifier_input.end_evidence
                                else None,
                            }.items():
                                if getattr(alias, key) != value:
                                    setattr(alias, key, value)
                                    changed = True
                    if changed:
                        record.updated_at = await database_now(session)
                    status = "created" if created else "updated" if changed else "unchanged"
                    report[status] += 1
                    report["securities"].append(
                        {
                            "id": str(record.id),
                            "registry_key": record.registry_key,
                            "status": status,
                        }
                    )
                await session.flush()
                assert_no_conflicts(
                    list(await session.scalars(select(SecurityListing))),
                    list(await session.scalars(select(SecurityIdentifier))),
                )
                if apply:
                    await transaction.commit()
                else:
                    await transaction.rollback()
            except BaseException as error:
                if transaction.is_active:
                    await transaction.rollback()
                if isinstance(error, IntegrityError):
                    raise ServiceError(
                        "SECURITY_IMPORT_CONFLICT", "身分資料違反登錄約束；整批未匯入。", 409
                    ) from error
                raise
        return report
