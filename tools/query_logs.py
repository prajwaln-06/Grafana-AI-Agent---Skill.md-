#!/usr/bin/env python3
"""
Query generated logs (spec Phase 10) -- works against either a real
OpenSearch cluster or the local file-sink output, so you can validate
queryability without standing up the full Docker stack.

Examples:
    # against the file sink written by `logsim.main --sink file`
    python3 -m tools.query_logs --backend file --node node-02 --since -5m

    # against real OpenSearch
    python3 -m tools.query_logs --backend opensearch \\
        --url https://localhost:9200 --node node-02 --severity WARN \\
        --service dcgm-exporter
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")


def parse_relative_time(s):
    """'-5m', '-1h', '-30s' -> datetime; anything else parsed as ISO8601."""
    m = re.match(r"^-(\d+)([smh])$", s)
    if not m:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    n, unit = int(m.group(1)), m.group(2)
    delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n), "h": timedelta(hours=n)}[unit]
    return datetime.now(timezone.utc) - delta


def query_file(data_dir, node, service, severity, since, until, index_prefix, limit):
    results = []
    pattern = os.path.join(data_dir, f"{index_prefix or ''}*.ndjson")
    for path in sorted(glob.glob(pattern)):
        index_name = os.path.basename(path).rsplit(".ndjson", 1)[0]
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                doc = json.loads(line)
                res = doc.get("Resource", {})
                if node and res.get("host.name") != node:
                    continue
                if service and res.get("service.name") != service:
                    continue
                if severity and doc.get("Severity", doc.get("SeverityText", "")).upper() != severity.upper():
                    continue
                ts = datetime.fromisoformat(doc["@timestamp"].replace("Z", "+00:00"))
                if since and ts < since:
                    continue
                if until and ts > until:
                    continue
                results.append((index_name, doc))
    results.sort(key=lambda t: t[1]["@timestamp"])
    return results[-limit:] if limit else results


def query_opensearch(url, username, password, node, service, severity,
                      since, until, index_prefix, limit):
    from opensearchpy import OpenSearch

    client = OpenSearch(hosts=[url], http_auth=(username, password),
                         use_ssl=url.startswith("https"), verify_certs=False,
                         ssl_show_warn=False)
    must = []
    if node:
        must.append({"term": {"Resource.host.name": node}})
    if service:
        must.append({"term": {"Resource.service.name": service}})
    if severity:
        must.append({"bool": {"should": [
            {"term": {"Severity": severity}}, {"term": {"SeverityText": severity}},
        ]}})
    rng = {}
    if since:
        rng["gte"] = since.isoformat()
    if until:
        rng["lte"] = until.isoformat()
    if rng:
        must.append({"range": {"@timestamp": rng}})

    body = {"query": {"bool": {"must": must}} if must else {"match_all": {}},
            "sort": [{"@timestamp": "asc"}], "size": limit or 100}
    index = f"{index_prefix}*" if index_prefix else "consolelog-*,syslog-*,heartbeat"
    resp = client.search(index=index, body=body)
    return [(hit["_index"], hit["_source"]) for hit in resp["hits"]["hits"]]


def main():
    p = argparse.ArgumentParser(description="Query generated OpenSearch-shaped logs")
    p.add_argument("--backend", choices=["file", "opensearch"], default="file")
    p.add_argument("--data-dir", default="./data/logsim")
    p.add_argument("--url", default="https://localhost:9200")
    p.add_argument("--username", default="admin")
    p.add_argument("--password", default="admin")
    p.add_argument("--node")
    p.add_argument("--service")
    p.add_argument("--severity")
    p.add_argument("--since", help="e.g. -5m, -1h, or an ISO8601 timestamp")
    p.add_argument("--until")
    p.add_argument("--index-prefix", help="e.g. syslog, consolelog, heartbeat")
    p.add_argument("--limit", type=int, default=50)
    args = p.parse_args()

    since = parse_relative_time(args.since) if args.since else None
    until = parse_relative_time(args.until) if args.until else None

    if args.backend == "file":
        results = query_file(args.data_dir, args.node, args.service, args.severity,
                              since, until, args.index_prefix, args.limit)
    else:
        results = query_opensearch(args.url, args.username, args.password, args.node,
                                    args.service, args.severity, since, until,
                                    args.index_prefix, args.limit)

    if not results:
        print("No matching documents.")
        return

    for index_name, doc in results:
        res = doc.get("Resource", {})
        sev = doc.get("Severity", doc.get("SeverityText", "?"))
        print(f"[{doc['@timestamp']}] {index_name:20s} {res.get('host.name', '?'):10s} "
              f"{res.get('service.name', '?'):14s} {sev:6s} {doc.get('Body', '')}")


if __name__ == "__main__":
    main()
