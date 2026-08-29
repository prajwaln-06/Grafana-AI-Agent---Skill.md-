#!/usr/bin/env python3
"""
scripts/smoke_test_grafana.py

Only relevant if you're turning on ALERT_RULE_CREATION_ENABLED (SKILL.md
§12). Run this before trying a real alert-creation question through the
API -- it checks the three things that have to be true for
app/grafana_client.py to succeed, in order, so a failure here tells you
unambiguously which piece of GRAFANA_* configuration is wrong instead of
debugging a confusing 500/502 from POST /api/v1/alerts/confirm later.

This script only ever makes READ calls (GET) against Grafana -- it never
creates, modifies, or deletes anything. Creating a real alert rule this way
is intentionally left to actually exercising POST /api/v1/alerts/confirm
through the API once these three checks pass.

Usage:
    GRAFANA_URL=http://localhost:3000 \\
    GRAFANA_SERVICE_ACCOUNT_TOKEN=glsa_... \\
    GRAFANA_DEFAULT_FOLDER_UID=... \\
    GRAFANA_DEFAULT_DATASOURCE_UID=... \\
        python3 scripts/smoke_test_grafana.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from dotenv import load_dotenv  # noqa: E402

load_dotenv()


def main() -> int:
    base_url = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
    token = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN")
    folder_uid = os.environ.get("GRAFANA_DEFAULT_FOLDER_UID")
    datasource_uid = os.environ.get("GRAFANA_DEFAULT_DATASOURCE_UID")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    print(f"Testing against {base_url}\n")

    if not token:
        print("FAILED before making any request: GRAFANA_SERVICE_ACCOUNT_TOKEN is unset. "
              "Create a service account under Grafana -> Administration -> Service accounts "
              "with at least the 'Alert Rule Writer' role, generate a token, and set it.")
        return 1

    print("1. Auth check -- GET /api/v1/provisioning/alert-rules (lists existing rules; "
          "confirms the token itself is valid and has provisioning-API access)...")
    try:
        resp = requests.get(f"{base_url}/api/v1/provisioning/alert-rules", headers=headers, timeout=15)
    except requests.exceptions.RequestException as e:
        print(f"   FAILED: could not reach {base_url}: {e}")
        print("\nCheck GRAFANA_URL and that Grafana is actually running and reachable from here.")
        return 1
    if resp.status_code == 401:
        print("   FAILED: 401 Unauthorized -- the token is invalid, expired, or revoked.")
        return 1
    if resp.status_code == 403:
        print("   FAILED: 403 Forbidden -- the token is valid but lacks alert-provisioning "
              "permissions. Give the service account the 'Alert Rule Writer' role (or higher).")
        return 1
    if resp.status_code != 200:
        print(f"   FAILED: unexpected HTTP {resp.status_code}: {resp.text[:300]}")
        return 1
    existing_count = len(resp.json()) if isinstance(resp.json(), list) else "?"
    print(f"   OK -- token is valid, {existing_count} existing alert rule(s) visible.\n")

    if not folder_uid:
        print("2. Folder check -- SKIPPED: GRAFANA_DEFAULT_FOLDER_UID is unset.")
        print("\nSet GRAFANA_DEFAULT_FOLDER_UID and GRAFANA_DEFAULT_DATASOURCE_UID, then re-run "
              "this script, before turning ALERT_RULE_CREATION_ENABLED on.")
        return 1
    print(f"2. Folder check -- GET /api/folders/{folder_uid} ...")
    resp2 = requests.get(f"{base_url}/api/folders/{folder_uid}", headers=headers, timeout=15)
    if resp2.status_code == 200:
        print(f"   OK -- folder {resp2.json().get('title', '(untitled)')!r} exists.\n")
    else:
        print(f"   FAILED: HTTP {resp2.status_code} -- this folder UID doesn't exist, or the "
              f"token can't see it. Find the correct UID under Grafana -> Alerting -> Alert "
              f"rules -> (your folder); it's in the URL, not the folder's display name.")
        return 1

    if not datasource_uid:
        print("3. Datasource check -- SKIPPED: GRAFANA_DEFAULT_DATASOURCE_UID is unset.")
        return 1
    print(f"3. Datasource check -- GET /api/datasources/uid/{datasource_uid} ...")
    resp3 = requests.get(f"{base_url}/api/datasources/uid/{datasource_uid}", headers=headers, timeout=15)
    if resp3.status_code != 200:
        print(f"   FAILED: HTTP {resp3.status_code} -- this datasource UID doesn't exist, or "
              f"the token can't see it.")
        return 1
    ds = resp3.json()
    if ds.get("type") != "prometheus":
        print(f"   FAILED: this datasource is type {ds.get('type')!r}, not 'prometheus' -- "
              f"app/grafana_client.py's condition queries are PromQL and must run against a "
              f"Prometheus datasource.")
        return 1
    print(f"   OK -- datasource {ds.get('name')!r} is a Prometheus datasource.")
    print(f"   Double-check its URL ({ds.get('url')!r}) points at the SAME Prometheus this "
          f"backend queries via PROMETHEUS_URL -- this script cannot verify that for you; a "
          f"mismatch here fails silently (a proposed rule would evaluate against the wrong "
          f"series, not error out).\n")

    print("All checks passed. Safe to set ALERT_RULE_CREATION_ENABLED=true and try a real "
          "alert-creation question through the API.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
