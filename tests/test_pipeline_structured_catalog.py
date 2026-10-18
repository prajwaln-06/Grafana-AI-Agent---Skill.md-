"""Structured metric catalog <-> fullstack pipeline integration.

Covers: scoped/ranked Generator candidates, the advisory prompt block,
fail-safe behavior for missing/malformed catalogs, catalog misses never
rejecting supported metrics, opt-in status validation, per-stage candidate
scope in dependency-aware execution, unchanged alert gating, preservation of
the dashboard catalog, and the reload/startup lifecycle."""

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import label_discovery, llm_client, pipeline, prometheus_client, validator
from app.catalog import runtime as rt
from app.config import Settings

REPO = Path(__file__).resolve().parent.parent
REAL_CATALOG = REPO / "app" / "catalog" / "catalog.json"
DASHBOARD_CATALOG = REPO / "app" / "catalog.json"
SKILLS = REPO / "skills"

CPU_REF = "references/node-exporter/cpu.md"
DCGM_MEM_REF = "references/dcgm-exporter/memory.md"
BLOCK_HEADER = "Catalog-ranked candidate metrics"


@pytest.fixture(autouse=True)
def _fresh_catalog_cache():
    rt.reset_structured_catalog_cache()
    yield
    rt.reset_structured_catalog_cache()


@pytest.fixture()
def cat_settings(settings):
    return settings.model_copy(update={
        "catalog_path": REAL_CATALOG,
        "catalog_metric_status_validation_enabled": False,
        "dependent_query_resolution_enabled": False,
    })


def _run(coro):
    return asyncio.run(coro)


def _matched(ref, **extra):
    return {"reference_path": ref, "data_source": "prometheus", **extra}


def _router(matched):
    return MagicMock(parsed={
        "gate_stop": None, "matched_references": matched,
        "panic_mode": False, "unresolved_topics": [],
    })


def _ok_contract(metric, ref=CPU_REF, **extra):
    contract = {
        "mode": "single", "status": "ok", "reference_used": ref,
        "measurement_used": {"type": "raw_metric", "name": metric, "source_metrics": []},
        "data_source": "prometheus", "query": f'{metric}{{instance="node-1:9100"}}',
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "test",
    }
    contract.update(extra)
    return contract


def _capturing_llm(responses):
    prompts = []
    remaining = iter(responses)

    def fake(prompt, system_instruction, api_key, model):
        prompts.append(prompt)
        return next(remaining)

    return fake, prompts


def _block_names(prompt):
    """Metric names listed in the catalog block of a Generator prompt."""
    if BLOCK_HEADER not in prompt:
        return []
    block = prompt.split(BLOCK_HEADER, 1)[1].split("User question:", 1)[0]
    return re.findall(r"^- (\S+) \(type: ", block, flags=re.MULTILINE)


def _catalog_with_status(tmp_path, metric, status):
    data = json.loads(REAL_CATALOG.read_text(encoding="utf-8"))
    for m in data["metrics"]:
        if m["name"] == metric:
            m["status"] = status
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _run_single_metric_pipeline(skill_index, settings, metric="node_load1", question="load average on node-1"):
    """Router -> Generator(metric) -> validator, returning (result, generator_prompt)."""
    fake, prompts = _capturing_llm([_router([_matched(CPU_REF)]), MagicMock(parsed=_ok_contract(metric))])
    with patch.object(llm_client, "call_llm_json", side_effect=fake), \
         patch.object(label_discovery, "discover_labels_for_metrics",
                      return_value={metric: ["instance", "job"]}):
        result = _run(pipeline.run_pipeline(question, skill_index, settings))
    return result, prompts[1]


# ---- Generator context: ranked, scoped candidates ----------------------------------


def test_generator_context_gets_ranked_candidates_scoped_to_matched_references(skill_index, cat_settings):
    with patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        ctx = pipeline._build_generator_context(
            [_matched(CPU_REF)], skill_index, cat_settings, question="What is the CPU utilization?"
        )
    names = [m["name"] for m in ctx.metric_metadata]
    assert names[0] == "node_cpu_seconds_total"
    assert set(names) <= ctx.known_prometheus_metrics
    assert not any(n.startswith("DCGM_") for n in names)
    # Candidates never narrow the validator's/label discovery's metric universe.
    assert len(ctx.known_prometheus_metrics) > len(names)
    assert ctx.catalog_status_by_metric == {}      # status validation is off by default


def test_each_stage_context_only_sees_candidates_for_its_own_references(skill_index, cat_settings):
    question = "Which node has the highest CPU utilization, and how much GPU memory is used on that node?"
    with patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        root = pipeline._build_generator_context([_matched(CPU_REF)], skill_index, cat_settings, question=question)
        dep = pipeline._build_generator_context([_matched(DCGM_MEM_REF)], skill_index, cat_settings, question=question)
    root_names = {m["name"] for m in root.metric_metadata}
    dep_names = {m["name"] for m in dep.metric_metadata}
    assert root_names and dep_names
    assert root_names <= root.known_prometheus_metrics and dep_names <= dep.known_prometheus_metrics
    assert all(n.startswith("node_") for n in root_names)
    assert all(n.startswith("DCGM_") for n in dep_names)
    assert not root_names & dep_names


def test_catalog_miss_leaves_context_unchanged_and_prompt_has_no_catalog_block(skill_index, cat_settings):
    with patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        ctx = pipeline._build_generator_context(
            [_matched(CPU_REF)], skill_index, cat_settings, question="zzz qqq xyzzy"
        )
    assert ctx.metric_metadata == ()
    fake, prompts = _capturing_llm([MagicMock(parsed={})])
    with patch.object(llm_client, "call_llm_json", side_effect=fake):
        _run(pipeline._run_generator("zzz qqq xyzzy", [_matched(CPU_REF)], False, ctx, skill_index,
                                     cat_settings, pipeline.PipelineContext()))
    assert BLOCK_HEADER not in prompts[0]


def test_generator_prompt_contains_advisory_catalog_block_before_the_question(skill_index, cat_settings):
    _, prompt = _run_single_metric_pipeline(skill_index, cat_settings, question="CPU utilization on node-1")
    assert BLOCK_HEADER in prompt
    assert "node_cpu_seconds_total" in _block_names(prompt)
    assert "not a restriction" in prompt
    assert prompt.index(BLOCK_HEADER) < prompt.index("User question:")
    # The authoritative material is still there, unchanged.
    assert "Matched reference file(s):" in prompt and "Live label keys confirmed" in prompt


# ---- fail-safe: missing / malformed catalog -----------------------------------------


@pytest.mark.parametrize("make_path", [
    lambda tmp: tmp / "does-not-exist.json",
    lambda tmp: (tmp / "bad.json").write_text("{not json", encoding="utf-8") or tmp / "bad.json",
])
def test_missing_or_malformed_catalog_does_not_change_pipeline_behavior(skill_index, cat_settings, tmp_path, make_path):
    settings = cat_settings.model_copy(update={"catalog_path": make_path(tmp_path)})
    result, prompt = _run_single_metric_pipeline(skill_index, settings, question="CPU utilization on node-1")
    assert result["status"] == "ok"
    assert BLOCK_HEADER not in prompt


# ---- a catalog miss / ranking miss never makes a supported metric unsupported -------


def test_catalog_miss_does_not_reject_a_supported_metric(skill_index, cat_settings):
    result, prompt = _run_single_metric_pipeline(skill_index, cat_settings, question="zzz qqq xyzzy")
    assert BLOCK_HEADER not in prompt
    assert result["status"] == "ok" and result["measurement_used"]["name"] == "node_load1"


def test_a_documented_metric_outside_the_candidate_list_is_still_accepted(skill_index, cat_settings):
    with patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        ctx = pipeline._build_generator_context(
            [_matched(CPU_REF)], skill_index, cat_settings, question="CPU utilization"
        )
    candidates = {m["name"] for m in ctx.metric_metadata}
    outside = sorted(ctx.known_prometheus_metrics - candidates)
    assert candidates and outside
    result, _ = _run_single_metric_pipeline(skill_index, cat_settings, metric=outside[0], question="CPU utilization")
    assert result["status"] == "ok" and result["measurement_used"]["name"] == outside[0]


def test_catalog_candidates_do_not_bypass_existing_metric_validation(skill_index, cat_settings):
    """A Generator metric that is in the catalog but NOT in the Metric Directory
    of the references the Router opened must still be rejected."""
    result, _ = _run_single_metric_pipeline(skill_index, cat_settings, metric="DCGM_FI_DEV_GPU_TEMP",
                                            question="GPU temperature")
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS


# ---- opt-in catalog status validation ------------------------------------------------


def test_status_validation_off_by_default_ignores_pending_status_and_passes_no_extra_kwarg(
        skill_index, cat_settings, tmp_path):
    settings = cat_settings.model_copy(update={
        "catalog_path": _catalog_with_status(tmp_path, "node_load1", "discovered_pending_review"),
    })
    with patch.object(validator, "validate_contract", wraps=validator.validate_contract) as spy:
        result, _ = _run_single_metric_pipeline(skill_index, settings)
    assert result["status"] == "ok"
    assert "catalog_status_by_metric" not in spy.call_args.kwargs


def test_status_validation_on_rejects_a_pending_review_metric(skill_index, cat_settings, tmp_path):
    settings = cat_settings.model_copy(update={
        "catalog_path": _catalog_with_status(tmp_path, "node_load1", "discovered_pending_review"),
        "catalog_metric_status_validation_enabled": True,
    })
    result, _ = _run_single_metric_pipeline(skill_index, settings)
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert "discovered_pending_review" in result["explanation"]


def test_status_validation_on_still_accepts_approved_metrics(skill_index, cat_settings):
    settings = cat_settings.model_copy(update={"catalog_metric_status_validation_enabled": True})
    with patch.object(validator, "validate_contract", wraps=validator.validate_contract) as spy:
        result, _ = _run_single_metric_pipeline(skill_index, settings)
    assert result["status"] == "ok"
    assert len(spy.call_args.kwargs["catalog_status_by_metric"]) == 43


def test_status_validation_on_fails_open_when_the_catalog_is_unavailable(skill_index, cat_settings, tmp_path):
    settings = cat_settings.model_copy(update={
        "catalog_path": tmp_path / "missing.json",
        "catalog_metric_status_validation_enabled": True,
    })
    with patch.object(validator, "validate_contract", wraps=validator.validate_contract) as spy:
        result, _ = _run_single_metric_pipeline(skill_index, settings)
    assert result["status"] == "ok"
    assert "catalog_status_by_metric" not in spy.call_args.kwargs


# ---- dependency-aware execution: candidates are built independently per stage -------


def _instant_outcome(metric, value):
    return prometheus_client.ExecutionOutcome(
        status="success",
        raw_data={"resultType": "vector", "result": [{"metric": metric, "value": [1735689600, str(value)]}]},
    )


def test_dependency_stages_each_get_their_own_catalog_scope_and_no_extra_llm_calls(skill_index, cat_settings):
    settings = cat_settings.model_copy(update={"dependent_query_resolution_enabled": True})
    router = _router([
        _matched(CPU_REF, intent_id="top_cpu", depends_on=[]),
        _matched(DCGM_MEM_REF, intent_id="gpu_mem_on_node", depends_on=["top_cpu"]),
    ])
    root = {
        "mode": "single", "resolution_id": "top_cpu", "status": "ok", "reference_used": CPU_REF,
        "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
        "data_source": "prometheus",
        "query": 'topk(1, 100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))',
        "query_type": "instant", "time_range": {"time": "now"}, "explanation": "top CPU node",
    }
    dependent = {
        "mode": "single", "resolution_id": "gpu_mem_on_node", "status": "ok", "reference_used": DCGM_MEM_REF,
        "measurement_used": {"type": "raw_metric", "name": "DCGM_FI_DEV_FB_USED", "source_metrics": []},
        "data_source": "prometheus", "query": 'DCGM_FI_DEV_FB_USED{node_id="node-02"}',
        "query_type": "instant", "time_range": {"time": "now"}, "explanation": "GPU memory on selected node",
    }
    fake, prompts = _capturing_llm([router, MagicMock(parsed=root), MagicMock(parsed=dependent)])
    with patch.object(llm_client, "call_llm_json", side_effect=fake) as mock_llm, \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={
             "node_cpu_seconds_total": ["node_id", "mode"], "DCGM_FI_DEV_FB_USED": ["node_id"],
         }), \
         patch.object(prometheus_client, "query_instant", side_effect=[
             _instant_outcome({"node_id": "node-02"}, 38.7),
             _instant_outcome({"__name__": "DCGM_FI_DEV_FB_USED", "node_id": "node-02"}, 1024),
         ]):
        outcome = _run(pipeline.run_dependency_aware_pipeline(
            "Which node has the highest CPU utilization, and how much GPU memory is used on that node?",
            skill_index, settings,
        ))

    assert outcome.already_executed is True
    assert [r["status"] for r in outcome.contract["results"]] == ["ok", "ok"]
    assert mock_llm.call_count == 3                      # router once + one generator per stage
    router_prompt, root_prompt, dep_prompt = prompts
    root_names, dep_names = _block_names(root_prompt), _block_names(dep_prompt)
    assert "node_cpu_seconds_total" in root_names and "DCGM_FI_DEV_FB_USED" in dep_names
    assert all(n.startswith("node_") for n in root_names)
    assert all(n.startswith("DCGM_") for n in dep_names)
    assert BLOCK_HEADER not in router_prompt             # the Router is untouched by the catalog


# ---- clarification follow-ups --------------------------------------------------------


def test_catalog_search_text_for_a_follow_up_uses_the_full_combined_intent():
    plain = pipeline.PipelineContext()
    assert pipeline._catalog_query_text("CPU please", plain) == "CPU please"
    follow_up = pipeline.PipelineContext(
        previous_question="how busy is the machine", previous_result={}, clarification_answer="the CPU",
    )
    assert pipeline._catalog_query_text("the CPU", follow_up) == "how busy is the machine the CPU"


# ---- alert-rule gating is unchanged --------------------------------------------------


def test_alert_creation_gate_is_unchanged_with_catalog_status_validation_enabled(skill_index, cat_settings):
    settings = cat_settings.model_copy(update={
        "alert_rule_creation_enabled": False, "catalog_metric_status_validation_enabled": True,
    })
    router_resp = _router([_matched("references/dcgm-exporter/thermal.md")])
    with patch.object(llm_client, "call_llm_json", return_value=router_resp) as mock_call:
        result = _run(pipeline.run_pipeline(
            "Create an alert when power draw exceeds 500 watts for 5 minutes.", skill_index, settings))
    assert result["status"] == "out_of_scope_action"
    assert mock_call.call_count == 1


# ---- the dashboard catalog is a separate thing and is untouched -----------------------


def test_dashboard_catalog_is_independent_of_the_structured_catalog():
    from app.api import routes_query

    dashboard = json.loads(DASHBOARD_CATALOG.read_text(encoding="utf-8"))
    assert routes_query._load_catalog() == dashboard
    assert "catalog_version" not in dashboard                      # not the structured schema
    structured = json.loads(REAL_CATALOG.read_text(encoding="utf-8"))
    assert "catalog_version" in structured
    assert DASHBOARD_CATALOG != REAL_CATALOG


def test_catalog_api_endpoint_still_serves_the_dashboard_catalog():
    from app.api import main as api_main

    with TestClient(api_main.app) as client:
        response = client.get("/api/catalog")
    assert response.status_code == 200
    assert response.json() == json.loads(DASHBOARD_CATALOG.read_text(encoding="utf-8"))


# ---- lifecycle: startup and reload ----------------------------------------------------


def _small_catalog_settings(tmp_path, count):
    path = tmp_path / "catalog.json"
    data = json.loads(REAL_CATALOG.read_text(encoding="utf-8"))
    data["metrics"] = data["metrics"][:count]
    path.write_text(json.dumps(data), encoding="utf-8")
    return Settings(gemini_api_key="test-key-not-real", skills_root=SKILLS, catalog_path=path), path


def test_app_starts_and_serves_when_the_structured_catalog_is_missing(monkeypatch, tmp_path):
    from app.api import main as api_main

    broken = Settings(gemini_api_key="test-key-not-real", skills_root=SKILLS,
                      catalog_path=tmp_path / "missing.json")
    monkeypatch.setattr(api_main, "get_settings", lambda: broken)
    with TestClient(api_main.app) as client:
        assert client.get("/readyz").json()["status"] == "ready"
        assert client.get("/api/catalog").status_code == 200       # dashboard unaffected
    assert rt.get_structured_catalog(broken) is None


def test_admin_reload_refreshes_the_catalog_and_survives_a_bad_catalog_edit(monkeypatch, tmp_path):
    from app.api import main as api_main

    settings, path = _small_catalog_settings(tmp_path, 5)
    monkeypatch.setattr("app.api.routes_admin.get_settings", lambda: settings)
    assert len(rt.get_structured_catalog(settings).metrics) == 5

    data = json.loads(REAL_CATALOG.read_text(encoding="utf-8"))
    data["metrics"] = data["metrics"][:9]
    path.write_text(json.dumps(data), encoding="utf-8")

    with TestClient(api_main.app) as client:
        assert len(rt.get_structured_catalog(settings).metrics) == 5   # not live until reload
        assert client.post("/api/v1/admin/reload-skill").status_code == 200
        assert len(rt.get_structured_catalog(settings).metrics) == 9

        path.write_text("{broken", encoding="utf-8")
        assert client.post("/api/v1/admin/reload-skill").status_code == 200
        assert len(rt.get_structured_catalog(settings).metrics) == 9   # previous good catalog kept


def test_agent_reload_skill_also_refreshes_the_catalog(monkeypatch, tmp_path):
    from app.agent import root_agent

    settings, path = _small_catalog_settings(tmp_path, 4)
    monkeypatch.setattr("app.agent.get_settings", lambda: settings)
    assert len(rt.get_structured_catalog(settings).metrics) == 4
    data = json.loads(REAL_CATALOG.read_text(encoding="utf-8"))
    data["metrics"] = data["metrics"][:7]
    path.write_text(json.dumps(data), encoding="utf-8")

    root_agent.reload_skill()
    assert len(rt.get_structured_catalog(settings).metrics) == 7
