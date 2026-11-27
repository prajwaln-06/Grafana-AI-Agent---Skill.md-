import asyncio
import json
import logging
import threading
from contextlib import aclosing
from types import SimpleNamespace

from app import agent, llm_client, pipeline
from app.opensearch_catalog import runtime, timings
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
    assert fresh["cache_store_ms"] is None
    assert all(value is not None and value >= 0 for key, value in fresh.items() if key != "cache_store_ms")
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


def test_response_does_not_wait_for_cache_writes(monkeypatch, settings, skill_index, caplog):
    settings = settings.model_copy(update={"opensearch_query_plan_cache_enabled": True,
                                           "opensearch_result_cache_enabled": True})
    install_rows_fixture(monkeypatch, settings, skill_index)
    started, release = threading.Event(), threading.Event()
    stored_plans = []

    def store_answer(*args, **kwargs):
        started.set()
        assert release.wait(3)
        raise RuntimeError("store failed")

    monkeypatch.setattr(runtime, "store_executed_result", store_answer)
    monkeypatch.setattr(runtime, "store_validated_plan", lambda *a, **kw: stored_plans.append(a[0]))
    question = "Show service logs with the fan off"
    context = SimpleNamespace(user_content=SimpleNamespace(parts=[SimpleNamespace(text=question)]),
                              session=SimpleNamespace(state={}, user_id="viewer"), invocation_id="background",
                              app_name="app")

    async def ask():
        try:
            async with aclosing(agent.root_agent._run_async_impl(context)) as events:
                event = await asyncio.wait_for(anext(events), 2)
                assert await asyncio.to_thread(started.wait, 2)
                result = json.loads(event.content.parts[0].text)["result"]
                assert result["execution"]["metadata"]["timings"]["cache_store_ms"] is None
                assert not stored_plans
        finally:
            release.set()
            await asyncio.gather(*tuple(agent._cache_write_tasks))

    asyncio.run(ask())
    assert stored_plans == [question]
    assert "Background OpenSearch cache write failed" in caplog.text
