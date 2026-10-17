from unittest.mock import patch

from fastapi.testclient import TestClient


def _client():
    from run_server import app
    return TestClient(app)


def test_healthz():
    with _client() as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readyz_reports_loaded_skill():
    with _client() as client:
        r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"
    assert r.json()["skill_name"] == "observability-query-builder"


def test_capabilities_exposes_routing_table():
    with _client() as client:
        r = client.get("/api/v1/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["skill_version"] == "1.4"
    assert len(body["routing_rows"]) >= 10


def test_query_happy_path_returns_executed_result():
    fake_contract = {
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_load1", "source_metrics": []},
        "data_source": "prometheus", "query": "node_load1",
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "test",
    }
    fake_executed = {**fake_contract, "execution": {"execution_status": "success", "result_type": "series", "series": []}}

    async def fake_run_pipeline(*a, **kw):
        return fake_contract

    with patch("app.agent.run_pipeline", side_effect=fake_run_pipeline), \
         patch("app.agent.executor.execute_contract", return_value=fake_executed):
        with _client() as client:
            r = client.post("/api/v1/query", json={"question": "load average on node-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["execution"]["execution_status"] == "success"
    assert body["session_id"] is None


def test_query_ambiguous_metric_returns_session_id_and_skips_execution():
    fake_contract = {
        "mode": "single", "status": "ambiguous_metric",
        "candidates": [{"name": "node_memory_MemAvailable_bytes", "purpose": "..."},
                        {"name": "node_memory_MemFree_bytes", "purpose": "..."}],
        "clarification": "Do you mean available memory or free memory?",
    }

    async def fake_run_pipeline(*a, **kw):
        return fake_contract

    with patch("app.agent.run_pipeline", side_effect=fake_run_pipeline) as mock_pipeline, \
         patch("app.agent.executor.execute_contract") as mock_execute:
        with _client() as client:
            r = client.post("/api/v1/query", json={"question": "how much memory is free"})
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] is not None
    assert not mock_execute.called  # never executes a query that still needs clarification


def test_query_follow_up_with_session_id_threads_prior_context():
    ambiguous_contract = {
        "mode": "single", "status": "ambiguous_metric",
        "candidates": [{"name": "a", "purpose": "x"}, {"name": "b", "purpose": "y"}],
        "clarification": "Which one?",
    }
    resolved_contract = {
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/memory.md",
        "measurement_used": {"type": "raw_metric", "name": "node_memory_MemAvailable_bytes", "source_metrics": []},
        "data_source": "prometheus", "query": "node_memory_MemAvailable_bytes",
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "resolved",
    }
    fake_executed = {**resolved_contract, "execution": {"execution_status": "success", "result_type": "series", "series": []}}

    call_log = []

    async def fake_run_pipeline(question, skill_index, settings, context=None):
        call_log.append(context)
        if context and context.clarification_answer:
            return resolved_contract
        return ambiguous_contract

    with patch("app.agent.run_pipeline", side_effect=fake_run_pipeline), \
         patch("app.agent.executor.execute_contract", return_value=fake_executed):
        with _client() as client:
            r1 = client.post("/api/v1/query", json={"question": "how much memory is free"})
            session_id = r1.json()["session_id"]

            r2 = client.post("/api/v1/query", json={"question": "available memory", "session_id": session_id})

    assert r2.status_code == 200
    assert r2.json()["result"]["status"] == "ok"
    assert call_log[1].previous_question == "how much memory is free"
    assert call_log[1].clarification_answer == "available memory"


def test_query_with_unknown_session_id_returns_410():
    with _client() as client:
        r = client.post("/api/v1/query", json={"question": "yes", "session_id": "does-not-exist"})
    assert r.status_code == 410


def test_multi_mode_with_one_ambiguous_entry_defers_all_execution():
    mixed_contract = {
        "mode": "multi",
        "results": [
            {"status": "ok", "reference_used": "references/node-exporter/memory.md",
             "measurement_used": {"type": "raw_metric", "name": "node_memory_SwapTotal_bytes", "source_metrics": []},
             "data_source": "prometheus", "query": "node_memory_SwapTotal_bytes",
             "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "..."},
            {"status": "ambiguous_metric", "reference_used": "references/node-exporter/memory.md",
             "candidates": [{"name": "node_memory_MemAvailable_bytes", "purpose": "..."},
                             {"name": "node_memory_MemFree_bytes", "purpose": "..."}],
             "clarification": "Available memory or free memory?", "explanation": "..."},
        ],
        "synthesis": None,
    }

    async def fake_run_pipeline(*a, **kw):
        return mixed_contract

    with patch("app.agent.run_pipeline", side_effect=fake_run_pipeline), \
         patch("app.agent.executor.execute_contract") as mock_execute:
        with _client() as client:
            r = client.post("/api/v1/query", json={"question": "show me swap and memory usage"})

    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] is not None  # a follow-up is now possible
    assert not mock_execute.called  # nothing executes until the whole multi-result is resolved
    assert body["result"] == mixed_contract  # returned as-is, not partially executed


def test_reload_skill_picks_up_a_new_routing_row(tmp_path, monkeypatch):
    refs = tmp_path / "references"
    refs.mkdir()
    (refs / "cpu.md").write_text("# CPU\n", encoding="utf-8")
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\nname: test-skill\ndescription: y\nmetadata:\n  version: \"1.0\"\n---\n"
        "## 4. Routing table\n"
        "| CPU | prometheus | [references/cpu.md](references/cpu.md) |\n", encoding="utf-8")
    from app.config import Settings
    monkeypatch.setattr("run_server.get_settings", lambda: Settings(gemini_api_key="test", skills_root=tmp_path))
    monkeypatch.setattr("app.agent.get_settings", lambda: Settings(gemini_api_key="test", skills_root=tmp_path))

    with _client() as client:
        from app.agent import root_agent
        root_agent.reload_skill()
        r0 = client.get("/api/v1/capabilities")
        assert len(r0.json()["routing_rows"]) == 1

        (refs / "memory.md").write_text("# Memory\n", encoding="utf-8")
        skill_md.write_text(
            "---\nname: test-skill\ndescription: y\nmetadata:\n  version: \"1.1\"\n---\n"
            "## 4. Routing table\n"
            "| CPU | prometheus | [references/cpu.md](references/cpu.md) |\n"
            "| Memory | prometheus | [references/memory.md](references/memory.md) |\n", encoding="utf-8")

        # Not yet visible -- no reload call made.
        r1 = client.get("/api/v1/capabilities")
        assert len(r1.json()["routing_rows"]) == 1

        r2 = client.post("/api/v1/admin/reload-skill")
        assert r2.status_code == 200
        assert r2.json()["routing_rows"] == 2
        assert r2.json()["skill_version"] == "1.1"

        r3 = client.get("/api/v1/capabilities")
        assert len(r3.json()["routing_rows"]) == 2


def test_reload_skill_keeps_previous_skill_on_bad_edit(tmp_path, monkeypatch):
    refs = tmp_path / "references"
    refs.mkdir()
    (refs / "cpu.md").write_text("# CPU\n", encoding="utf-8")
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\nname: test-skill\ndescription: y\nmetadata:\n  version: \"1.0\"\n---\n"
        "## 4. Routing table\n"
        "| CPU | prometheus | [references/cpu.md](references/cpu.md) |\n", encoding="utf-8")
    from app.config import Settings
    monkeypatch.setattr("run_server.get_settings", lambda: Settings(gemini_api_key="test", skills_root=tmp_path))
    monkeypatch.setattr("app.agent.get_settings", lambda: Settings(gemini_api_key="test", skills_root=tmp_path))

    with _client() as client:
        from app.agent import root_agent
        root_agent.reload_skill()
        # Break it: point a routing row at a file that doesn't exist.
        skill_md.write_text(
            "---\nname: test-skill\ndescription: y\nmetadata:\n  version: \"1.0\"\n---\n"
            "## 4. Routing table\n"
            "| CPU | prometheus | [references/missing.md](references/missing.md) |\n", encoding="utf-8")
        r = client.post("/api/v1/admin/reload-skill")
        assert r.status_code == 422

        # Previous, valid skill is still what's serving traffic.
        r2 = client.get("/api/v1/capabilities")
        assert len(r2.json()["routing_rows"]) == 1
        assert r2.json()["routing_rows"][0]["reference_path"] == "references/cpu.md"


def test_pipeline_exception_returns_502_not_a_crash():
    async def fake_run_pipeline(*a, **kw):
        raise RuntimeError("boom")

    with patch("app.agent.run_pipeline", side_effect=fake_run_pipeline):
        with _client() as client:
            r = client.post("/api/v1/query", json={"question": "anything"})
    assert r.status_code == 502


def test_pipeline_timeout_returns_504_not_a_hang():
    import asyncio

    async def fake_run_pipeline(*a, **kw):
        raise asyncio.TimeoutError()

    with patch("app.agent.run_pipeline", side_effect=fake_run_pipeline):
        with _client() as client:
            r = client.post("/api/v1/query", json={"question": "anything"})
    assert r.status_code == 504
    assert "timeout" not in r.json()["detail"].lower() or "took longer than" in r.json()["detail"]


def test_pipeline_timeout_is_actually_wired_to_settings():
    import asyncio

    from run_server import get_settings as route_get_settings

    short_timeout_settings = route_get_settings().model_copy(update={"pipeline_timeout_seconds": 0.01})

    async def slow_run_pipeline(*a, **kw):
        await asyncio.sleep(1)
        return {"mode": "single", "status": "ok"}

    with patch("run_server.get_settings", return_value=short_timeout_settings), \
         patch("app.agent.get_settings", return_value=short_timeout_settings), \
         patch("app.agent.run_pipeline", side_effect=slow_run_pipeline):
        with _client() as client:
            r = client.post("/api/v1/query", json={"question": "anything"})
    assert r.status_code == 504


def test_dependent_compound_query_returns_filtered_multi_result_with_synthesis_through_the_api(skills_root):
    """End to end through the real HTTP route and agent (only the LLM and
    Prometheus are scripted): the response carries both results, the
    dependent memory query filtered on the node the CPU query returned, a
    non-null synthesis, no leaked internal marker, and each query ran once
    (agent.py must not re-execute the already-executed contract)."""
    from app import label_discovery, llm_client, pipeline, prometheus_client
    from app.agent import root_agent
    from tests import test_pipeline as tp

    # The agent's SkillIndex needs a real catalog file; use the committed one
    # (so this also exercises Level-1 catalog narrowing against the real data).
    settings = tp._dependency_settings(
        skills_root, catalog_path=skills_root.parent / "app" / "catalog" / "catalog.json")
    llm = tp._ScriptedLLM(
        tp._router_resp(), tp._cpu_contract(),
        lambda prompt: tp._mem_contract(tp._dependent_filter_from_prompt(prompt)),
    )
    root_agent._skill_index = None  # force a load under the patched settings
    pipeline.reset_catalog_cache()  # module-global cache: don't inherit/leak a catalog across tests
    try:
        with patch("app.agent.get_settings", return_value=settings), \
             patch.object(llm_client, "call_llm_json", new=llm), \
             patch.object(label_discovery, "discover_label_details_for_metrics", return_value=tp._NODE_LABELS), \
             patch.object(prometheus_client, "query_instant",
                           return_value=tp._vector_outcome(("node-00", 38.72))) as inst, \
             patch.object(prometheus_client, "query_range",
                           return_value=tp._matrix_outcome("node-00", 3.2 * 1024 ** 3)) as rng:
            with _client() as client:
                r = client.post("/api/v1/query", json={"question": tp._Q})
    finally:
        root_agent._skill_index = None  # don't leak the patched-settings index into other tests
        pipeline.reset_catalog_cache()

    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] is None
    result = body["result"]
    assert result["mode"] == "multi" and len(result["results"]) == 2
    assert pipeline_marker_absent(result)
    assert result["results"][1]["query"] == 'node_memory_MemAvailable_bytes{node_id="node-00"}'
    assert [x["execution"]["execution_status"] for x in result["results"]] == ["success", "success"]
    assert result["synthesis"] and "node-00" in result["synthesis"]
    assert inst.call_count == 1 and rng.call_count == 1  # no double execution


def pipeline_marker_absent(result: dict) -> bool:
    from app.pipeline import ALREADY_EXECUTED_MARKER
    return ALREADY_EXECUTED_MARKER not in result
