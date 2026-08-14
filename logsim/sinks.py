"""
Pluggable output sinks for generated log documents.

OpenSearchSink is the real target (used in docker-compose). FileSink and
MemorySink let the rest of the simulator (scheduling, correlation,
background noise) be built and tested in this sandbox without a running
OpenSearch cluster or Docker -- same document shape either way, so
switching sinks is a one-flag change (`--sink`).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone


def index_name_for(index_base: str, ts: datetime) -> str:
    """consolelog/syslog are daily-rotated indices; heartbeat is not."""
    if index_base == "heartbeat":
        return "heartbeat"
    return f"{index_base}-{ts.strftime('%Y.%m.%d')}"


# Explicit mapping (spec section 6): Resource.host.name/service.name and
# Severity are `keyword` (exact-match, not analyzed) so an agent's term
# queries behave predictably -- e.g. a term match on "node-02" won't get
# silently tokenized into "node"/"02" by the default text analyzer. Body
# stays `text` since it's meant for full-text search (spec section 16
# example: {"match": {"Body": "GPU"}}).
LOG_INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "@timestamp": {"type": "date"},
            "Timestamp": {"type": "date"},
            "Severity": {"type": "keyword"},
            "SeverityText": {"type": "keyword"},
            "Body": {"type": "text"},
            "@version": {"type": "keyword"},
            "Resource": {
                "properties": {
                    "host.name": {"type": "keyword"},
                    "service.name": {"type": "keyword"},
                }
            },
            "Attributes": {"type": "object", "enabled": True},
        }
    }
}


class BaseSink:
    def index(self, index_base: str, doc: dict):
        raise NotImplementedError

    def ensure_ready(self, index_bases):
        """Optional: block until the backend is reachable and the given
        index bases exist. Default no-op for sinks that don't need it."""
        pass

    def close(self):
        pass


class MemorySink(BaseSink):
    """Keeps everything in a list, in-process. Used by unit tests."""

    def __init__(self):
        self.docs: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def index(self, index_base: str, doc: dict):
        ts = datetime.fromisoformat(doc["@timestamp"].replace("Z", "+00:00"))
        full_index = index_name_for(index_base, ts)
        with self._lock:
            self.docs.append((full_index, doc))

    def query(self, node=None, service=None, severity=None,
              since=None, until=None, index_prefix=None):
        out = []
        for full_index, doc in self.docs:
            if index_prefix and not full_index.startswith(index_prefix):
                continue
            res = doc.get("Resource", {})
            if node and res.get("host.name") != node:
                continue
            if service and res.get("service.name") != service:
                continue
            if severity and doc.get("Severity", "").upper() != severity.upper():
                continue
            ts = datetime.fromisoformat(doc["@timestamp"].replace("Z", "+00:00"))
            if since and ts < since:
                continue
            if until and ts > until:
                continue
            out.append((full_index, doc))
        return out


class FileSink(BaseSink):
    """Appends newline-delimited JSON per index to disk. Lets you run the
    full simulator end-to-end (`python3 -m logsim.main --sink file`) and
    inspect/query results with tools/query_logs.py, with zero external
    services -- useful for local dev before standing up real OpenSearch."""

    def __init__(self, data_dir="./data/logsim"):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self._lock = threading.Lock()

    def index(self, index_base: str, doc: dict):
        ts = datetime.fromisoformat(doc["@timestamp"].replace("Z", "+00:00"))
        full_index = index_name_for(index_base, ts)
        path = os.path.join(self.data_dir, f"{full_index}.ndjson")
        with self._lock:
            with open(path, "a") as f:
                f.write(json.dumps(doc) + "\n")


class OpenSearchSink(BaseSink):
    """Real sink, used in docker-compose. Bulk-friendly but indexes
    individually here since log volume is low (simulator, not prod)."""

    def __init__(self, url, username=None, password=None, verify_certs=False):
        from opensearchpy import OpenSearch  # imported lazily so the file/
                                              # memory sinks work without
                                              # this dependency installed
        auth = (username, password) if username else None
        self.client = OpenSearch(
            hosts=[url],
            http_auth=auth,
            use_ssl=url.startswith("https"),
            verify_certs=verify_certs,
            ssl_show_warn=False,
        )
        self._ensured = set()

    def ensure_ready(self, index_bases, max_wait_s=None):
        """Block (retrying indefinitely, or up to max_wait_s if given)
        until OpenSearch responds, then pre-create the given index bases
        with the explicit mapping. This is what makes container startup
        deterministic (spec section 5) -- logsim never starts generating
        logs against a cluster that isn't actually up yet, and `_cat/indices`
        shows real indices immediately rather than only after the first
        document happens to be written."""
        start = time.time()
        attempt = 0
        while True:
            try:
                if self.client.ping():
                    break
            except Exception:
                pass
            attempt += 1
            if max_wait_s is not None and time.time() - start > max_wait_s:
                raise TimeoutError(f"OpenSearch not reachable after {max_wait_s}s")
            print(f"[logsim] Waiting for OpenSearch... (attempt {attempt})", flush=True)
            time.sleep(min(2 + attempt, 10))
        print("[logsim] OpenSearch ready.", flush=True)

        print("[logsim] Creating indices...", flush=True)
        now = datetime.now(timezone.utc)
        for base in index_bases:
            self._ensure_index(index_name_for(base, now))
        print("[logsim] Indices ready.", flush=True)

    def _ensure_index(self, index_name):
        if index_name in self._ensured:
            return
        try:
            if not self.client.indices.exists(index=index_name):
                self.client.indices.create(index=index_name, body=LOG_INDEX_MAPPING,
                                            ignore=400)
        except Exception as e:  # noqa: BLE001
            print(f"[logsim] warning: could not pre-create index {index_name}: {e}")
        self._ensured.add(index_name)

    def index(self, index_base: str, doc: dict):
        ts = datetime.fromisoformat(doc["@timestamp"].replace("Z", "+00:00"))
        full_index = index_name_for(index_base, ts)
        self._ensure_index(full_index)
        self.client.index(index=full_index, body=doc)


def make_sink(kind: str, **kwargs) -> BaseSink:
    if kind == "opensearch":
        return OpenSearchSink(**kwargs)
    if kind == "file":
        return FileSink(**{k: v for k, v in kwargs.items() if k == "data_dir"})
    if kind == "memory":
        return MemorySink()
    raise ValueError(f"unknown sink kind: {kind}")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
