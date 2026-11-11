"""
correlation.py

Cross-index time-proximity ("did X occur near Y?") for OpenSearch, in plain
deterministic Python. The model never judges proximity: it writes two ordinary
row queries (one per index family, each validated by the guard against ITS OWN
pattern's fields, executed by the executor like any other entry) and one small
`correlation` object that names them. This module then

  * validates that object (`validate_spec`, called by the validator), and
  * compares the executed rows' timestamps (`run`, called by the executor after
    both entries ran).

Contract shape (only valid in `mode: "multi"`):

    {"mode": "multi", "results": [<entry A>, <entry B>], "synthesis": null,
     "correlation": {"type": "temporal_proximity", "left": 0, "right": 1,
                     "tolerance": "5m"}}

  * `left` / `right` index two DIFFERENT, ordinary rows entries (status ok,
    data_source opensearch, different `pattern_id`, a rows query with `size`
    >= 1, no `aggs`, no `analysis`).
  * `tolerance` is REQUIRED, a short duration ('90s', '5m', '1h'), at most the
    analysis context window limit. There is deliberately NO default: how close
    counts as "near" is an open question for the project owner
    (docs/NEXT_PHASE.md, C9), so a missing tolerance becomes a clarification
    request (see canonical.py), never an invented threshold.

Nothing here queries OpenSearch or widens a permission: it reads the already
executed, already projected and redacted rows of the two entries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.opensearch_catalog.registry import parse_short_duration_seconds

CORRELATION_TYPE = "temporal_proximity"
_ALLOWED_KEYS = frozenset({"type", "left", "right", "tolerance"})
_RUNNABLE = frozenset({"ok", "panic_mode_best_effort"})
_EXECUTED_OK = frozenset({"success", "empty_result"})

TIMESTAMP_BASIS = (
    "Compared on each source's @timestamp (ingest time). Both sources can have ingest lag and the event-time "
    "fields are not reliable, so closeness of ingest times only approximates closeness of the events themselves."
)
TOLERANCE_NOTE = (
    "The project has no defined meaning of 'near'; the tolerance used is the one stated in the request."
)


def tolerance_clarification(max_tolerance_seconds: int) -> str:
    mins = max_tolerance_seconds // 60
    return (f"How close in time should two events be to count as 'near' each other (for example 1m, 5m or 15m; "
            f"at most {mins}m)? No default is assumed.")


def needs_tolerance(contract: Any) -> bool:
    """A well-formed multi correlation request that only lacks its tolerance."""
    c = contract.get("correlation") if isinstance(contract, dict) else None
    return (isinstance(contract, dict) and contract.get("mode") == "multi" and isinstance(c, dict)
            and c.get("type") == CORRELATION_TYPE and "tolerance" not in c)


# ---- contract validation -------------------------------------------------------------------------


def validate_spec(contract: dict, max_tolerance_seconds: int) -> str | None:
    """None when the `correlation` object and the two entries it names are valid;
    otherwise the reason. Deterministic: no network, no model."""
    spec = contract.get("correlation")
    if contract.get("mode") != "multi":
        return "`correlation` is only valid in a mode:'multi' response."
    if not isinstance(spec, dict):
        return "`correlation` must be an object."
    extra = sorted(set(spec) - _ALLOWED_KEYS)
    if extra:
        return f"`correlation` has unknown key(s) {extra}; allowed: {sorted(_ALLOWED_KEYS)}."
    if spec.get("type") != CORRELATION_TYPE:
        return f"`correlation.type` must be {CORRELATION_TYPE!r}; got {spec.get('type')!r}."
    entries = contract.get("results") or []
    idx = []
    for side in ("left", "right"):
        v = spec.get(side)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < len(entries):
            return f"`correlation.{side}` must be the index (0..{len(entries) - 1}) of an entry in `results`."
        idx.append(v)
    if idx[0] == idx[1]:
        return "`correlation.left` and `correlation.right` must name two different entries."
    if "tolerance" not in spec:
        return ("`correlation.tolerance` is required: the user must say how close counts as 'near'. Ask them; do "
                "not choose a value.")
    try:
        seconds = parse_short_duration_seconds(spec["tolerance"])
    except ValueError as exc:
        return f"`correlation.tolerance`: {exc}."
    if seconds > max_tolerance_seconds:
        return (f"`correlation.tolerance` {spec['tolerance']!r} exceeds the maximum of "
                f"{max_tolerance_seconds // 60}m; it is rejected, never shortened.")
    pids = []
    for side, i in zip(("left", "right"), idx):
        e = entries[i]
        if not isinstance(e, dict) or e.get("status") not in _RUNNABLE or \
                (e.get("data_source") or "").strip().lower() != "opensearch":
            return f"`correlation.{side}` (results[{i}]) must be an executable OpenSearch entry (status ok)."
        if e.get("analysis") is not None:
            return f"`correlation.{side}` (results[{i}]) must be an ordinary rows query, not an analysis."
        q = e.get("query")
        if not isinstance(q, dict) or q.get("aggs") or q.get("aggregations"):
            return f"`correlation.{side}` (results[{i}]) must be a rows query without aggregations."
        size = q.get("size", 10)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            return f"`correlation.{side}` (results[{i}]) must return rows (`size` >= 1)."
        pids.append(e.get("pattern_id"))
    if not all(isinstance(p, str) and p for p in pids) or pids[0] == pids[1]:
        return "`correlation` compares two DIFFERENT index families: the two entries need different `pattern_id`s."
    return None


# ---- execution: pure Python over the executed rows ------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _side(role: str, index: int, entry: dict) -> dict:
    ex = entry.get("execution") if isinstance(entry.get("execution"), dict) else {}
    status = ex.get("execution_status")
    meta = ex.get("metadata") if isinstance(ex.get("metadata"), dict) else {}
    info: dict = {"role": role, "result_index": index, "pattern_id": entry.get("pattern_id"),
                  "execution_status": status, "usable": status in _EXECUTED_OK, "events": [],
                  "returned": 0, "matched": None, "complete": False, "duplicates_collapsed": 0,
                  "rows_without_timestamp": 0}
    if not info["usable"]:
        info["problem"] = ex.get("error") or f"execution status {status!r}"
        return info
    rows = ex.get("hits") or []
    info["returned"] = len(rows)
    count = meta.get("count") if isinstance(meta.get("count"), dict) else {}
    info["matched"] = count.get("value")
    seen: set[str] = set()
    for h in rows:
        if not isinstance(h, dict):
            continue
        ts = _parse_ts(h.get("timestamp"))
        if ts is None:
            info["rows_without_timestamp"] += 1
            continue
        fields = h.get("fields") if isinstance(h.get("fields"), dict) else {}
        key = f"{h.get('timestamp')}|{sorted(fields.items(), key=lambda kv: kv[0])!r}"
        if key in seen:
            info["duplicates_collapsed"] += 1
            continue
        seen.add(key)
        info["events"].append((ts, h.get("timestamp"), fields))
    info["events"].sort(key=lambda e: e[0])
    rows_meta = meta.get("rows") if isinstance(meta.get("rows"), dict) else {}
    if status == "empty_result":
        info["complete"] = not meta.get("partial")
    else:
        info["complete"] = (rows_meta.get("more_available") is False) and not meta.get("partial")
    return info


def _public_side(s: dict) -> dict:
    keys = ("role", "result_index", "pattern_id", "execution_status", "returned", "matched", "complete",
            "duplicates_collapsed", "rows_without_timestamp")
    out = {k: s[k] for k in keys}
    out["events_examined"] = len(s["events"])
    if s.get("problem"):
        out["problem"] = s["problem"]
    return out


def run(spec: dict, executed_results: list[dict], *, max_pairs: int) -> dict:
    """Correlate the executed entries named by `spec`. Never raises for bad data:
    anything it cannot establish is reported as `not_determinable`, never as
    "no correlation"."""
    tol = spec["tolerance"]
    tol_s = parse_short_duration_seconds(tol)
    left = _side("left", spec["left"], executed_results[spec["left"]])
    right = _side("right", spec["right"], executed_results[spec["right"]])
    block: dict = {
        "type": CORRELATION_TYPE, "tolerance": tol, "tolerance_seconds": tol_s,
        "tolerance_note": TOLERANCE_NOTE, "timestamp_basis": TIMESTAMP_BASIS,
        "direction": (f"For each {left['pattern_id']} event, the nearest {right['pattern_id']} event within "
                      f"+/-{tol}. delta_seconds = right timestamp - left timestamp (positive: the right event is later)."),
        "sources": {"left": _public_side(left), "right": _public_side(right)},
        "pairs": [], "warnings": [],
    }

    def finish(outcome: str, complete: bool, reason: str | None = None, **summary) -> dict:
        block.update(outcome=outcome, complete=complete, summary=summary)
        if reason:
            block["reason"] = reason
        return block

    for s in (left, right):
        if not s["usable"]:
            return finish("not_determinable", False,
                          f"The {s['role']} query ({s['pattern_id']}) did not complete: {s['problem']}. Nothing can be "
                          f"said about proximity, and this is NOT a finding of no correlation.")
        if s["returned"] and not s["events"]:
            return finish("not_determinable", False,
                          f"None of the {s['returned']} {s['role']} rows ({s['pattern_id']}) has a usable timestamp.")
    for s in (left, right):
        if not s["complete"]:
            block["warnings"].append(
                f"The {s['role']} source ({s['pattern_id']}) was not examined in full (returned {s['returned']} of "
                f"{s['matched']} matching events, or the answer was partial), so events outside the rows examined "
                f"were not compared.")
        if s["duplicates_collapsed"]:
            block["warnings"].append(
                f"{s['duplicates_collapsed']} identical {s['role']} rows ({s['pattern_id']}) were counted once.")
        if s["rows_without_timestamp"]:
            block["warnings"].append(
                f"{s['rows_without_timestamp']} {s['role']} rows ({s['pattern_id']}) had no timestamp and were skipped.")

    pairs: list[dict] = []
    unmatched = 0
    for lts, lraw, lfields in left["events"]:
        near = [(abs((rts - lts).total_seconds()), (rts - lts).total_seconds(), rraw, rfields)
                for rts, rraw, rfields in right["events"] if abs((rts - lts).total_seconds()) <= tol_s]
        if not near:
            unmatched += 1
            continue
        near.sort(key=lambda t: (t[0], t[2]))
        _, delta, rraw, rfields = near[0]
        pairs.append({"left": {"source": left["pattern_id"], "timestamp": lraw, "fields": lfields},
                      "right": {"source": right["pattern_id"], "timestamp": rraw, "fields": rfields},
                      "delta_seconds": delta, "right_events_within_tolerance": len(near)})
    both_complete = left["complete"] and right["complete"]
    summary = dict(left_events_examined=len(left["events"]), right_events_examined=len(right["events"]),
                   left_events_with_a_nearby_right_event=len(pairs), left_events_without=unmatched,
                   pairs_found=len(pairs), pairs_returned=min(len(pairs), max_pairs))
    if len(pairs) > max_pairs:
        block["warnings"].append(f"{len(pairs)} pairs found; only the first {max_pairs} (by left timestamp) are listed.")
    block["pairs"] = pairs[:max_pairs]
    if pairs:
        return finish("correlated", both_complete, None, **summary)
    if both_complete:
        return finish("none_found", True,
                      "Both sources were examined in full and no event pair lies within the tolerance "
                      "(an empty source also yields this).", **summary)
    return finish("none_found_in_examined_rows", False,
                  "No pair lies within the tolerance among the rows examined, but at least one source was not "
                  "examined in full, so a correlation cannot be ruled out.", **summary)
