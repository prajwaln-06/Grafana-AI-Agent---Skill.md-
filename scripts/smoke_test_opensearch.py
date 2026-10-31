#!/usr/bin/env python3
"""
scripts/smoke_test_opensearch.py

READ-ONLY smoke test of the OpenSearch path against a real cluster, with no LLM
involved. Everything it sends is a GET of `_mapping`, or a bounded `_search`
with size 0 (counts only). It never writes, never prints document contents, and
never prints credentials.

What it tells you (and what the unit tests cannot): whether the three registered
index families exist, whether their REAL mappings provide the fields the registry
permits, whether the field catalog considers each family usable, and whether a
guard-approved bounded query executes and normalises against the real cluster.

Usage (credentials come from the same settings as the app: OPENSEARCH_URL,
OPENSEARCH_AUTH_USERNAME / OPENSEARCH_AUTH_PASSWORD):

    OPENSEARCH_URL=http://localhost:9200 python3 scripts/smoke_test_opensearch.py
    python3 scripts/smoke_test_opensearch.py --queries            # + one bounded count query per family
    python3 scripts/smoke_test_opensearch.py --save-mappings DIR  # write mapping snapshots (field names/types only)

Mapping snapshots contain field names and types, never documents. Review them
before committing anything: they are what turns the synthetic test fixtures into
real ones.

Exit code 0 only if every enabled family is usable (and, with --queries, every
query executed). Anything else exits 1 -- a failure here is information about the
cluster or the registry, not necessarily a bug.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.opensearch_catalog import runtime  # noqa: E402
from app.opensearch_catalog.field_catalog import build_field_view  # noqa: E402

WINDOW = {"gte": "now-24h", "lte": "now"}


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

    print("\n" + ("FAILED: see above." if failed else "OK: every enabled family is usable"
                  + (" and its count query executed." if args.queries else ".")))
    print("This is a live check of the cluster you pointed it at; it does not replace the unit tests.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
