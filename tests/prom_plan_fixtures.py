"""Shared fixtures/fakes for the Prometheus query-plan cache tests.

Real: the agent's `_run_async_impl`, the real SkillIndex over the real skills/ tree, the real committed
metric catalog (app/catalog/catalog.json through the catalog runtime), the real validator, executor,
normalizer, label discovery (cache + parsing) and the real Prometheus client's URL/params handling up to
the HTTP call. Faked: only the HTTP layer of Prometheus (`FakePrometheus`) and the Router/Generator
(`run_pipeline`, a counting stub, via the OpenSearch tests' `AgentRun`).
"""
from __future__ import annotations

import copy
from pathlib import Path

from app import label_discovery, prometheus_client

REPO = Path(__file__).resolve().parent.parent
REAL_CATALOG = REPO / "app" / "catalog" / "catalog.json"

REF_CPU = "references/node-exporter/cpu.md"
REF_MEM = "references/node-exporter/memory.md"

Q_CPU = "Show CPU utilization for all nodes."
Q_CPU_REWORDED = "What is the CPU usage across all nodes?"

CPU_QUERY = '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
NODE1, NODE2 = "node-01:9100", "node-02:9100"


def cpu_contract(query: str = CPU_QUERY, metric: str = "node_cpu_seconds_total", ref: str = REF_CPU, *,
                 window_from: str = "now-1h", **over) -> dict:
    c = {
        "mode": "single", "status": "ok", "reference_used": ref,
        "measurement_used": {"type": "raw_metric", "name": metric, "source_metrics": []},
        "data_source": "prometheus", "query": query, "query_type": "range",
        "time_range": {"from": window_from, "to": "now", "step": "60s"},
        "explanation": "Average non-idle CPU per node.",
    }
    c.update(over)
    return c


def mem_contract(query: str = "node_memory_MemAvailable_bytes", **over) -> dict:
    return cpu_contract(query, "node_memory_MemAvailable_bytes", REF_MEM, **over)


def instant_contract(query: str = "node_load1", metric: str = "node_load1", **over) -> dict:
    c = cpu_contract(query, metric, query_type="instant", time_range={"time": "now"})
    c.update(over)
    return c


def default_series() -> dict[str, list[dict]]:
    def s(metric, **labels):
        return {"__name__": metric, **labels}
    cpu = [s("node_cpu_seconds_total", instance=i, job="node", cpu=c, mode=m)
           for i in (NODE1, NODE2) for c in ("0", "1") for m in ("idle", "user", "system")]
    flat = lambda m: [s(m, instance=i, job="node") for i in (NODE1, NODE2)]       # noqa: E731
    return {"node_cpu_seconds_total": cpu, "node_load1": flat("node_load1"),
            "node_memory_MemAvailable_bytes": flat("node_memory_MemAvailable_bytes")}


class FakePrometheus:
    """The HTTP layer of Prometheus: query_range/query and /api/v1/series."""

    def __init__(self):
        self.queries: list[tuple[str, str]] = []          # (path, promql) -- every data query
        self.series = default_series()
        self.series_calls: list[str] = []
        self.value = "12.5"
        self.mode = "ok"                                  # ok | empty | error
        self.fail_labels = False

    def install(self, monkeypatch) -> "FakePrometheus":
        monkeypatch.setattr(prometheus_client, "_get", self._get)
        monkeypatch.setattr(label_discovery, "_fetch_series", self._fetch_series)
        return self

    def _get(self, base_url, path, params, timeout):
        self.queries.append((path, params["query"]))
        if self.mode == "error":
            return prometheus_client.ExecutionOutcome(status="endpoint_error", error="boom")
        if self.mode == "empty":
            return prometheus_client.ExecutionOutcome(status="empty_result",
                                                      raw_data={"resultType": "matrix", "result": []})
        if path.endswith("/query"):
            return prometheus_client.ExecutionOutcome(status="success", raw_data={
                "resultType": "vector", "result": [{"metric": {"instance": NODE1}, "value": [1735689600, self.value]}]})
        return prometheus_client.ExecutionOutcome(status="success", raw_data={
            "resultType": "matrix",
            "result": [{"metric": {"instance": NODE1}, "values": [[1735689600, self.value], [1735689660, self.value]]}]})

    def _fetch_series(self, base_url, metric_name, lookback, timeout):
        self.series_calls.append(metric_name)
        if self.fail_labels:
            return None
        return copy.deepcopy(self.series.get(metric_name, []))

    @property
    def data_queries(self) -> int:
        return len(self.queries)
