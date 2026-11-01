#!/usr/bin/env python3
"""
scripts/smoke_test_opensearch.py

READ-ONLY smoke test of the OpenSearch path against a real cluster, with no LLM
involved. Everything it sends is a GET of `_mapping`, or a bounded `_search`
(size 0 for --queries; a few rows for --rows). It never writes, never prints
document contents or field values, and never prints credentials or tracebacks.

What it tells you (and what the unit tests cannot): whether the three registered
index families exist, whether their REAL mappings provide the fields the registry
permits, whether the field catalog considers each family usable, and whether a
guard-approved bounded query executes and normalises against the real cluster.

Usage (credentials come from the same settings as the app: OPENSEARCH_URL,
OPENSEARCH_AUTH_USERNAME / OPENSEARCH_AUTH_PASSWORD):

    OPENSEARCH_URL=http://localhost:9200 python3 scripts/smoke_test_opensearch.py
    python3 scripts/smoke_test_opensearch.py --queries            # + one bounded count query per family
    python3 scripts/smoke_test_opensearch.py --rows [--rows-size N]  # + a few rows per family: are the projected fields returned?
    python3 scripts/smoke_test_opensearch.py --save-mappings DIR  # write mapping snapshots (field names/types only)

--rows runs the SAME guard -> executor path the app uses, with the family's registered
output projection, a 24h window and at most ROWS_HARD_MAX (10) rows, newest first. It reports ONLY
registry field names with "present in n of N rows" counts -- never a field value, never a row. It
closes the gap --queries leaves: server-side `_source` filtering with dotted paths (e.g. controller
`Resource.host.name`) is exercised for real. A projected field absent from every sampled row is
reported as NOT SEEN (it may be legitimately optional; the sample is tiny) and is not a failure;
a missing @timestamp, zero rows, or any guard/executor failure is a failure.

Mapping snapshots contain field names and types, never documents. Review them
before committing anything: they are what turns the synthetic test fixtures into
real ones.

Exit code 0 only if every enabled family is usable (and, with --queries/--rows, every
query executed and, with --rows, every family returned rows carrying @timestamp). Anything else exits 1 -- a failure here is information about the
cluster or the registry, not necessarily a bug.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.opensearch_catalog import runtime  # noqa: E402
from app.opensearch_catalog.field_catalog import build_field_view  # noqa: E402

WINDOW = {"gte": "now-24h", "lte": "now"}
ROWS_DEFAULT = 3
ROWS_HARD_MAX = 10   # --rows-size is clamped to this, whatever the user passes


def check_family(entry, cache) -> dict:
    """Field-catalog compatibility of one registered family with the live cluster."""
    catalog = cache.get(entry)
    view = build_field_view(entry, catalog)
    have_mapping = view.discovery_status in ("fresh", "stale")   # otherwise nothing can be said about fields
    return {
        "pattern_id": entry.pattern_id,
        "index_pattern": entry.index_pattern,
        "discovery_status": view.discovery_status,
        "reason": view.reason,
        "usable": view.usable,
        "concrete_indices_seen": len(catalog.concrete_indices_seen),
        "mapping_variation": view.mapping_variation,
        "registered_fields_missing_live": list(view.missing_fields) if have_mapping else [],
        "role_bindings_dropped": [f"{r}:{p}" for r, p in view.dropped_role_bindings] if have_mapping else [],
        "warnings": list(view.warnings) if have_mapping else [],
    }


def count_entry(entry) -> dict:
    """A minimal, bounded, counts-only query for one family."""
    return {
        "status": "ok", "data_source": "opensearch", "pattern_id": entry.pattern_id,
        "measurement_used": {"type": "raw_metric", "name": f"{entry.pattern_id}_smoke_count", "source_metrics": []},
        "reference_used": entry.domain_reference, "explanation": "smoke test",
        "query": {"size": 0, "track_total_hits": True,
                  "query": {"bool": {"filter": [{"range": {entry.time_field: dict(WINDOW)}}]}}},
    }


def run_count_query(entry, ctx, settings) -> dict:
    """guard -> executor for one family; reports shape and metadata only (no documents)."""
    from app import executor
    from app.opensearch_catalog import guard
    g = guard.check_entry(count_entry(entry), ctx)
    if not g.ok:
        return {"pattern_id": entry.pattern_id, "ok": False, "stage": "guard", "detail": g.reason}
    e = {**count_entry(entry), "query": g.query, "index": g.index}
    ex = executor.execute_contract(e, settings)["execution"]
    md = ex.get("metadata") or {}
    return {"pattern_id": entry.pattern_id, "ok": ex["execution_status"] in ("success", "empty_result"),
            "stage": "executor", "execution_status": ex["execution_status"],
            "count": md.get("count"), "partial": md.get("partial"), "shards": md.get("shards"),
            "error": ex.get("error")}


def rows_entry(entry, size: int) -> dict:
    """A bounded rows query for one family: newest first, the family's own output projection."""
    return {
        "status": "ok", "data_source": "opensearch", "pattern_id": entry.pattern_id,
        "measurement_used": {"type": "raw_metric", "name": f"{entry.pattern_id}_smoke_rows", "source_metrics": []},
        "reference_used": entry.domain_reference, "explanation": "smoke test",
        "query": {"size": size, "track_total_hits": False,
                  "sort": [{entry.time_field: {"order": "desc"}}],
                  "_source": list(entry.output_projection),
                  "query": {"bool": {"filter": [{"range": {entry.time_field: dict(WINDOW)}}]}}},
    }


def clamp_rows_size(n) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = ROWS_DEFAULT
    return max(1, min(n, ROWS_HARD_MAX))


_CRED_PAIR = re.compile(r"(?i)\b(pass(?:word|wd)?|pwd|token|secret|api[_-]?key|authorization|auth)\b(\s*[=:]\s*)(?:(?:bearer|basic)\s+)?\S+")
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_TRACEBACK = re.compile(r"Traceback \(most recent call last\).*", re.S)


def _failure_text(exc_or_text, settings) -> str:
    """Redacted, truncated, single-line diagnostic for a failure: never a traceback, never raw backend text,
    never a credential (the configured username/password and credential-looking key=value pairs are masked)."""
    from app.opensearch_catalog.redaction import RedactionPolicy, safe_diagnostic
    policy = RedactionPolicy(mask_usernames=settings.opensearch_redact_usernames,
                             ip_mode=settings.opensearch_redact_ip_mode)
    text = f"{type(exc_or_text).__name__}: {exc_or_text}" if isinstance(exc_or_text, BaseException) else str(exc_or_text)
    text = _TRACEBACK.sub("[traceback omitted]", text)
    for secret in (getattr(settings, "opensearch_auth_password", None), getattr(settings, "opensearch_auth_username", None)):
        if secret and len(str(secret)) >= 3:
            text = text.replace(str(secret), "[REDACTED_CREDENTIAL]")
    text = _CRED_PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED_CREDENTIAL]", text)
    text = _AUTH_SCHEME.sub(lambda m: f"{m.group(1)} [REDACTED_CREDENTIAL]", text)
    text = " ".join(text.split())
    return (safe_diagnostic(text, policy, max_len=200) or "").strip()


def run_rows_query(entry, ctx, settings, size: int = ROWS_DEFAULT) -> dict:
    """guard -> executor for one family with the registered projection. Returns counts and registry
    field NAMES only; no row, no value, ever leaves this function."""
    size = clamp_rows_size(size)
    base = {"pattern_id": entry.pattern_id, "requested_size": size}
    try:
        from app import executor
        from app.opensearch_catalog import guard
        g = guard.check_entry(rows_entry(entry, size), ctx)
        if not g.ok:
            return {**base, "ok": False, "stage": "guard", "detail": _failure_text(g.reason, settings)}
        e = {**rows_entry(entry, size), "query": g.query, "index": g.index}
        ex = executor.execute_contract(e, settings)["execution"]
    except Exception as exc:  # noqa: BLE001 -- report the type and a redacted message, never a traceback
        return {**base, "ok": False, "stage": "executor", "detail": _failure_text(exc, settings)}

    status = ex.get("execution_status")
    if status not in ("success", "empty_result"):
        return {**base, "ok": False, "stage": "executor", "execution_status": status,
                "detail": _failure_text(ex.get("error"), settings)}
    hits = ex.get("hits") or []
    n = len(hits)
    projection = list(entry.output_projection)
    present = {f: sum(1 for h in hits if isinstance(h.get("fields"), dict) and f in h["fields"]) for f in projection}
    time_missing = n - present.get(entry.time_field, 0)
    not_seen = [f for f in projection if n and present[f] == 0]
    problems = []
    if n == 0:
        problems.append("no rows returned in the last 24h; projection NOT verified")
    elif time_missing:
        problems.append(f"{entry.time_field} missing from {time_missing} of {n} rows")
    return {**base, "ok": not problems, "stage": "executor", "execution_status": status,
            "rows_returned": n, "over_requested": n > size, "present_in": present, "not_seen": not_seen,
            "partial": (ex.get("metadata") or {}).get("partial"), "detail": "; ".join(problems) or None}


def save_mapping(entry, cache_fetch, directory: Path) -> Path | None:
    mapping = cache_fetch(entry.index_pattern)
    if not mapping:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / f"mapping_{entry.pattern_id}.json"
    out.write_text(json.dumps(mapping, indent=2, sort_keys=True), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--queries", action="store_true", help="also run one bounded size-0 count query per family")
    ap.add_argument("--rows", action="store_true",
                    help=f"also fetch a few rows per family and check the projected fields come back (never printed; "
                         f"at most {ROWS_HARD_MAX} rows)")
    ap.add_argument("--rows-size", type=int, default=ROWS_DEFAULT, metavar="N",
                    help=f"rows per family for --rows (default {ROWS_DEFAULT}, clamped to 1..{ROWS_HARD_MAX})")
    ap.add_argument("--save-mappings", metavar="DIR", help="write live mapping snapshots (field names/types only)")
    args = ap.parse_args(argv)

    settings = get_settings()
    print(f"Testing against {settings.opensearch_url} (read-only; no documents are printed)\n")
    try:
        registry = runtime.get_registry(settings)
    except Exception as exc:  # noqa: BLE001
        print(f"REGISTRY FAILED TO LOAD: {exc}")
        return 1
    cache = runtime.get_field_cache(settings)

    failed = False
    ctx_patterns = {}
    for entry in registry.enabled_patterns:
        r = check_family(entry, cache)
        failed |= not r["usable"]
        print(f"[{entry.pattern_id}] {entry.index_pattern}")
        print(f"   discovery={r['discovery_status']} usable={r['usable']} indices_seen={r['concrete_indices_seen']} "
              f"variation={r['mapping_variation']}" + (f" reason={r['reason']}" if r["reason"] else ""))
        if r["registered_fields_missing_live"]:
            print(f"   registered fields NOT in the live mapping: {', '.join(r['registered_fields_missing_live'])}")
        if r["role_bindings_dropped"]:
            print(f"   role bindings dropped: {', '.join(r['role_bindings_dropped'])}")
        for w in r["warnings"]:
            print(f"   warning: {w}")
        if not r["usable"] and r["discovery_status"] == "unavailable":
            print("   no live mapping could be fetched (cluster unreachable, not authorised, or no matching "
                  "indices); nothing can be said about this family's fields")
        ctx_patterns[entry.pattern_id] = entry
        if args.save_mappings:
            from app.opensearch_catalog.field_catalog import make_mapping_fetcher
            fetch = make_mapping_fetcher(settings.opensearch_url, auth=settings.opensearch_auth,
                                         timeout=settings.opensearch_timeout_seconds)
            path = save_mapping(entry, fetch, Path(args.save_mappings))
            print(f"   mapping snapshot: {path or 'NOT SAVED (fetch failed)'}")

    if args.queries:
        print("\nBounded count queries (size 0, last 24h):")
        matched = [{"reference_path": e.domain_reference, "data_source": "opensearch"} for e in ctx_patterns.values()]
        ctx = runtime.build_request_context(matched, settings)
        for entry in ctx_patterns.values():
            r = run_count_query(entry, ctx, settings)
            failed |= not r["ok"]
            print(f"   [{r['pattern_id']}] ok={r['ok']} stage={r['stage']} "
                  + (f"status={r.get('execution_status')} count={r.get('count')} partial={r.get('partial')}"
                     if r["stage"] == "executor" else f"detail={r['detail']}")
                  + (f" error={r['error']}" if r.get("error") else ""))

    unverified = False
    if args.rows:
        size = clamp_rows_size(args.rows_size)
        print(f"\nBounded row retrieval (up to {size} rows, newest first, last 24h; field names and counts only):")
        matched = [{"reference_path": e.domain_reference, "data_source": "opensearch"} for e in ctx_patterns.values()]
        ctx = runtime.build_request_context(matched, settings)
        for entry in ctx_patterns.values():
            r = run_rows_query(entry, ctx, settings, size)
            failed |= not r["ok"]
            if r.get("rows_returned") is None:
                print(f"   [{r['pattern_id']}] ok=False stage={r['stage']} detail={r['detail']}")
                continue
            print(f"   [{r['pattern_id']}] ok={r['ok']} rows={r['rows_returned']} partial={r['partial']}"
                  + (f" detail={r['detail']}" if r["detail"] else ""))
            for f, c in r["present_in"].items():
                print(f"      {f}: present in {c} of {r['rows_returned']} rows")
            if r["not_seen"]:
                unverified = True
                print(f"      NOT SEEN in the sample (may be optional; unverified): {', '.join(r['not_seen'])}")

    tail = (" and its count query executed" if args.queries else "") + (
        " and its rows came back with the projected @timestamp" if args.rows else "")
    print("\n" + ("FAILED: see above." if failed else "OK: every enabled family is usable" + tail + "."
                  + (" Some projected fields were not seen in the sampled rows (see NOT SEEN): those are unverified."
                     if unverified else "")))
    print("This is a live check of the cluster you pointed it at; it does not replace the unit tests.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
