from datetime import datetime, timedelta, timezone

import pytest

from app.time_utils import (
    TimeParseError,
    cap_step_for_max_points,
    parse_step_seconds,
    resolve_relative_time,
    resolve_time_range,
)

FIXED_NOW = datetime(2026, 8, 1, 12, 30, 0, tzinfo=timezone.utc)


def test_resolve_now():
    assert resolve_relative_time("now", reference=FIXED_NOW) == FIXED_NOW


@pytest.mark.parametrize("expr,delta", [
    ("now-1h", timedelta(hours=1)),
    ("now-15m", timedelta(minutes=15)),
    ("now-30s", timedelta(seconds=30)),
    ("now-7d", timedelta(days=7)),
    ("now-2w", timedelta(weeks=2)),
])
def test_resolve_relative_offsets(expr, delta):
    assert resolve_relative_time(expr, reference=FIXED_NOW) == FIXED_NOW - delta


def test_resolve_day_rounding():
    assert resolve_relative_time("now/d", reference=FIXED_NOW) == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_resolve_offset_then_day_rounding():
    assert resolve_relative_time("now-1d/d", reference=FIXED_NOW) == datetime(2026, 7, 31, tzinfo=timezone.utc)


def test_unrecognized_expression_raises():
    with pytest.raises(TimeParseError):
        resolve_relative_time("yesterday", reference=FIXED_NOW)


def test_unrecognized_unit_raises():
    with pytest.raises(TimeParseError):
        resolve_relative_time("now-5x", reference=FIXED_NOW)


@pytest.mark.parametrize("expr,seconds", [("30s", 30), ("60s", 60), ("5m", 300), ("1h", 3600)])
def test_parse_step_seconds(expr, seconds):
    assert parse_step_seconds(expr) == seconds


def test_parse_step_seconds_invalid():
    with pytest.raises(TimeParseError):
        parse_step_seconds("not-a-duration")


def test_resolve_time_range_happy_path():
    resolved = resolve_time_range({"from": "now-1h", "to": "now", "step": "60s"})
    assert resolved["step_seconds"] == 60
    assert (resolved["end"] - resolved["start"]) == timedelta(hours=1)


def test_resolve_time_range_start_not_before_end_raises():
    with pytest.raises(TimeParseError):
        resolve_time_range({"from": "now", "to": "now-1h", "step": "60s"})


def test_cap_step_widens_when_over_budget():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 2, 1, tzinfo=timezone.utc)
    step, widened = cap_step_for_max_points(start, end, step_seconds=1, max_points=1000)
    assert widened is True
    assert step > 1
    assert (end - start).total_seconds() / step <= 1000


def test_cap_step_leaves_reasonable_step_alone():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
    step, widened = cap_step_for_max_points(start, end, step_seconds=60, max_points=11_000)
    assert widened is False
    assert step == 60
