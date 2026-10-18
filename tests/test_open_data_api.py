import json
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.session import get_session
from app.main import create_app
from app.schemas.open_data import DataCategory, RecordQuery
from app.services.data_catalog import DATASETS
from app.services.errors import ServiceError
from app.services.open_data import OpenDataService
from app.services.open_data_import import publish_dataset
from app.services.providers.open_data import decode_records


def make_app():
    return create_app(Settings(_env_file=None, app_env="testing", postgres_password="test-only"))


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/fundamentals/monthly-revenue",
        "/api/v1/fundamentals/monthly-revenue?dataset=twse_revenue&limit=201",
        "/api/v1/ownership/institutional-trades?dataset=tpex_institutional&page=0",
        "/api/v1/data-sources/twse_prices/records?date_from=2026-10-16&date_to=2026-10-15",
        "/api/v1/derivatives/put-call-ratio?dataset=taifex_put_call_ratio&unknown=true",
    ],
)
def test_query_models_reject_invalid_input(path):
    with TestClient(make_app()) as client:
        response = client.get(path)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_or_wrong_category_dataset_fails_before_database():
    with TestClient(make_app()) as client:
        unknown = client.get("/api/v1/data-sources/unknown/records")
        wrong = client.get("/api/v1/fundamentals/monthly-revenue?dataset=twse_prices")
    assert unknown.status_code == 404
    assert wrong.status_code == 422


@pytest.fixture
def revenue_snapshot():
    return decode_records(
        DATASETS["twse_revenue"],
        json.dumps(
            [
                {
                    "公司代號": "2330",
                    "出表日期": "1151016",
                    "資料年月": "11509",
                    "營業收入-當月營收": "123.456",
                },
                {
                    "公司代號": "2317",
                    "出表日期": "1151016",
                    "資料年月": "11509",
                    "營業收入-當月營收": "--",
                },
            ]
        ).encode(),
    )


@pytest.mark.integration
async def test_publication_query_filtering_pagination_and_atomic_replacement(
    db_session, revenue_snapshot
):
    service = OpenDataService(db_session)
    with pytest.raises(ServiceError) as exc:
        await service.records("twse_revenue", RecordQuery())
    assert exc.value.code == "DATASET_NOT_LOADED"
    await publish_dataset(db_session, revenue_snapshot)
    result = await service.records("twse_revenue", RecordQuery(symbol="2330", limit=1))
    assert result.pagination.total == 1
    assert result.data[0].fields["營業收入-當月營收"] == "123.456"
    assert result.meta.units == "source_native"
    assert result.meta.data_as_of == revenue_snapshot.data_as_of
    assert result.meta.data_version == revenue_snapshot.data_version
    catalog = await service.catalog(category=DataCategory.MONTHLY_REVENUE)
    assert {r.meta.dataset: r.status for r in catalog.data} == {
        "twse_revenue": "available",
        "tpex_revenue": "not_loaded",
    }
    await publish_dataset(
        db_session,
        replace(
            revenue_snapshot, retrieved_at=revenue_snapshot.retrieved_at + timedelta(seconds=1)
        ),
    )
    result = await service.records("twse_revenue", RecordQuery(limit=1))
    assert result.pagination.total == 2
    assert len(result.data) == 1
    result = await service.records("twse_revenue", RecordQuery(date_to="2026-10-15"))
    assert result.pagination.total == 0


@pytest.mark.integration
async def test_older_and_failed_imports_preserve_last_published_data(
    db_session, revenue_snapshot, monkeypatch
):
    await publish_dataset(db_session, revenue_snapshot)
    async with db_session.begin_nested():
        with pytest.raises(ServiceError) as exc:
            await publish_dataset(
                db_session,
                replace(
                    revenue_snapshot, data_as_of=revenue_snapshot.data_as_of - timedelta(days=1)
                ),
            )
        assert exc.value.code == "SOURCE_DATE_REGRESSION"
    original = db_session.execute

    async def failing_insert(statement, *args, **kwargs):
        if str(statement).startswith("INSERT INTO market.open_data_records"):
            raise RuntimeError("synthetic write failure after delete")
        return await original(statement, *args, **kwargs)

    with pytest.raises(RuntimeError):
        async with db_session.begin_nested():
            monkeypatch.setattr(db_session, "execute", failing_insert)
            await publish_dataset(
                db_session,
                replace(
                    revenue_snapshot,
                    retrieved_at=revenue_snapshot.retrieved_at + timedelta(seconds=1),
                ),
            )
    monkeypatch.setattr(db_session, "execute", original)
    result = await OpenDataService(db_session).records("twse_revenue", RecordQuery())
    assert result.pagination.total == 2


@pytest.mark.integration
async def test_http_reads_persisted_snapshot_without_source_network(db_session, revenue_snapshot):
    await publish_dataset(db_session, revenue_snapshot)
    app = make_app()

    async def session():
        yield db_session

    app.dependency_overrides[get_session] = session
    import httpx

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/fundamentals/monthly-revenue?dataset=twse_revenue&symbol=2330"
            )
            assert response.status_code == 200
            assert response.json()["pagination"]["total"] == 1
            assert response.json()["data"][0]["symbol"] == "2330"
            missing = await client.get(
                "/api/v1/ownership/holding-distributions?dataset=tdcc_distribution"
            )
            assert missing.status_code == 503
            assert missing.json()["error"]["code"] == "DATASET_NOT_LOADED"


@pytest.mark.integration
async def test_market_totals_do_not_pretend_to_be_individual_balances(db_session):
    snapshot = decode_records(
        DATASETS["twse_margin"],
        json.dumps(
            [
                {"Date": "20261016", "MarginPurchaseTodayBalance": "12345"},
            ]
        ).encode(),
    )
    await publish_dataset(db_session, snapshot)
    service = OpenDataService(db_session)
    result = await service.records("twse_margin", RecordQuery())
    assert result.data[0].symbol is None
    with pytest.raises(ServiceError) as exc:
        await service.records("twse_margin", RecordQuery(symbol="2330"))
    assert exc.value.code == "SYMBOL_FILTER_UNSUPPORTED"
