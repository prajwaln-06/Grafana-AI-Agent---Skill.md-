"""Reproducible demo of the Prometheus query-plan cache through the REAL agent pipeline.

    python scripts/demo_prometheus_plan_cache.py

Real: app.agent, the skill index, the committed catalog, the validator, the executor, label discovery.
Stubbed (no network needed): the Router/Generator LLM call (a counting stub) and Prometheus' HTTP layer.
Every line printed comes from the response metadata and the stub counters; nothing is asserted by hand.
For a live server see docs in the final report (curl against POST /api/v1/query).
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("GEMINI_API_KEY", "demo-not-real")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import agent as agent_mod, label_discovery, prometheus_client, prometheus_plan_cache as ppc  # noqa: E402
from app.catalog import runtime as catalog_runtime  # noqa: E402
from app.config import Settings  # noqa: E402
from app.skill_index import SkillIndex  # noqa: E402

Q1 = "Show CPU utilization for all nodes."
Q2 = "What is the CPU usage across all nodes?"
PLAN = {"mode": "single", "status": "ok", "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
        "data_source": "prometheus", "query_type": "range",
        "query": '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "CPU busy %"}
SERIES = [{"__name__": "node_cpu_seconds_total", "instance": i, "job": "node", "cpu": c, "mode": m}
          for i in ("n1:9100", "n2:9100") for c in "01" for m in ("idle", "user", "system")]

state = SimpleNamespace(router_generator_calls=0, prometheus_queries=0, value=10.0)


def fake_get(base_url, path, params, timeout):
    state.prometheus_queries += 1
    return prometheus_client.ExecutionOutcome(status="success", raw_data={"resultType": "matrix", "result": [
        {"metric": {"instance": "n1:9100"}, "values": [[1735689600, str(state.value)]]}]})


async def fake_pipeline(question, skill_index, settings, context=None, catalog=None):
    state.router_generator_calls += 1
    return copy.deepcopy(PLAN)


def ask(settings, skill_index, question):
    agent_mod.get_settings = lambda: settings
    agent_mod.ObservabilityQueryBuilderAgent._get_skill_index_or_reload = lambda _self: skill_index
    ctx = SimpleNamespace(user_content=SimpleNamespace(parts=[SimpleNamespace(text=question)]),
                          session=SimpleNamespace(state={}, user_id="demo"), invocation_id="i", app_name="demo")

    async def go():
        return [e async for e in agent_mod.root_agent._run_async_impl(ctx)]
    ev = asyncio.run(go())
    return json.loads(ev[0].content.parts[0].text)["result"]


def case(title, settings, skill_index, question, expect):
    r0, p0 = state.router_generator_calls, state.prometheus_queries
    res = ask(settings, skill_index, question)
    meta = res["execution"].get("metadata", {}).get("query_plan_cache")
    print(f"\n### {title}\nquestion : {question}\nexpected : {expect}\nmetadata : {json.dumps(meta)}")
    print(f"router+generator ran: {state.router_generator_calls - r0 > 0}   "
          f"prometheus executed: {state.prometheus_queries - p0 > 0}   "
          f"value returned: {res['execution']['series'][0]['points'][0]['value']}")


def main():
    prometheus_client._get = fake_get
    label_discovery._fetch_series = lambda *a, **k: copy.deepcopy(SERIES)
    agent_mod.run_pipeline = fake_pipeline
    base = Settings(gemini_api_key="demo", skills_root=ROOT / "skills", prometheus_url="http://localhost:9090",
                    catalog_path=ROOT / "app" / "catalog" / "catalog.json",
                    prometheus_query_plan_cache_enabled=True,
                    prometheus_query_plan_cache_aliases={"node-exporter": {"usage": "utilization"}})
    si = SkillIndex.load(base.skills_root, base.catalog_path,
                         catalog_provider=lambda: catalog_runtime.get_runtime(base).catalog())
    ppc.reset()
    case("1. first query", base, si, Q1, "hit=false, stored=true; Router+Generator ran; Prometheus executed")
    state.value = 20.0
    case("2. same query", base, si, Q1, "hit=true match=exact; no Router/Generator; Prometheus executed again (fresh value)")
    case("3. reworded query", base, si, Q2, "hit=true match=semantic (alias usage->utilization declared); no Router/Generator")
    label_discovery.clear_cache()
    changed = copy.deepcopy(SERIES)
    for s in changed:
        s.pop("cpu", None)
    label_discovery._fetch_series = lambda *a, **k: copy.deepcopy(changed)
    case("4. label metadata changed (label key `cpu` gone)", base, si, Q1,
         "hit=false with invalidation_reason; plan rejected and evicted; Router+Generator ran; Prometheus executed")
    off = base.model_copy(update={"prometheus_query_plan_cache_enabled": False})
    case("5. cache disabled", off, si, Q1, "no query_plan_cache metadata (None); Router+Generator ran; Prometheus executed")
    print("\ncache snapshot:", json.dumps(ppc.snapshot(base)))


if __name__ == "__main__":
    main()
