"""Known indicator values, warm-up, missing prices and complete-month aggregates."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.stock_analysis import history_points, month_shift, revenue_points
from app.services.technical_indicators import technical_indicators


def test_wilder_rsi_published_reference_sequence():
    # Wilder's sample: the initial RSI uses 14 changes, not 14 closing prices.
    closes = [
        Decimal(v)
        for v in (
            "44.34",
            "44.09",
            "44.15",
            "43.61",
            "44.33",
            "44.83",
            "45.10",
            "45.42",
            "45.84",
            "46.08",
            "45.89",
            "46.03",
            "45.61",
            "46.28",
            "46.28",
            "46.00",
            "46.03",
        )
    ]
    result = technical_indicators(closes)
    assert all(point.rsi14 is None for point in result[:14])
    assert [point.rsi14 for point in result[14:]] == ["70.4641", "66.2496", "66.4809"]


@pytest.mark.parametrize("direction,expected", [(1, "100.0000"), (-1, "0.0000"), (0, "50.0000")])
def test_rsi_rising_falling_and_unchanged_prices(direction, expected):
    points = technical_indicators([Decimal(100 + i * direction) for i in range(60)])
    assert points[-1].rsi14 == expected


def test_sma_seeded_macd_warmup_and_linear_series():
    points = technical_indicators([Decimal(i) for i in range(1, 61)])
    assert all(p.macd is None for p in points[:25])
    assert points[25].macd == "7.0000"
    assert points[32].macd_signal is None
    assert points[33].macd_signal == "7.0000"
    assert points[33].macd_histogram == "0.0000"
    assert points[-1].ma20 == "50.5000"
    assert points[58].ma60 is None
    assert points[-1].ma60 == "30.5000"


def test_bollinger_population_standard_deviation_and_constant_series():
    points = technical_indicators([Decimal(i) for i in range(1, 21)])
    assert points[18].bollinger_middle is None
    assert points[19].bollinger_middle == "10.5000"
    assert points[19].bollinger_upper == "22.0326"
    assert points[19].bollinger_lower == "-1.0326"
    flat = technical_indicators([Decimal(100)] * 60)[-1]
    assert flat.bollinger_lower == flat.bollinger_middle == flat.bollinger_upper == "100.0000"
    assert flat.macd == flat.macd_signal == flat.macd_histogram == "0.0000"


def test_macd_matches_closed_form_weights_without_lookahead():
    # Compare the recurrence with the closed-form EMA on a changing series.
    closes = [Decimal(100 + (i * i % 37) - i % 5) for i in range(55)]

    def weighted_ema(values, size):
        seed = sum(values[:size]) / size
        remaining = len(values) - size
        weight = Decimal(size - 1) / (size + 1)
        return seed * weight**remaining + (1 - weight) * sum(
            value * weight**offset for offset, value in enumerate(reversed(values[size:]))
        )

    differences = [
        weighted_ema(closes[:size], 12) - weighted_ema(closes[:size], 26)
        for size in range(26, len(closes) + 1)
    ]
    signal = weighted_ema(differences, 9)
    result = technical_indicators(closes)
    for field, expected in (
        ("macd", differences[-1]),
        ("macd_signal", signal),
        ("macd_histogram", differences[-1] - signal),
    ):
        assert abs(Decimal(getattr(result[-1], field)) - expected) <= Decimal("0.00005")
    assert technical_indicators(closes[:40]) == result[:40]


def test_unannounced_price_preserves_volume_but_never_enters_indicator_windows():
    start = date(2026, 1, 1)
    rows = [
        {
            "date": (start + timedelta(days=i)).isoformat(),
            "stock_id": "2330",
            "open": "100",
            "max": "100",
            "min": "100",
            "close": "100",
            "Trading_Volume": "1000",
            "Trading_money": "100000",
        }
        for i in range(62)
    ]
    for field in ("open", "max", "min", "close"):
        rows[60][field] = "0"
    rows[60]["Trading_Volume"] = "451"  # A no-price row may still have volume.
    result = history_points(rows, "2330", start, start + timedelta(days=61))
    missing = result[60]
    assert missing.price_available is False
    assert missing.volume == "451"
    assert missing.close == "0"  # Preserve the source value in the API.
    assert missing.ma20 is missing.rsi14 is missing.macd is missing.bollinger_middle is None
    assert result[61].ma20 == result[61].ma60 == "100.0000"
    assert result[61].rsi14 == "50.0000"
    assert result[61].macd == "0.0000"


def revenue_rows():
    return [
        {
            "date": month_shift(date(2024, 1, 1), i + 1).isoformat(),
            "stock_id": "2330",
            "revenue_year": str(month_shift(date(2024, 1, 1), i).year),
            "revenue_month": str(month_shift(date(2024, 1, 1), i).month),
            "revenue": "100" if i < 12 else "200",
        }
        for i in range(30)
    ]


def test_revenue_year_to_date_and_rolling_twelve_compare_matching_complete_periods():
    points = revenue_points(revenue_rows(), "2330", date(2026, 7, 31))
    by_month = {p.month: p for p in points}
    assert by_month["2024-11"].trailing12_revenue is None
    assert by_month["2024-12"].trailing12_revenue == "1200"
    assert by_month["2024-12"].trailing12_yoy is None
    assert by_month["2025-03"].ytd_revenue == "600"
    assert by_month["2025-03"].ytd_yoy == "100.0000"
    june = by_month["2026-06"]
    assert june.ytd_revenue == "1200"
    assert june.ytd_yoy == "0.0000"
    assert june.trailing12_revenue == "2400"
    assert june.trailing12_yoy == "33.3333"
    assert by_month["2026-01"].ytd_revenue == "200"  # Reset at January.


def test_missing_months_do_not_become_zero_and_nonpositive_comparisons_are_null():
    rows = revenue_rows()
    # Remove April 2025; June 2026's previous trailing year is now incomplete.
    rows = [
        row for row in rows if not (row["revenue_year"] == "2025" and row["revenue_month"] == "4")
    ]
    for row in rows:
        if row["revenue_year"] == "2024":
            row["revenue"] = "0"
    points = {p.month: p for p in revenue_points(rows, "2330", date(2026, 7, 31))}
    assert points["2025-03"].ytd_yoy is None
    assert points["2025-06"].ytd_revenue is None
    assert points["2025-06"].trailing12_revenue is None
    assert points["2026-06"].trailing12_revenue == "2400"
    assert points["2026-06"].trailing12_yoy is None
    assert points["2026-06"].ytd_yoy is None


def test_oldest_visible_revenue_has_enough_warmup_for_prior_year_and_trailing_year():
    end = date(2026, 10, 31)
    rows = []
    for offset in range(-60, 0):
        period = month_shift(end, offset)
        rows.append(
            {
                "date": month_shift(period, 1).isoformat(),
                "stock_id": "2330",
                "revenue_year": str(period.year),
                "revenue_month": str(period.month),
                "revenue": "100",
            }
        )
    first = revenue_points(rows, "2330", end)[0]
    assert first.month == "2023-11"
    assert first.ytd_revenue == "1100"
    assert first.ytd_yoy == "0.0000"
    assert first.trailing12_revenue == "1200"
    assert first.trailing12_yoy == "0.0000"
