import json
import logging

from app import agent, llm_client, pipeline
from app.opensearch_catalog import timings
from tests.os_fixtures import REF, FakeCluster, hits_body, os_entry, window
from tests.test_opensearch_result_cache import AgentRun


def install_rows_fixture(monkeypatch, settings, skill_index):
    cluster = FakeCluster(lambda *_: hits_body([{"Body": "fan off rpm"}])).install(monkeypatch)
    run = AgentRun(monkeypatch, settings, lambda _: None)
    run.skill_index = skill_index
    monkeypatch.setattr(agent, "run_pipeline", pipeline.run_pipeline)
    route = {"matched_references": [{"reference_path": REF["controller_log"], "data_source": "opensearch"}]}
    query = {"size": 5, "query": {"bool": {"filter": [window(), {"match": {"Body": "fan off rpm"}}]}}}
    generated = {"mode": "single", **os_entry("controller_log", query)}
    calls = []

    def complete(**kwargs):
        parsed = (route, generated)[len(calls) % 2]
        calls.append(kwargs)
        return llm_client.LLMResponse(text="", parsed=parsed)

    monkeypatch.setattr(llm_client, "call_llm_json", complete)
    return run, cluster, calls


def test_fresh_search_and_result_cache_hit_have_per_request_timings(monkeypatch, settings, skill_index, caplog):
    caplog.set_level(logging.INFO, logger=timings.logger.name)
    run, cluster, calls = install_rows_fixture(monkeypatch, settings, skill_index)
    question = "Show service logs with the fan off"
    first, _ = run.ask(question)
    fresh = first["execution"]["metadata"]["timings"]
    assert set(fresh) == {*timings.STAGES, "total_ms"}
    assert all(value is not None and value >= 0 for value in fresh.values())
    assert fresh["opensearch_took_ms"] == 3
    assert fresh["total_ms"] >= fresh["router_ms"]

    second, _ = run.ask(question)
    cached = second["execution"]["metadata"]["timings"]
    assert cached["router_ms"] is None and cached["generator_ms"] is None and cached["context_ms"] is None
    assert cached["search_client_ms"] is None and cached["opensearch_took_ms"] is None
    assert cached["guard_ms"] is not None and cached["cache_store_ms"] is None
    assert len(cluster.searches) == 1 and len(calls) == 2
    logged = [json.loads(record.getMessage().split(" ", 1)[1])["timings"]
              for record in caplog.records if record.name == timings.logger.name]
    assert logged == [fresh, cached]
