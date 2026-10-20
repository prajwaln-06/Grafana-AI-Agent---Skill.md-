"""Structured metric catalog <-> fullstack pipeline integration.

Fullstack-specific coverage (the Level 1/2/3 cascade itself is covered by
tests/test_pipeline_catalog_integration.py, ported from `main`): scoped/ranked
Generator candidates per request and per dependency stage, the Level 1 -> 2
retry inside dependency-aware stages (and the rule that a stage never
re-routes), fail-safe behavior for missing/malformed catalogs, a catalog miss
never rejecting a supported metric, opt-in status validation, unchanged alert
gating, preservation of the dashboard catalog, and the reload/startup
lifecycle."""

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
BLOCK_HEADER = "Catalog-identified candidate metrics"


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


def _role_llm(router_resp, *generator_resps):
    """Fake `call_llm_json`: Router calls always get `router_resp`; Generator
    calls get `generator_resps` in order (the last one repeats). Records
    (role, prompt) so tests can count calls per role and inspect prompts."""
    calls = []
    state = {"i": 0}

    def fake(prompt, system_instruction, api_key, model):
        if system_instruction.startswith(pipeline._ROUTER_INSTRUCTIONS):
            calls.append(("router", prompt))
            return router_resp
        i = min(state["i"], len(generator_resps) - 1)
        state["i"] += 1
        calls.append(("generator", prompt))
        return generator_resps[i]

    return fake, calls


def _count(calls, role):
    return sum(1 for r, _ in calls if r == role)


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


def _run_single_metric_pipeline(skill_index, settings, metric="node_load1", question="load average on node-1",
                                router_ref=CPU_REF):
    """Router -> Generator(metric) -> validator, returning (result, calls)."""
    fake, calls = _role_llm(_router([_matched(router_ref)]), MagicMock(parsed=_ok_contract(metric)))
    with patch.object(llm_client, "call_llm_json", side_effect=fake), \
         patch.object(label_discovery, "discover_labels_for_metrics",
                      return_value={metric: ["instance", "job"]}):
        result = _run(pipeline.run_pipeline(question, skill_index, settings))
    return result, calls


def _level2_expected_names(catalog, ref, known):
    """Level 2 offers exactly the catalog's metrics for the matched reference
    (restricted to the metrics that reference's overview actually opened)."""
    return {e.name for e in catalog.by_reference_path(ref)} & known


def _generator_prompts(calls):
    return [p for r, p in calls if r == "generator"]


# ---- Generator context: ranked, scoped candidates ----------------------------------


def test_generator_context_gets_ranked_candidates_scoped_to_matched_references(skill_index, cat_settings):
    catalog = rt.get_structured_catalog(cat_settings)
    with patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        ctx = pipeline._build_generator_context(
            [_matched(CPU_REF)], skill_index, cat_settings, question="What is the CPU utilization?",
            catalog=catalog,
        )
    names = [m["name"] for m in ctx.metric_metadata]
    assert names[0] == "node_cpu_seconds_total"
    assert set(names) <= ctx.known_prometheus_metrics
    assert not any(n.startswith("DCGM_") for n in names)
    # Candidates never narrow the validator's/label discovery's metric universe.
    assert len(ctx.known_prometheus_metrics) > len(names)
    assert ctx.reference_universe_mismatch is False
    # Status validation is off by default: no lookup is built at all.
    assert pipeline._catalog_status_lookup(cat_settings, catalog) is None


def test_no_catalog_means_no_candidates_in_the_context(skill_index, cat_settings):
    with patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        ctx = pipeline._build_generator_context(
            [_matched(CPU_REF)], skill_index, cat_settings, question="CPU utilization", catalog=None,
        )
    assert ctx.metric_metadata == () and ctx.reference_universe_mismatch is False


def test_each_stage_context_only_sees_candidates_for_its_own_references(skill_index, cat_settings):
    question = "Which node has the highest CPU utilization, and how much GPU memory is used on that node?"
    catalog = rt.get_structured_catalog(cat_settings)
    with patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        root = pipeline._build_generator_context([_matched(CPU_REF)], skill_index, cat_settings,
                                                 question=question, catalog=catalog)
        dep = pipeline._build_generator_context([_matched(DCGM_MEM_REF)], skill_index, cat_settings,
                                                question=question, catalog=catalog)
    root_names = {m["name"] for m in root.metric_metadata}
    dep_names = {m["name"] for m in dep.metric_metadata}
    assert root_names and dep_names
    assert root_names <= root.known_prometheus_metrics and dep_names <= dep.known_prometheus_metrics
    assert all(n.startswith("node_") for n in root_names)
    assert all(n.startswith("DCGM_") for n in dep_names)
    assert not root_names & dep_names


def test_catalog_miss_leaves_context_unchanged_and_prompt_has_no_catalog_block(skill_index, cat_settings):
    catalog = rt.get_structured_catalog(cat_settings)
    with patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        ctx = pipeline._build_generator_context(
            [_matched(CPU_REF)], skill_index, cat_settings, question="zzz qqq xyzzy", catalog=catalog,
        )
    assert ctx.metric_metadata == ()
    fake, calls = _role_llm(None, MagicMock(parsed={}))
    with patch.object(llm_client, "call_llm_json", side_effect=fake):
        _run(pipeline._run_generator("zzz qqq xyzzy", [_matched(CPU_REF)], False, ctx, skill_index,
                                     cat_settings, pipeline.PipelineContext()))
    assert BLOCK_HEADER not in _generator_prompts(calls)[0]


def test_generator_prompt_contains_the_catalog_candidate_block_before_the_question(skill_index, cat_settings):
    _, calls = _run_single_metric_pipeline(skill_index, cat_settings, metric="node_cpu_seconds_total",
                                           question="CPU utilization on node-1")
    prompt = _generator_prompts(calls)[0]
    assert BLOCK_HEADER in prompt
    assert "node_cpu_seconds_total" in _block_names(prompt)
    assert "unsupported_metric" in prompt                     # restriction wording, as on main
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
    result, calls = _run_single_metric_pipeline(skill_index, cat_settings, question="zzz qqq xyzzy")
    assert BLOCK_HEADER not in _generator_prompts(calls)[0]
    assert result["status"] == "ok" and result["measurement_used"]["name"] == "node_load1"
    assert _count(calls, "generator") == 1                    # no Level 2: nothing narrower to fall back from


def test_a_documented_metric_outside_the_candidate_list_is_recovered_by_level2(skill_index, cat_settings):
    catalog = rt.get_structured_catalog(cat_settings)
    with patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        ctx = pipeline._build_generator_context(
            [_matched(CPU_REF)], skill_index, cat_settings, question="CPU utilization", catalog=catalog,
        )
    candidates = {m["name"] for m in ctx.metric_metadata}
    outside = sorted(ctx.known_prometheus_metrics - candidates)
    assert candidates and outside

    result, calls = _run_single_metric_pipeline(skill_index, cat_settings, metric=outside[0],
                                                question="CPU utilization")
    assert result["status"] == "ok" and result["measurement_used"]["name"] == outside[0]
    # Level 1 (scope violation) then Level 2 (the matched reference's metrics); the Router ran
    # once and there is no Level 3. Level 2 is validated without a narrower scope, so the
    # documented-but-unranked metric is accepted.
    assert _count(calls, "generator") == 2 and _count(calls, "router") == 1
    level2_names = set(_block_names(_generator_prompts(calls)[1]))
    assert level2_names == _level2_expected_names(catalog, CPU_REF, ctx.known_prometheus_metrics)


def test_catalog_candidates_do_not_bypass_existing_metric_validation(skill_index, cat_settings):
    """A Generator metric that is in the catalog but NOT in the Metric Directory
    of the references the Router opened is a fabrication for THIS request: it is
    rejected by the known-metrics check, which is not a Level-2-retryable
    scope violation."""
    result, calls = _run_single_metric_pipeline(skill_index, cat_settings, metric="DCGM_FI_DEV_GPU_TEMP",
                                                question="CPU utilization")
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert _count(calls, "generator") == 1


def test_confident_catalog_hit_in_another_domain_goes_to_level3_and_stays_validated(skill_index, cat_settings):
    """Router picked cpu.md but the catalog confidently says GPU: a reference-universe
    mismatch skips Level 1/2 and re-routes once, catalog-unnarrowed (Level 3). The
    final Generator output is still validated against the references THAT pass opened."""
    result, calls = _run_single_metric_pipeline(skill_index, cat_settings, metric="DCGM_FI_DEV_GPU_TEMP",
                                                question="GPU temperature")
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert _count(calls, "router") == 2 and _count(calls, "generator") == 1


# ---- off-topic / weak matches fall back to the pre-catalog behavior --------------------


def test_off_topic_question_gets_full_section4_and_no_catalog_block_and_no_retry(skill_index, cat_settings):
    """'What is the weather in Paris' used to score 2.1 and narrow the Router's
    table. It must now be a clean miss: the Router sees the complete Section 4,
    the Generator gets no candidate block, and there is no Level 2/3 machinery."""
    question = "What is the weather in Paris"
    fake, calls = _role_llm(_router([_matched(CPU_REF)]), MagicMock(parsed=_ok_contract("node_load1")))
    with patch.object(llm_client, "call_llm_json", side_effect=fake), \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={"node_load1": ["instance"]}):
        result = _run(pipeline.run_pipeline(question, skill_index, cat_settings))
    router_prompt = calls[0][1]
    assert skill_index.section("## 4.") in router_prompt            # byte-for-byte unnarrowed
    assert BLOCK_HEADER not in _generator_prompts(calls)[0]
    assert _count(calls, "router") == 1 and _count(calls, "generator") == 1
    assert result["status"] == "ok"                                  # a catalog miss never means "unsupported"


def test_router_prompt_is_narrowed_for_a_clear_gpu_question_but_keeps_uncovered_rows(skill_index, cat_settings):
    fake, calls = _role_llm(_router([_matched("references/dcgm-exporter/thermal.md")]),
                            MagicMock(parsed=_ok_contract("DCGM_FI_DEV_GPU_TEMP",
                                                          ref="references/dcgm-exporter/thermal.md")))
    with patch.object(llm_client, "call_llm_json", side_effect=fake), \
         patch.object(label_discovery, "discover_labels_for_metrics",
                      return_value={"DCGM_FI_DEV_GPU_TEMP": ["instance"]}):
        _run(pipeline.run_pipeline("What is the GPU temperature?", skill_index, cat_settings))
    router_prompt = calls[0][1]
    assert "references/dcgm-exporter/thermal.md" in router_prompt
    assert "references/node-exporter/cpu.md" not in router_prompt     # catalog-covered, not suggested
    assert "references/prometheus-fundamentals.md" in router_prompt   # uncovered rows are always kept
    assert "references/node-exporter/overview.md" in router_prompt


# ---- opt-in catalog status validation ------------------------------------------------


def test_status_validation_off_by_default_ignores_pending_status_and_passes_no_extra_kwarg(
        skill_index, cat_settings, tmp_path):
    settings = cat_settings.model_copy(update={
        "catalog_path": _catalog_with_status(tmp_path, "node_load1", "discovered_pending_review"),
    })
    with patch.object(validator, "validate_contract", wraps=validator.validate_contract) as spy:
        result, _ = _run_single_metric_pipeline(skill_index, settings)
    assert result["status"] == "ok"
    assert all("catalog_status_by_metric" not in c.kwargs for c in spy.call_args_list)


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


_DEP_QUESTION = "Which node has the highest CPU utilization, and how much GPU memory is used on that node?"


def _dep_router():
    return _router([
        _matched(CPU_REF, intent_id="top_cpu", depends_on=[]),
        _matched(DCGM_MEM_REF, intent_id="gpu_mem_on_node", depends_on=["top_cpu"]),
    ])


def _root_contract(metric="node_cpu_seconds_total", query=None):
    return {
        "mode": "single", "resolution_id": "top_cpu", "status": "ok", "reference_used": CPU_REF,
        "measurement_used": {"type": "raw_metric", "name": metric, "source_metrics": []},
        "data_source": "prometheus",
        "query": query or f'topk(1, 100 - (avg(rate({metric}{{mode="idle"}}[5m])) * 100))',
        "query_type": "instant", "time_range": {"time": "now"}, "explanation": "top CPU node",
    }


def _dependent_contract(metric="DCGM_FI_DEV_FB_USED", **extra):
    contract = {
        "mode": "single", "resolution_id": "gpu_mem_on_node", "status": "ok", "reference_used": DCGM_MEM_REF,
        "measurement_used": {"type": "raw_metric", "name": metric, "source_metrics": []},
        "data_source": "prometheus", "query": f'{metric}{{node_id="node-02"}}',
        "query_type": "instant", "time_range": {"time": "now"}, "explanation": "GPU memory on selected node",
    }
    contract.update(extra)
    return contract


def _run_staged(skill_index, settings, generator_responses):
    fake, calls = _role_llm(_dep_router(), *[MagicMock(parsed=r) for r in generator_responses])
    with patch.object(llm_client, "call_llm_json", side_effect=fake), \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={
             "node_cpu_seconds_total": ["node_id", "mode"], "DCGM_FI_DEV_FB_USED": ["node_id"],
             "DCGM_FI_DEV_FB_FREE": ["node_id"], "DCGM_FI_DEV_GPU_TEMP": ["node_id"],
         }), \
         patch.object(prometheus_client, "query_instant", side_effect=[
             _instant_outcome({"node_id": "node-02"}, 38.7),
             _instant_outcome({"__name__": "DCGM_FI_DEV_FB_USED", "node_id": "node-02"}, 1024),
         ]):
        outcome = _run(pipeline.run_dependency_aware_pipeline(_DEP_QUESTION, skill_index, settings))
    return outcome, calls


def _dep_settings(cat_settings):
    return cat_settings.model_copy(update={"dependent_query_resolution_enabled": True})


def test_dependency_stages_each_get_their_own_catalog_scope_and_no_extra_llm_calls(skill_index, cat_settings):
    outcome, calls = _run_staged(skill_index, _dep_settings(cat_settings),
                                 [_root_contract(), _dependent_contract()])
    assert outcome.already_executed is True
    assert [r["status"] for r in outcome.contract["results"]] == ["ok", "ok"]
    # Router once + one Generator per stage: candidates in scope => no retry.
    assert _count(calls, "router") == 1 and _count(calls, "generator") == 2
    router_prompt = calls[0][1]
    root_prompt, dep_prompt = _generator_prompts(calls)
    root_names, dep_names = _block_names(root_prompt), _block_names(dep_prompt)
    assert "node_cpu_seconds_total" in root_names and "DCGM_FI_DEV_FB_USED" in dep_names
    assert all(n.startswith("node_") for n in root_names)
    assert all(n.startswith("DCGM_") for n in dep_names)
    assert BLOCK_HEADER not in router_prompt                # candidates go to the Generator, not the Router


def test_dependent_stage_retries_at_level2_within_its_own_reference_and_never_reroutes(skill_index, cat_settings):
    catalog = rt.get_structured_catalog(cat_settings)
    with patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        dep_ctx = pipeline._build_generator_context([_matched(DCGM_MEM_REF)], skill_index, cat_settings,
                                                    question=_DEP_QUESTION, catalog=catalog)
    outside = sorted(dep_ctx.known_prometheus_metrics - {m["name"] for m in dep_ctx.metric_metadata})
    assert outside, "need a known metric outside the Level-1 candidates for this scenario"

    # 1: root. 2: dependent Level 1 (picks a metric outside the candidates -> scope violation).
    # 3: dependent Level 2 (same pick is now accepted).
    outcome, calls = _run_staged(
        skill_index, _dep_settings(cat_settings),
        [_root_contract(), _dependent_contract(outside[0]), _dependent_contract(outside[0])],
    )
    assert [r["status"] for r in outcome.contract["results"]] == ["ok", "ok"]
    assert outcome.contract["results"][1]["measurement_used"]["name"] == outside[0]
    assert _count(calls, "router") == 1                     # a stage never re-routes (no Level 3)
    assert _count(calls, "generator") == 3
    dep_l1, dep_l2 = _generator_prompts(calls)[1], _generator_prompts(calls)[2]
    # Level 1 is scoped to the stage's own reference file, so it is a subset of Level 2 (all of that
    # file's metrics) -- equal here, because this question already matches every metric of the file.
    assert set(_block_names(dep_l1)) <= set(_block_names(dep_l2))
    assert set(_block_names(dep_l2)) == _level2_expected_names(catalog, DCGM_MEM_REF,
                                                              dep_ctx.known_prometheus_metrics)
    assert all(n.startswith("DCGM_") for n in _block_names(dep_l2))   # still scoped to ITS reference


def test_root_stage_level2_retry_is_scoped_to_the_root_reference(skill_index, cat_settings):
    # A narrower question than _DEP_QUESTION so some node metric is NOT a Level-1 candidate.
    question = "Which host has the highest load average, and how much GPU memory is used on that host?"
    catalog = rt.get_structured_catalog(cat_settings)
    with patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        root_ctx = pipeline._build_generator_context([_matched(CPU_REF)], skill_index, cat_settings,
                                                     question=question, catalog=catalog)
    outside = sorted(root_ctx.known_prometheus_metrics - {m["name"] for m in root_ctx.metric_metadata})
    assert outside, "scenario needs a node metric outside the Level-1 candidates"
    fake, calls = _role_llm(
        _dep_router(),
        MagicMock(parsed=_root_contract(outside[0], f"topk(1, {outside[0]})")),
        MagicMock(parsed=_root_contract(outside[0], f"topk(1, {outside[0]})")),
        MagicMock(parsed=_dependent_contract()),
    )
    with patch.object(llm_client, "call_llm_json", side_effect=fake), \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={
             outside[0]: ["node_id"], "DCGM_FI_DEV_FB_USED": ["node_id"],
         }), \
         patch.object(prometheus_client, "query_instant", side_effect=[
             _instant_outcome({"node_id": "node-02"}, 1.0),
             _instant_outcome({"__name__": "DCGM_FI_DEV_FB_USED", "node_id": "node-02"}, 1024),
         ]):
        outcome = _run(pipeline.run_dependency_aware_pipeline(question, skill_index, _dep_settings(cat_settings)))
    assert outcome.already_executed is True
    assert _count(calls, "router") == 1 and _count(calls, "generator") == 3
    root_l2 = set(_block_names(_generator_prompts(calls)[1]))
    assert root_l2 == _level2_expected_names(catalog, CPU_REF, root_ctx.known_prometheus_metrics)
    assert all(n.startswith("node_") for n in root_l2)


def test_stage_that_stays_unsupported_after_level2_is_final_and_does_not_reroute(skill_index, cat_settings):
    unsupported = {
        "mode": "single", "resolution_id": "top_cpu", "status": "unsupported_metric",
        "reference_used": CPU_REF, "requested_measurement": "something exotic",
        "explanation": "no matching metric",
    }
    fake, calls = _role_llm(_dep_router(), MagicMock(parsed=unsupported))
    with patch.object(llm_client, "call_llm_json", side_effect=fake), \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        outcome = _run(pipeline.run_dependency_aware_pipeline(_DEP_QUESTION, skill_index, _dep_settings(cat_settings)))
    assert _count(calls, "router") == 1                     # Level 3 would re-route the WHOLE question: never in a stage
    assert _count(calls, "generator") == 2                  # Level 1, then Level 2, then final
    assert outcome.already_executed is False or outcome.contract["mode"] in ("single", "multi")


def test_cascade_is_diagnosable_from_logs(skill_index, cat_settings, caplog):
    import logging

    caplog.set_level(logging.INFO)
    _run_single_metric_pipeline(skill_index, cat_settings, metric="node_cpu_seconds_total",
                                question="CPU utilization on node-1")
    text = caplog.text
    assert "catalog_routing:" in text                       # Router narrowing decision
    assert "catalog_pipeline[flat]: Level 1 candidates" in text
    assert "answered at Level 1" in text
    assert "test-key-not-real" not in text                  # never log credentials


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
