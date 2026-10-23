"""Synthetic official notice/calendar contracts. No database or recorded market data."""

import asyncio
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_twse_service
from app.core.config import Settings
from app.db.session import get_session
from app.main import create_app
from app.services.notices import source_date
from app.services.twse import TwseService
from app.services.twse_catalog import BY_ID

EX = "ex-dividend"
MARKET = "market-calendar"
ATTENTION = "attention-securities"
DISPOSITION = "disposition-securities"
SUSPENSION = "suspended-securities"


@pytest.fixture
def notices_api():
    app = create_app(Settings(_env_file=None, app_env="testing", postgres_password="test-only"))
    state = {
        "rows": {
            EX: [
                {
                    "Date": "1151023",
                    "Code": "0050",
                    "Name": "測試ETF",
                    "Exdividend": "息",
                    "CashDividend": "1.2500",
                },
                {"Date": "1151024", "Code": "2330", "Name": "測試股票", "Exdividend": "權"},
                {"Date": "未定", "Code": "0051", "Name": "日期未定", "Exdividend": "息"},
            ],
            MARKET: [
                {
                    "Date": "1151023",
                    "Name": "開始交易日",
                    "Description": "依公告開始交易。<br>保留事項",
                },
                {"Date": "1151025", "Name": "測試休市日", "Description": "依公告休市。"},
            ],
            ATTENTION: [
                {
                    "Date": "1151022",
                    "Code": "0050",
                    "Name": "測試ETF",
                    "TradingInfoForAttention": "注意原因原文",
                    "Number": "1",
                }
            ],
            DISPOSITION: [
                {
                    "Date": "1151021",
                    "Code": "0050",
                    "Name": "測試ETF",
                    "DispositionPeriod": "115/10/22～115/11/02",
                    "DispositionMeasures": "處置措施原文",
                }
            ],
            SUSPENSION: [
                {
                    "Code": "2330",
                    "Name": "測試股票",
                    "TradingHaltDate": "1150813",
                    "TradingHaltTime": "080000",
                    "TradingResumptionDate": "1150814",
                    "TradingResumptionTime": "080000",
                }
            ],
        },
        "fail": set(),
        "delay": set(),
        "calls": [],
        "active": 0,
    }
    paths = {BY_ID[key].source_url: key for key in state["rows"]}

    async def forbidden_db():
        raise AssertionError("Calendar and notices must not open a database session")
        yield

    async def upstream(request):
        assert state["active"] == 0
        state["active"] += 1
        key = paths[str(request.url)]
        state["calls"].append(key)
        try:
            assert request.headers["cache-control"] == "no-cache"
            assert not request.url.query
            if key in state["delay"]:
                await asyncio.sleep(0.1)
            if key in state["fail"]:
                return httpx.Response(503)
            return httpx.Response(200, json=state["rows"][key])
        finally:
            state["active"] -= 1

    async def source():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            yield TwseService(client)

    app.dependency_overrides[get_session] = forbidden_db
    app.dependency_overrides[get_twse_service] = source
    with TestClient(app) as client:
        yield client, state


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1151023", "2026-10-23"),
        ("20261023", "2026-10-23"),
        ("115/10/23", "2026-10-23"),
        ("2026-10-23", "2026-10-23"),
        ("115年10月23日", "2026-10-23"),
        ("1151332", None),
        ("115/10/20～115/10/27", None),
        (None, None),
        ("99991231", None),
    ],
)
def test_source_date_only_normalizes_single_valid_dates(raw, expected):
    result = source_date(raw)
    assert (result.isoformat() if result else None) == expected


def test_calendar_filters_inclusive_dates_then_paginates_combined_snapshot(notices_api):
    client, state = notices_api
    body = client.get(
        "/api/v1/research/calendar?from_date=2026-10-23&to_date=2026-10-24&limit=1&page=2"
    ).json()
    assert body["complete"] is True
    assert body["undated"] == 1
    assert body["pagination"] == {"page": 2, "limit": 1, "total": 3}
    assert body["data"][0]["date"] == "2026-10-23"
    assert state["calls"] == [EX, MARKET]
    assert [source["status"] for source in body["sources"]] == ["ok", "ok"]


def test_calendar_uses_only_selected_source_and_preserves_zero_codes_and_raw_values(notices_api):
    client, state = notices_api
    body = client.get("/api/v1/research/calendar?kind=ex_dividend&q=0050").json()
    assert len(body["data"]) == 1
    assert body["data"][0]["code"] == "0050"
    assert body["data"][0]["source_date"] == "1151023"
    assert body["data"][0]["fields"]["CashDividend"] == "1.2500"
    assert state["calls"] == [EX]


def test_calendar_keeps_unknown_dates_visible_without_date_filter(notices_api):
    client, _ = notices_api
    body = client.get("/api/v1/research/calendar").json()
    assert body["data"][-1]["date"] is None
    assert body["data"][-1]["source_date"] == "未定"
    market = client.get("/api/v1/research/calendar?kind=market&q=開始交易").json()
    assert market["pagination"]["total"] == 1
    assert "<br>" not in market["data"][0]["description"]
    assert "\n" in market["data"][0]["description"]
    assert "<br>" in market["data"][0]["fields"]["Description"]
    assert client.get("/api/v1/research/calendar?q=保留事項").json()["pagination"]["total"] == 1


@pytest.mark.parametrize(
    "query",
    [
        "calendar?from_date=1151023",
        "calendar?from_date=2026-02-30",
        "calendar?to_date=0",
        "calendar?from_date=2026-11-01&to_date=2026-10-01",
        "calendar?kind=holiday",
        "calendar?year=2027",
        "calendar?page=0",
        "calendar?limit=101",
        "calendar/export?kind=unsupported",
        "risks?code=abc",
        "risks?code=2330,0050",
        "risks?kind=random",
        "risks?date=1151023",
        "risks?limit=101",
    ],
)
def test_invalid_notice_queries_never_reach_sources(notices_api, query):
    client, state = notices_api
    assert client.get("/api/v1/research/" + query).status_code == 422
    assert not state["calls"]


def test_partial_calendar_shows_only_fresh_success_and_blocks_incomplete_export(notices_api):
    client, state = notices_api
    assert client.get("/api/v1/research/calendar").json()["complete"] is True
    state["fail"] = {MARKET}
    body = client.get("/api/v1/research/calendar").json()
    assert body["complete"] is False
    assert all(event["kind"] == "ex_dividend" for event in body["data"])
    assert body["sources"][1]["error"]
    assert body["sources"][1]["retrieved_at"] is None
    exported = client.get("/api/v1/research/calendar/export")
    assert exported.status_code == 503
    assert exported.json()["error"]["code"] == "SOURCE_INCOMPLETE"
    assert not exported.headers.get("content-disposition")
    assert state["calls"] == [EX, MARKET] * 3


def test_ics_exports_all_matching_dated_events_with_correct_exclusive_end(notices_api):
    client, state = notices_api
    response = client.get("/api/v1/research/calendar/export?limit=1&page=4")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("text/calendar")
    assert response.headers["x-skipped-undated"] == "1"
    assert 'filename="twse-calendar.ics"' in response.headers["content-disposition"]
    text = response.text.replace("\r\n ", "")
    assert text.count("BEGIN:VEVENT\r\n") == 4
    assert "DTSTART;VALUE=DATE:20261023\r\nDTEND;VALUE=DATE:20261024" in text
    assert "開始交易日" in text  # Opening events must not be relabelled as holidays.
    assert len(set(re.findall(r"UID:(.+)\r\n", text))) == 4
    assert "TRANSP:TRANSPARENT" in text
    assert text.endswith("END:VCALENDAR\r\n")
    assert state["calls"] == [EX, MARKET]


def test_ics_escapes_text_injection_and_folds_utf8_lines_by_octet(notices_api):
    client, state = notices_api
    state["rows"][MARKET] = [
        {
            "Date": "1151023",
            "Name": "測試" * 60 + "\r\nBEGIN:VEVENT,;\\",
            "Description": "第一行\r\nEND:VEVENT",
        }
    ]
    response = client.get("/api/v1/research/calendar/export?kind=market")
    physical = response.content.split(b"\r\n")
    assert all(len(line) <= 75 for line in physical)
    for line in physical:
        line.decode("utf-8")
    unfolded = response.text.replace("\r\n ", "")
    assert unfolded.count("\r\nBEGIN:VEVENT\r\n") == 1
    assert "\\nBEGIN:VEVENT\\,\\;\\\\" in unfolded
    assert "DESCRIPTION:第一行\\nEND:VEVENT" in unfolded


def test_risk_sources_are_sequential_and_filter_exact_codes_without_quote_lookup(notices_api):
    client, state = notices_api
    body = client.get("/api/v1/research/risks?code=0050").json()
    assert state["calls"] == [ATTENTION, DISPOSITION, SUSPENSION]
    assert body["complete"] is True
    assert body["pagination"]["total"] == 2
    assert [notice["kind"] for notice in body["data"]] == ["attention", "disposition"]
    assert body["data"][1]["fields"]["DispositionPeriod"] == "115/10/22～115/11/02"
    assert body["data"][0]["announced_on"] == "2026-10-22"
    assert "未列出不代表" in body["note"]


def test_suspension_preserves_halt_and_resumption_without_inventing_announcement_date(notices_api):
    client, state = notices_api
    data = client.get("/api/v1/research/risks?kind=suspension&code=2330").json()["data"][0]
    assert data["announced_on"] is None
    assert data["date_label"] == "1150813"
    assert data["fields"]["TradingResumptionDate"] == "1150814"
    assert data["fields"]["TradingResumptionTime"] == "080000"
    assert "active" not in data  # These snapshots do not establish current trading status.
    assert state["calls"] == [SUSPENSION]


def test_partial_risk_result_cannot_reuse_previous_notices_or_claim_completeness(notices_api):
    client, state = notices_api
    path = "/api/v1/research/risks?code=0050"
    assert client.get(path).json()["pagination"]["total"] == 2
    state["fail"] = {ATTENTION}
    body = client.get(path).json()
    assert body["complete"] is False
    assert [notice["kind"] for notice in body["data"]] == ["disposition"]
    assert body["sources"][0]["status"] == "error"
    state["fail"] = {ATTENTION, DISPOSITION, SUSPENSION}
    response = client.get(path)
    assert response.status_code == 503
    assert "data" not in response.json()
    assert state["calls"] == [ATTENTION, DISPOSITION, SUSPENSION] * 3


def test_malformed_source_does_not_leak_partial_records_from_that_source(notices_api):
    client, state = notices_api
    state["rows"][ATTENTION].append({"Code": "2330"})
    body = client.get("/api/v1/research/risks").json()
    assert body["complete"] is False
    assert all(notice["kind"] != "attention" for notice in body["data"])
    assert body["sources"][0]["status"] == "error"


@pytest.mark.parametrize("zero", ["", "0.0000"])
def test_official_empty_attention_marker_is_a_successful_zero_notice_snapshot(notices_api, zero):
    client, state = notices_api
    state["rows"][ATTENTION] = [
        {
            "Number": "0",
            "Code": "",
            "Name": "",
            "NumberOfAnnouncement": zero,
            "TradingInfoForAttention": "",
            "Date": "",
            "ClosingPrice": zero,
            "PE": zero,
        }
    ]
    body = client.get("/api/v1/research/risks?kind=attention").json()
    assert body["complete"] is True
    assert body["data"] == []
    assert body["sources"][0]["row_count"] == 0
    assert body["sources"][0]["status"] == "ok"
    state["rows"][ATTENTION][0]["TradingInfoForAttention"] = "仍有未辨識內容"
    assert client.get("/api/v1/research/risks?kind=attention").status_code == 503
    assert state["calls"] == [ATTENTION, ATTENTION]


def test_slow_source_is_cancelled_before_next_source_without_retry(notices_api, monkeypatch):
    client, state = notices_api
    monkeypatch.setattr("app.services.notices.SOURCE_TIMEOUT", 0.01)
    state["delay"] = {ATTENTION}
    body = client.get("/api/v1/research/risks").json()
    assert body["complete"] is False
    assert "逾時" in body["sources"][0]["error"]
    assert state["calls"] == [ATTENTION, DISPOSITION, SUSPENSION]
    assert state["active"] == 0


@pytest.mark.parametrize(
    "path,keys", [("calendar", [EX, MARKET]), ("risks", [ATTENTION, DISPOSITION, SUSPENSION])]
)
def test_successful_empty_snapshots_are_distinct_from_failed_sources_and_always_fresh(
    notices_api, path, keys
):
    client, state = notices_api
    for key in keys:
        state["rows"][key] = []
    for _ in range(2):
        response = client.get("/api/v1/research/" + path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["complete"] is True
        assert response.json()["data"] == []
    assert state["calls"] == keys * 2
