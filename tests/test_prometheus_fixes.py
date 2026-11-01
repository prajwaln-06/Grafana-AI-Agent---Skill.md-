"""Prometheus-side fixes: discovery cache + parallelism, reference-file cache, non-blocking LLM/exec calls."""
import threading
import time
from datetime import timedelta
from unittest.mock import patch

from app import label_discovery as ld


def _details(name):
    return ld.LabelDetails(keys=["instance"], values={})


def test_discovery_is_cached_per_metric_and_failures_are_not_cached():
    calls = []

    def fake(base_url, name, lookback=None, timeout=None):
        calls.append(name)
        return None if name == "bad" else _details(name)
    with patch.object(ld, "discover_label_details_for_metric", side_effect=fake):
        ld.configure(ttl_seconds=60, max_workers=4)
        first = ld.discover_label_details_for_metrics("http://p", ["a", "b", "bad"], lookback=timedelta(hours=1))
        second = ld.discover_label_details_for_metrics("http://p", ["a", "b", "bad"], lookback=timedelta(hours=1))
    assert first["a"] and first["bad"] is None
    assert calls.count("a") == 1 and calls.count("b") == 1 and calls.count("bad") == 2   # failure retried, successes reused
    assert second["a"] == first["a"]


def test_discovery_ttl_zero_disables_the_cache_and_clear_resets_it():
    calls = []
    with patch.object(ld, "discover_label_details_for_metric", side_effect=lambda *a, **k: calls.append(1) or _details("x")):
        ld.configure(ttl_seconds=0)
        ld.discover_label_details_for_metrics("http://p", ["a"])
        ld.discover_label_details_for_metrics("http://p", ["a"])
        assert len(calls) == 2
        ld.configure(ttl_seconds=60)
        ld.discover_label_details_for_metrics("http://p", ["a"])
        ld.discover_label_details_for_metrics("http://p", ["a"])
        assert len(calls) == 3
        ld.clear_cache()
        ld.discover_label_details_for_metrics("http://p", ["a"])
        assert len(calls) == 4
    ld.configure(ttl_seconds=300, max_workers=8)


def test_discovery_runs_misses_in_parallel_and_keeps_order():
    active, peak = [0], [0]
    lock = threading.Lock()

    def slow(base_url, name, lookback=None, timeout=None):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return _details(name)
    names = [f"m{i}" for i in range(8)]
    with patch.object(ld, "discover_label_details_for_metric", side_effect=slow):
        ld.configure(ttl_seconds=0, max_workers=4)
        t0 = time.time()
        out = ld.discover_label_details_for_metrics("http://p", names)
        elapsed = time.time() - t0
    assert list(out) == names and peak[0] > 1 and elapsed < 0.3          # serial would take 0.4s
    ld.configure(ttl_seconds=300, max_workers=8)


def test_duplicate_metric_names_are_fetched_once():
    calls = []
    with patch.object(ld, "discover_label_details_for_metric", side_effect=lambda b, n, **k: calls.append(n) or _details(n)):
        ld.configure(ttl_seconds=0)
        out = ld.discover_label_details_for_metrics("http://p", ["a", "a", "b"])
    assert calls.count("a") == 1 and set(out) == {"a", "b"}
    ld.configure(ttl_seconds=300)


def test_pipeline_configures_discovery_from_settings(skill_index, settings):
    """The pipeline hands the cache TTL and worker count from Settings to label discovery."""
    from app import pipeline
    s = settings.model_copy(update={"label_discovery_cache_ttl_seconds": 12.0, "label_discovery_max_workers": 3})
    matched = [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}]
    with patch.object(ld, "configure") as configure, \
            patch.object(ld, "discover_label_details_for_metrics", return_value={}):
        pipeline._build_generator_context(matched, skill_index, s, question="cpu")
    configure.assert_called_once_with(12.0, 3)


def test_blocking_llm_and_discovery_run_off_the_event_loop(skill_index, settings):
    """Slow LLM calls (router + generator) and slow label discovery must not freeze the event
    loop: a ticking task keeps running while the pipeline waits on them."""
    import asyncio
    from unittest.mock import MagicMock
    from app import llm_client, pipeline

    router = MagicMock(parsed={
        "gate_stop": None, "panic_mode": False, "unresolved_topics": [],
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}]})
    gen = MagicMock(parsed={
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_load1", "source_metrics": []},
        "data_source": "prometheus", "query": "node_load1", "query_type": "range",
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "t"})
    responses = [router, gen]

    def slow_llm(**kw):
        time.sleep(0.2)
        return responses.pop(0)

    def slow_discovery(*a, **k):
        time.sleep(0.2)
        return {"node_load1": ld.LabelDetails(keys=["instance"], values={})}
    ticks = []

    async def main():
        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.02)
        t = asyncio.create_task(ticker())
        result = await pipeline.run_pipeline("load average", skill_index, settings)
        t.cancel()
        return result
    with patch.object(llm_client, "call_llm_json", side_effect=slow_llm), \
            patch.object(ld, "discover_label_details_for_metrics", side_effect=slow_discovery):
        result = asyncio.run(main())
    assert result["status"] == "ok"
    assert len(ticks) >= 15, f"event loop was blocked (only {len(ticks)} ticks in ~0.6s of blocking work)"


def test_agent_executes_contracts_off_the_event_loop():
    """app/agent.py runs execute_contract (blocking HTTP) through asyncio.to_thread."""
    import inspect
    from app import agent
    src = inspect.getsource(agent.ObservabilityQueryBuilderAgent._run_async_impl)
    assert "asyncio.to_thread(executor.execute_contract" in src
    assert "asyncio.to_thread(\n                        grafana_client.create_alert_rule" in src
    assert hasattr(agent.ObservabilityQueryBuilderAgent, "_get_skill_index_or_reload")
    assert not hasattr(agent.ObservabilityQueryBuilderAgent, "_get_get_skill_index_or_reload")


def test_reference_reads_are_cached_until_the_file_changes(skill_index, tmp_path):
    from app import skill_index as si
    f = tmp_path / "r.md"
    f.write_text("one")
    assert si._read_text_cached(str(f)) == "one"
    with patch("builtins.open", side_effect=AssertionError("should come from cache")):
        assert si._read_text_cached(str(f)) == "one"
    f.write_text("two!")                                                   # size changes -> reread
    assert si._read_text_cached(str(f)) == "two!"
    ref = next(iter(skill_index.routing_rows)).reference_path
    assert skill_index.read_reference(ref) == skill_index.read_reference(ref)
