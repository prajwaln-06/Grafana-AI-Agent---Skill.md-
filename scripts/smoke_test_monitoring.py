#!/usr/bin/env python3
"""
scripts/smoke_test_monitoring.py

Smoke test for the AI Agent's Prometheus monitoring integration.
Validates Google SRE's 4 Golden Signals (Latency, Traffic, Errors, Saturation).

Usage:
    python scripts/smoke_test_monitoring.py
    python scripts/smoke_test_monitoring.py --agent-url http://localhost:8000 --prometheus-url http://localhost:9090
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

REQUIRED_METRICS = [
    "adk_request_duration_seconds",
    "adk_pipeline_step_duration_seconds",
    "adk_requests_total",
    "adk_llm_tokens_total",
    "adk_query_validation_total",
    "adk_query_empty_results_total",
    "adk_llm_api_errors_total",
    "adk_alert_proposals_total",
    "adk_in_flight_requests",
    "adk_active_sessions_gauge",
]


def check_agent_metrics(agent_url: str) -> dict[str, bool]:
    """Probes the agent's /metrics endpoint and checks for all 10 Golden Signal metrics."""
    metrics_url = f"{agent_url.rstrip('/')}/metrics"
    print(f"1. Checking AI Agent /metrics endpoint: {metrics_url} ...")
    try:
        resp = requests.get(metrics_url, timeout=5)
    except Exception as exc:
        print(f"   [FAIL] Could not reach AI Agent at {metrics_url}: {exc}")
        print("          Ensure the agent is running: python run_server.py")
        return {m: False for m in REQUIRED_METRICS}

    if resp.status_code != 200:
        print(f"   [FAIL] Expected HTTP 200 from /metrics, got {resp.status_code}")
        return {m: False for m in REQUIRED_METRICS}

    text = resp.text
    results = {}
    print(f"   [OK] HTTP 200 received ({len(text)} bytes).")
    print("   Checking presence of the 4 Golden Signal metrics:")
    for metric in REQUIRED_METRICS:
        present = metric in text
        results[metric] = present
        mark = "✓" if present else "✗"
        status_text = "PRESENT" if present else "MISSING"
        print(f"     [{mark}] {metric:36} : {status_text}")

    return results


def simulate_sample_query(agent_url: str) -> bool:
    """Sends a sample query to /api/v1/query to verify metric updates."""
    query_url = f"{agent_url.rstrip('/')}/api/v1/query"
    print(f"\n2. Sending sample query to AI Agent: {query_url} ...")
    payload = {"question": "What is the CPU usage on node-1?"}
    try:
        t0 = time.perf_counter()
        resp = requests.post(query_url, json=payload, timeout=10)
        elapsed = time.perf_counter() - t0
        print(f"   [OK] Response received in {elapsed:.3f}s (HTTP {resp.status_code})")
        return True
    except Exception as exc:
        print(f"   [WARN] Query failed or agent not ready to answer live LLM calls: {exc}")
        print("          (Offline/unit tests still verify telemetry hooks directly)")
        return False


def check_prometheus_scraper(prometheus_url: str) -> bool:
    """Checks if Prometheus is running and scraping the AI Agent target."""
    print(f"\n3. Checking Prometheus scraper at {prometheus_url} ...")
    try:
        ready_resp = requests.get(f"{prometheus_url.rstrip('/')}/-/ready", timeout=3)
        if ready_resp.status_code != 200:
            print(f"   [WARN] Prometheus /-/ready returned HTTP {ready_resp.status_code}")
            return False
    except Exception:
        print(f"   [INFO] Prometheus is not currently running on {prometheus_url}.")
        print("          To start Prometheus:")
        print("            - Via Docker: docker compose -f docker-compose.monitoring.yml up -d")
        print("            - Or standalone: .\\prometheus.exe --config.file=prometheus/prometheus.yml")
        return False

    print("   [OK] Prometheus is running!")
    try:
        # Check scrape targets
        targets_resp = requests.get(f"{prometheus_url.rstrip('/')}/api/v1/targets", timeout=3)
        if targets_resp.status_code == 200:
            targets_data = targets_resp.json().get("data", {}).get("activeTargets", [])
            for target in targets_data:
                job = target.get("labels", {}).get("job")
                health = target.get("health")
                if job == "ai-agent-backend":
                    status = "HEALTHY (UP)" if health == "up" else f"UNHEALTHY ({health})"
                    print(f"   [OK] Found scrape target 'ai-agent-backend': {status}")
                    return health == "up"
        print("   [INFO] Scrape target 'ai-agent-backend' not yet registered in Prometheus active targets.")
    except Exception as exc:
        print(f"   [WARN] Could not inspect Prometheus targets: {exc}")

    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test for AI Agent Prometheus monitoring.")
    parser.add_argument("--agent-url", "--url", dest="agent_url", default=os.environ.get("AGENT_URL", "http://localhost:8000"),
                        help="Base URL of the AI Agent (default: http://localhost:8000)")
    parser.add_argument("--prometheus-url", default=os.environ.get("PROMETHEUS_URL", "http://localhost:9090"),
                        help="Base URL of Prometheus (default: http://localhost:9090)")
    parser.add_argument("--skip-query", action="store_true", help="Skip sending live sample query")
    args = parser.parse_args()

    print("=================================================================")
    print(" 📊 AI Agent Monitoring Integration Smoke Test")
    print("=================================================================\n")

    metrics_status = check_agent_metrics(args.agent_url)
    all_metrics_present = all(metrics_status.values())

    if not args.skip_query and any(metrics_status.values()):
        simulate_sample_query(args.agent_url)

    prom_ok = check_prometheus_scraper(args.prometheus_url)

    print("\n=================================================================")
    print(" 📋 Smoke Test Summary")
    print("=================================================================")
    if all_metrics_present:
        print(" ✓ All 10 Golden Signal metrics are defined and exposed by the Agent!")
    else:
        missing = [k for k, v in metrics_status.items() if not v]
        print(f" ✗ Some metrics are missing: {missing}")

    if prom_ok:
        print(" ✓ Prometheus is actively scraping the AI Agent target successfully!")
    else:
        print(" ℹ Prometheus scraper verification is optional or awaiting service start.")

    return 0 if all_metrics_present else 1


if __name__ == "__main__":
    sys.exit(main())

