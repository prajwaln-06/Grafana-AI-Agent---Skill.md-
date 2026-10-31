"""
severity.py

Deterministic meaning of "abnormal" for each index family, driven by the
registry's `severity_semantics` (never by the model, never by prose).

Two uses:

1. expand_abnormal_condition(): the Generator may place the named condition
   {"named_condition": "abnormal_severity"} in a query; Python expands it to
   real DSL using the EFFECTIVE field paths the live field catalog confirmed.
   - positive_abnormal (controller, iLO): a `bool.should` over the RECOGNISED
     abnormal values of each severity field, OR-combined, minimum_should_match
     1. A missing, empty or unrecognised value is therefore never abnormal.
   - negative_healthy (fabric): the source team's own definition -- the field
     exists and is not a documented normal value.

2. assess_hit(): classifies one returned document per field and overall, and
   reports missing and conflicting fields explicitly instead of hiding them.

Field values are compared exactly (case-sensitive). A configured severity
field that is absent from the live mapping is skipped WITH a warning (the
caller surfaces it); if none remain the condition cannot be built.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.opensearch_catalog.field_catalog import OP_TERM, PatternFieldView
from app.opensearch_catalog.registry import PatternEntry, SeverityField, SeveritySemantics

CONDITION_ABNORMAL = "abnormal_severity"
NAMED_CONDITIONS = frozenset({CONDITION_ABNORMAL})

CLASS_NORMAL = "normal"
CLASS_ABNORMAL = "abnormal"
CLASS_OTHER = "other_known"
CLASS_UNRECOGNISED = "unrecognised"
CLASS_MISSING = "missing"

STATE_ABNORMAL = "abnormal"
STATE_NORMAL = "normal"
STATE_UNKNOWN = "unknown"


class SeverityConditionError(Exception):
    """The abnormal-severity condition cannot be built for this pattern."""


# ---- query expansion ---------------------------------------------------------------


def expand_abnormal_condition(entry: PatternEntry, view: PatternFieldView) -> tuple[dict, list[str]]:
    """Returns (DSL clause, warnings). Raises SeverityConditionError."""
    sem = entry.severity_semantics
    if sem is None:
        raise SeverityConditionError(f"pattern {entry.pattern_id!r} defines no severity semantics")
    warnings: list[str] = []
    usable: list[tuple[SeverityField, str]] = []
    for f in sem.fields:
        info = view.allowed_fields.get(f.path)
        eff = info.ops.get(OP_TERM) if info is not None else None
        if eff is None:
            warnings.append(f"severity field {f.path!r} has no confirmed exact-match path in the live mapping; "
                            f"it is left out of the abnormal condition")
            continue
        usable.append((f, eff))
    if not usable:
        raise SeverityConditionError(
            f"no severity field of pattern {entry.pattern_id!r} supports exact matching in the live mapping")

    if sem.mode == "negative_healthy":
        clauses = []
        for f, eff in usable:
            clauses.append({"bool": {
                "filter": [{"exists": {"field": f.path}}],
                "must_not": [{"terms": {eff: sorted(f.normal)}}],
            }})
        if len(clauses) == 1:
            return clauses[0], warnings
        return {"bool": {"should": clauses, "minimum_should_match": 1}}, warnings

    should = [{"terms": {eff: sorted(f.abnormal)}} for f, eff in usable]
    if len(should) == 1:
        return {"bool": {"filter": [should[0]]}}, warnings
    return {"bool": {"should": should, "minimum_should_match": 1}}, warnings


def severity_paths(sem: SeveritySemantics | None) -> frozenset[str]:
    return frozenset(f.path for f in sem.fields) if sem else frozenset()


# ---- hit assessment ------------------------------------------------------------------


def _lookup(source: dict, dotted: str) -> Any:
    """Value at a dotted path, tolerating both nested objects and a literal
    dotted key at any level. Missing -> None."""
    if dotted in source:
        return source[dotted]
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        head = ".".join(parts[:i])
        if head in source and isinstance(source[head], dict):
            found = _lookup(source[head], ".".join(parts[i:]))
            if found is not None:
                return found
    return None


def classify_value(f: SeverityField, value: Any, mode: str) -> str:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return CLASS_MISSING
    if not isinstance(value, str):
        return CLASS_UNRECOGNISED
    if value in f.abnormal:
        return CLASS_ABNORMAL
    if value in f.normal:
        return CLASS_NORMAL
    if value in f.other_known:
        return CLASS_OTHER
    # negative_healthy: "not a documented normal value" IS abnormal (the
    # source team's definition). positive_abnormal never infers abnormality.
    return CLASS_ABNORMAL if mode == "negative_healthy" else CLASS_UNRECOGNISED


def assess_hit(sem: SeveritySemantics | None, source: dict) -> dict | None:
    """None when the pattern defines no severity semantics. Otherwise:
    {"state": abnormal|normal|unknown, "abnormal": bool, "conflict": bool,
     "basis": [abnormal field paths], "fields": {path: {"value", "class"}},
     "missing_fields": [...], "unrecognised_fields": [...]}"""
    if sem is None:
        return None
    per_field: dict[str, dict] = {}
    for f in sem.fields:
        value = _lookup(source, f.path)
        per_field[f.path] = {"value": value, "class": classify_value(f, value, sem.mode)}
    classes = [v["class"] for v in per_field.values()]
    abnormal_fields = [p for p, v in per_field.items() if v["class"] == CLASS_ABNORMAL]
    has_normal = CLASS_NORMAL in classes
    if abnormal_fields:
        state = STATE_ABNORMAL
    elif has_normal and all(c in (CLASS_NORMAL, CLASS_MISSING, CLASS_OTHER) for c in classes):
        state = STATE_NORMAL
    else:
        state = STATE_UNKNOWN
    return {
        "state": state,
        "abnormal": state == STATE_ABNORMAL,
        "conflict": bool(abnormal_fields) and has_normal,
        "basis": abnormal_fields,
        "fields": per_field,
        "missing_fields": [p for p, v in per_field.items() if v["class"] == CLASS_MISSING],
        "unrecognised_fields": [p for p, v in per_field.items() if v["class"] == CLASS_UNRECOGNISED],
    }
