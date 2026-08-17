"""
time_utils.py

Resolves the relative time expressions the LLM phases emit (e.g. "now",
"now-1h", "now-15m", "now-7d", "now/d") into absolute UTC timestamps at the
moment of execution. These strings are never valid literal params for
Prometheus's HTTP API or OpenSearch's date math in the form the LLM emits
them, so this module is the single place that translation happens for BOTH
backends -- one grammar, one implementation, used identically regardless of
which datasource a result targets.

Grammar supported (matches prometheus-fundamentals.md's "Time Expression
Grammar" and opensearch-fundamentals.md's "Date Math" table -- both describe
the same shape, so there is deliberately only one parser, not one per
backend):

    now
    now-<N><unit>          unit in s|m|h|d|w
    now/d                   round down to the start of the current UTC day
    now-<N><unit>/d         resolve the offset first, then round down

Also parses the contract's `step` field (e.g. "60s", "5m") into an integer
number of seconds, and resolves a single instant timestamp for
`query_type: "instant"` results (as opposed to a `from`/`to` range).
"""
import re
from datetime import datetime, timedelta, timezone

_RELATIVE_RE = re.compile(r"^now(?:-(\d+)([smhdw]))?(?:/(d))?$")
_DURATION_RE = re.compile(r"^(\d+)(ms|s|m|h|d|w)$")

_UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


class TimeParseError(ValueError):
    """Raised when a time expression doesn't match any supported format."""


def resolve_relative_time(expr: str, reference: datetime = None) -> datetime:
    """
    Resolve one time expression (e.g. "now", "now-1h", "now/d",
    "now-1h/d") into an absolute UTC datetime.

    reference: the "now" instant to resolve against. Defaults to the real
    current time. Passing a fixed reference lets multiple expressions in the
    same request (e.g. a contract's `from` and `to`, or `from`/`to` across
    two results being compared) resolve against the *same* instant rather
    than two slightly different calls to datetime.now() microseconds apart.
    """
    if reference is None:
        reference = datetime.now(timezone.utc)

    expr = expr.strip().lower()
    m = _RELATIVE_RE.match(expr)
    if not m:
        raise TimeParseError(
            f"Unrecognized time expression: {expr!r}. Expected 'now', "
            f"'now-<N><unit>' (unit in s/m/h/d/w), optionally suffixed with "
            f"'/d' to round down to the start of the day."
        )

    amount, unit, round_day = m.group(1), m.group(2), m.group(3)
    resolved = reference
    if amount is not None:
        delta_seconds = int(amount) * _UNIT_SECONDS[unit]
        resolved = resolved - timedelta(seconds=delta_seconds)

    if round_day:
        resolved = resolved.replace(hour=0, minute=0, second=0, microsecond=0)

    return resolved


def resolve_instant(time_expr: str, reference: datetime = None) -> datetime:
    """Resolve a single instant expression (contract's `time` field for
    `query_type: "instant"`). Thin, named wrapper around
    resolve_relative_time so call sites read clearly regardless of whether
    they're resolving one instant or a range."""
    return resolve_relative_time(time_expr, reference=reference)


def parse_step_seconds(step_expr: str) -> int:
    """
    Parse a step duration like "60s", "5m", "1h" into an integer number of
    seconds. Both Prometheus's query_range API and OpenSearch's
    date_histogram fixed_interval accept duration strings directly, but our
    normalized output records step as a plain integer -- chart libraries
    want a number, not a string.
    """
    step_expr = step_expr.strip().lower()
    m = _DURATION_RE.match(step_expr)
    if not m:
        raise TimeParseError(
            f"Unrecognized step expression: {step_expr!r}. "
            f"Expected e.g. '30s', '60s', '5m', '1h'."
        )
    amount, unit = m.group(1), m.group(2)
    seconds = int(amount) * _UNIT_SECONDS[unit]
    if seconds < 1:
        raise TimeParseError(
            f"Step resolves to less than 1 second ({step_expr!r}); "
            f"neither backend accepts a sub-second step here."
        )
    return int(seconds)


def resolve_time_range(time_range: dict) -> dict:
    """
    Takes the contract's {"from": "...", "to": "...", "step": "..."} and
    returns {"start": datetime, "end": datetime, "step_seconds": int}, with
    from/to resolved against the SAME reference instant.
    """
    reference = datetime.now(timezone.utc)
    start = resolve_relative_time(time_range["from"], reference=reference)
    end = resolve_relative_time(time_range["to"], reference=reference)
    step_seconds = parse_step_seconds(time_range["step"])

    if start >= end:
        raise TimeParseError(
            f"Resolved start ({start.isoformat()}) is not before "
            f"end ({end.isoformat()}) -- check the 'from'/'to' values."
        )

    return {"start": start, "end": end, "step_seconds": step_seconds}


def cap_step_for_max_points(start: datetime, end: datetime, step_seconds: int,
                             max_points: int = 11_000) -> tuple[int, bool]:
    """
    If the requested step would produce more than max_points samples over
    [start, end], widen it just enough to fit -- computed *before* the
    request goes out, rather than reacting to a backend's "too many
    samples"/"too many buckets" rejection after the fact. Prometheus's own
    default query.max-samples-related guidance is the origin of the 11,000
    default; the same cap is a sane guard for OpenSearch date_histogram
    buckets too, since an unbounded bucket count is exactly as damaging to a
    frontend chart regardless of which backend produced it.

    Returns (effective_step_seconds, was_widened).
    """
    total_seconds = (end - start).total_seconds()
    if step_seconds <= 0:
        return step_seconds, False
    point_count = total_seconds / step_seconds
    if point_count <= max_points:
        return step_seconds, False

    widened = int((total_seconds / max_points) + 1)
    return max(widened, step_seconds), True


if __name__ == "__main__":
    # Quick self-check, run directly: python3 time_utils.py
    fixed_now = datetime(2026, 8, 1, 12, 30, 0, tzinfo=timezone.utc)

    assert resolve_relative_time("now", reference=fixed_now) == fixed_now
    assert resolve_relative_time("now-1h", reference=fixed_now) == fixed_now - timedelta(hours=1)
    assert resolve_relative_time("now-15m", reference=fixed_now) == fixed_now - timedelta(minutes=15)
    assert resolve_relative_time("now-7d", reference=fixed_now) == fixed_now - timedelta(days=7)
    assert resolve_relative_time("now/d", reference=fixed_now) == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert resolve_relative_time("now-1d/d", reference=fixed_now) == datetime(2026, 7, 31, tzinfo=timezone.utc)
    assert parse_step_seconds("60s") == 60
    assert parse_step_seconds("5m") == 300
    assert parse_step_seconds("1h") == 3600

    resolved = resolve_time_range({"from": "now-1h", "to": "now", "step": "60s"})
    assert resolved["step_seconds"] == 60
    assert (resolved["end"] - resolved["start"]) == timedelta(hours=1)

    eff, widened = cap_step_for_max_points(
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 2, 1, tzinfo=timezone.utc),
        step_seconds=1,
        max_points=1000,
    )
    assert widened and eff > 1

    try:
        resolve_relative_time("yesterday", reference=fixed_now)
        raise SystemExit("FAILED: should have raised TimeParseError")
    except TimeParseError:
        pass

    print("time_utils.py: all self-checks passed")
