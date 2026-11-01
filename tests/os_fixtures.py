"""
Shared helpers for the OpenSearch analysis / mentor-question tests.

FakeCluster replaces ONLY the HTTP session used by app/opensearch_client.py, so
the REAL client code runs in every test (URL building, `_mapping` and `_search`
calls, response parsing, empty/success/error classification). `_mapping`
requests are answered from the SYNTHETIC fixture mappings in
tests/fixtures/opensearch/ (see its README: they are not real exports); `_search`
requests are answered by a per-test handler(index, dsl) -> raw response body.
Every search body the code sent is recorded in `searches`.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from app import opensearch_client as osc

FIX = Path(__file__).resolve().parent / "fixtures" / "opensearch"
INDEX = {"fabric_health": "slingshot_crayfabrichealth-*", "controller_log": "controllerlog-*", "ilo_events": "log_iml-*"}
REF = {"fabric_health": "references/opensearch-logs/fabric-health.md",
       "controller_log": "references/opensearch-logs/controller-log.md",
       "ilo_events": "references/opensearch-logs/ilo-events.md"}
_MAPPING_FILE = {INDEX[k]: f"mapping_{k}.json" for k in INDEX}


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(str(self.status_code))


def error(status: int, reason: str) -> _Resp:
    """A handler may return this to make the cluster answer with an HTTP error."""
    return _Resp(status, {"error": {"type": "search_phase_execution_exception", "reason": reason}})


class FakeCluster:
    def __init__(self, handler=None, mapping_status: int = 200):
        self.handler = handler
        self.mapping_status = mapping_status
        self.searches: list[tuple[str, dict]] = []
        self.mapping_calls = 0

    def _get(self, url, timeout=None, auth=None, **_):
        if url.endswith("/_mapping"):
            self.mapping_calls += 1
            if self.mapping_status != 200:
                return _Resp(self.mapping_status, {"error": "boom"})
            pattern = url.rsplit("/_mapping", 1)[0].rsplit("/", 1)[1]
            name = _MAPPING_FILE.get(pattern)
            return _Resp(200, json.loads((FIX / name).read_text()) if name else {})
        return _Resp(404, {})

    def _post(self, url, json=None, timeout=None, auth=None, **_):
        index = url.rsplit("/_search", 1)[0].rsplit("/", 1)[1]
        self.searches.append((index, json))
        body = self.handler(index, json) if self.handler else hits_body([])
        return body if isinstance(body, _Resp) else _Resp(200, body)

    def install(self, monkeypatch):
        session = MagicMock()
        session.get.side_effect = self._get
        session.post.side_effect = self._post
        monkeypatch.setattr(osc, "_get_session", lambda: session)
        return self


SHARDS_OK = {"total": 3, "successful": 3, "skipped": 0, "failed": 0}


def hits_body(docs, total=None, relation="eq", index="controllerlog-2026.10.28", ids=None, timed_out=False):
    return {"took": 3, "timed_out": timed_out, "_shards": dict(SHARDS_OK),
            "hits": {"total": {"value": len(docs) if total is None else total, "relation": relation},
                     "hits": [{"_index": index, "_id": (ids[i] if ids else str(i)), "_source": d}
                              for i, d in enumerate(docs)]}}


def agg_body(aggs, total=0, relation="eq"):
    return {"took": 3, "timed_out": False, "_shards": dict(SHARDS_OK),
            "hits": {"total": {"value": total, "relation": relation}, "hits": []}, "aggregations": aggs}


def window(gte="now-24h", lte="now", field="@timestamp"):
    return {"range": {field: {"gte": gte, "lte": lte}}}


def os_entry(pattern_id, query, **extra):
    """What the Generator writes for an OpenSearch result, in the friend's contract
    (pattern_id + raw_metric; never an index)."""
    e = {"status": "ok", "reference_used": REF[pattern_id], "data_source": "opensearch", "pattern_id": pattern_id,
         "measurement_used": {"type": "raw_metric", "name": f"{pattern_id}_search", "source_metrics": []},
         "query": query, "explanation": "test"}
    e.update(extra)
    return e
