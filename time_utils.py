"""
time_utils.py

Resolves the relative time expressions produced by the skill's LLM phases
(e.g. "now", "now-1h", "now-15m", "now-7d") into absolute UTC timestamps
at the moment of execution. These strings are never valid literal params
for Prometheus's HTTP API or OpenSearch's date math in the form the LLM
emits them, so this module is the single place that translation happens.

Also parses the contract's `step` field (e.g. "60s", "5m") into an
integer number of seconds, since Prometheus's query_range API and our
own normalized output both need that as a plain number.
"""
import re
from datetime import datetime, timedelta, timezone

_RELATIVE_RE = re.compile(r"^now(?:-(\d+)([smhdw]))?$")
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
    """Raised when a time_range field doesn't match any supported format."""


def resolve_relative_time(expr: str, reference: datetime = None) -> datetime:
    """
    Resolve one side of a contract's time_range (e.g. "now", "now-1h")
    into an absolute UTC datetime.

    reference: the "now" instant to resolve against. Defaults to the
    real current time. Passing a fixed reference lets from/to be resolved
    against the *same* instant rather than two slightly different calls
    to datetime.now() a few microseconds apart.
    """
    if reference is None:
        reference = datetime.now(timezone.utc)

    expr = expr.strip().lower()
    m = _RELATIVE_RE.match(expr)
    if not m:
        raise TimeParseError(
            f"Unrecognized time expression: {expr!r}. "
            f"Expected 'now' or 'now-<N><unit>' with unit in s/m/h/d/w."
        )

    amount, unit = m.group(1), m.group(2)
    if amount is None:
        return reference

    delta_seconds = int(amount) * _UNIT_SECONDS[unit]
    return reference - timedelta(seconds=delta_seconds)


def parse_step_seconds(step_expr: str) -> int:
    """
    Parse a step duration like "60s", "5m", "1h" into an integer number
    of seconds. Prometheus's own API also accepts these strings directly,
    but our normalized output records step as a plain integer for
    downstream consumers (chart libraries want a number, not a string).
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
            f"Prometheus requires step >= 1s."
        )
    return int(seconds)


def resolve_time_range(time_range: dict) -> dict:
    """
    Takes the contract's {"from": "...", "to": "...", "step": "..."} and
    returns {"start": datetime, "end": datetime, "step_seconds": int},
    with from/to resolved against the SAME reference instant.
    """
    reference = datetime.now(timezone.utc)
    start = resolve_relative_time(time_range["from"], reference=reference)
    end = resolve_relative_time(time_range["to"], reference=reference)
    step_seconds = parse_step_seconds(time_range["step"])

    if start >= end:
        raise TimeParseError(
            f"Resolved start ({start.isoformat()}) is not before "
            f"end ({end.isoformat()}) — check the 'from'/'to' values."
        )

    return {"start": start, "end": end, "step_seconds": step_seconds}


if __name__ == "__main__":
    # Quick self-check, run directly: python3 time_utils.py
    fixed_now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

    assert resolve_relative_time("now", reference=fixed_now) == fixed_now
    assert resolve_relative_time("now-1h", reference=fixed_now) == fixed_now - timedelta(hours=1)
    assert resolve_relative_time("now-15m", reference=fixed_now) == fixed_now - timedelta(minutes=15)
    assert resolve_relative_time("now-7d", reference=fixed_now) == fixed_now - timedelta(days=7)
    assert parse_step_seconds("60s") == 60
    assert parse_step_seconds("5m") == 300
    assert parse_step_seconds("1h") == 3600

    resolved = resolve_time_range({"from": "now-1h", "to": "now", "step": "60s"})
    assert resolved["step_seconds"] == 60
    assert (resolved["end"] - resolved["start"]) == timedelta(hours=1)

    try:
        resolve_relative_time("yesterday", reference=fixed_now)
        raise SystemExit("FAILED: should have raised TimeParseError")
    except TimeParseError:
        pass

    print("time_utils.py: all self-checks passed")
