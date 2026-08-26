#!/usr/bin/env python3
"""
scripts/smoke_test_prometheus.py

Run this second (after smoke_test_gemini.py), still before trying real
questions through the API. Exercises the deterministic execution layer
directly against your real Prometheus instance -- no LLM involved -- so a
failure here tells you unambiguously whether the problem is "Prometheus
isn't reachable/configured the way this code expects" as opposed to
anything about query construction or the LLM pipeline.

Usage:
    PROMETHEUS_URL=http://localhost:9090 python3 scripts/smoke_test_prometheus.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app import label_discovery, prometheus_client  # noqa: E402


def main() -> int:
    base_url = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
    print(f"Testing against {base_url}\n")

    print("1. Instant query for `up` (should always exist on a running Prometheus)...")
    outcome = prometheus_client.query_instant(base_url, "up", datetime.now(timezone.utc))
    print(f"   status={outcome.status}")
    if outcome.status != "success":
        print(f"   error={outcome.error}")
        print("\nFAILED at the very first, simplest possible query. Check PROMETHEUS_URL, "
              "that Prometheus is actually running, and that nothing (firewall, auth) is "
              "blocking a plain GET to /api/v1/query.")
        return 1
    print(f"   {len(outcome.raw_data.get('result', []))} series returned. Looks reachable.\n")

    print("2. Range query for `up` over the last 5 minutes...")
    now = datetime.now(timezone.utc)
    outcome2 = prometheus_client.query_range(base_url, "up", now - timedelta(minutes=5), now, 30)
    print(f"   status={outcome2.status}\n")

    print("3. Live label discovery for `node_load1` (needs node-exporter actually "
          "scraped and recently reporting -- an empty/failed result here doesn't "
          "necessarily mean Prometheus itself is broken, just that this specific "
          "metric isn't present right now)...")
    labels = label_discovery.discover_labels_for_metric(base_url, "node_load1")
    if labels is None:
        print("   DISCOVERY FAILED (connection/parse problem -- see above, this is "
              "the same code path as step 1, so if step 1 worked this is unexpected).")
    else:
        print(f"   labels found: {labels or '(none beyond the metric name)'}")

    print("\nIf steps 1-2 succeeded, the execution layer is confirmed working against "
          "your real Prometheus. Safe to try real questions through the API now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
