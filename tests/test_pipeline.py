import asyncio
from unittest.mock import MagicMock, patch

from app import field_discovery, label_discovery, llm_client, pipeline, prometheus_client


def _run(coro):
    return asyncio.run(coro)


def test_router_gate_stop_short_circuits_before_generator(skill_index, settings):
    """A gate_stop from the Router must return immediately -- the Generator
    (and therefore any LLM-based query construction) must never run."""
    router_resp = MagicMock(parsed={
        "gate_stop": {"status": "out_of_scope_action", "requested_action": "delete a metric",
                      "explanation": "read-only skill"},
        "matched_references": [], "panic_mode": False, "unresolved_topics": [],
    })
    with patch.object(llm_client, "call_llm_json", return_value=router_resp) as mock_call:
        result = _run(pipeline.run_pipeline("delete node_load1", skill_index, settings))
    assert result["mode"] == "single"
    assert result["status"] == "out_of_scope_action"
    assert mock_call.call_count == 1  # generator never ran -- only the Router hit the LLM


def test_only_two_llm_calls_for_a_normal_ok_result(skill_index, settings):
    """Regression test for the deterministic-validator rewrite: the old
    pipeline made 3 LLM calls (router, generator, validator). The validator
    is now plain Python -- exactly 2 LLM calls should happen for any
    ordinary resolvable question."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [],
    })
    gen_contract = {
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_load1", "source_metrics": []},
        "data_source": "prometheus", "query": 'node_load1{instance="node-1:9100"}',
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "test",
    }
    gen_resp = MagicMock(parsed=gen_contract)

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, gen_resp]) as mock_call, \
         patch.object(label_discovery, "discover_labels_for_metrics",
                       return_value={"node_load1": ["instance", "job"]}):
        result = _run(pipeline.run_pipeline("load average on node-1", skill_index, settings))

    assert mock_call.call_count == 2
    assert result["status"] == "ok"
    assert result["query"] == 'node_load1{instance="node-1:9100"}'


def test_generator_prompt_includes_live_labels_block_and_section_7(skill_index, settings):
    """The Generator prompt must include the live-discovered labels block
    (Principle 9 depends on the model actually seeing it) and Section 7
    (needed for correct panic_mode_best_effort framing per Section 7.4)."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": True, "unresolved_topics": [],
    })
    gen_contract = {
        "mode": "single", "status": "panic_mode_best_effort", "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_load1", "source_metrics": []},
        "data_source": "prometheus", "query": "node_load1",
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"},
        "explanation": "test", "caveat": "interpreted broadly",
    }
    captured = {}

    def fake_call(prompt, system_instruction, api_key, model):
        if "router" not in captured:
            captured["router"] = (prompt, system_instruction)
            return router_resp
        captured["generator"] = (prompt, system_instruction)
        return MagicMock(parsed=gen_contract)

    with patch.object(llm_client, "call_llm_json", side_effect=fake_call), \
         patch.object(label_discovery, "discover_labels_for_metrics",
                       return_value={"node_load1": ["node_id", "instance", "job", "cluster"]}):
        result = _run(pipeline.run_pipeline("system load", skill_index, settings))

    assert result["status"] == "panic_mode_best_effort"
    generator_prompt, generator_system = captured["generator"]
    assert "node_id" in generator_prompt
    assert "Live label keys confirmed" in generator_prompt
    assert "## 7." in generator_prompt or "Error Handling" in generator_prompt
    assert "panic_mode is currently: True" in generator_prompt


def test_validation_failure_produces_internal_status_not_a_crash(skill_index, settings):
    """A structurally-broken Generator output (here: a label key that was
    never confirmed by live discovery) must be caught deterministically and
    turned into INTERNAL_VALIDATION_FAILED_STATUS, never raise or silently
    pass through to the Executor."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [],
    })
    gen_contract = {
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_load1", "source_metrics": []},
        "data_source": "prometheus", "query": 'node_load1{fabricated_label="x"}',
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "test",
    }

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, MagicMock(parsed=gen_contract)]), \
         patch.object(label_discovery, "discover_labels_for_metrics",
                       return_value={"node_load1": ["instance", "job"]}):
        result = _run(pipeline.run_pipeline("load average", skill_index, settings))

    assert result["mode"] == "single"
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert "fabricated_label" in result["explanation"]


def test_unresolved_topic_appended_alongside_a_resolved_prometheus_result(skill_index, settings):
    """Core requirement: a compound question needing both Prometheus (which
    exists) and OpenSearch (which doesn't have a domain reference yet) must
    resolve the Prometheus half normally AND surface the OpenSearch half as
    an explicit unmapped entry -- neither half should silently swallow the
    other."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": False,
        "unresolved_topics": [{"description": "recent error logs for node-1",
                                "reason": "needs an OpenSearch-backed log measurement; no domain reference yet"}],
    })
    gen_contract = {
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_load1", "source_metrics": []},
        "data_source": "prometheus", "query": "node_load1",
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "test",
    }

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, MagicMock(parsed=gen_contract)]), \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={"node_load1": []}):
        result = _run(pipeline.run_pipeline(
            "show CPU load and recent error logs for node-1", skill_index, settings))

    assert result["mode"] == "multi"
    assert result["synthesis"] is None
    assert len(result["results"]) == 2
    statuses = {r["status"] for r in result["results"]}
    assert statuses == {"ok", "unmapped"}
    unmapped_entry = next(r for r in result["results"] if r["status"] == "unmapped")
    assert "error logs" in unmapped_entry["explanation"]
    ok_entry = next(r for r in result["results"] if r["status"] == "ok")
    assert ok_entry["query"] == "node_load1"


def test_question_entirely_unresolved_with_no_matched_references_becomes_single_unmapped(skill_index, settings):
    """When EVERY sub-intent is unresolved (matched_references empty),
    the result collapses to a single unmapped response, not a
    one-item multi."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [],
        "panic_mode": False,
        "unresolved_topics": [{"description": "recent error logs for node-1",
                                "reason": "no OpenSearch domain reference yet"}],
    })
    with patch.object(llm_client, "call_llm_json", return_value=router_resp) as mock_call:
        result = _run(pipeline.run_pipeline("show me recent error logs for node-1", skill_index, settings))

    assert mock_call.call_count == 1  # generator never ran -- nothing matched
    assert result["mode"] == "single"
    assert result["status"] == "unmapped"
    assert "error logs" in result["explanation"]


def test_generator_omitting_the_mode_wrapper_is_repaired_not_rejected(skill_index, settings):
    """Regression test for a real bug hit in live testing: the Generator
    produced a structurally-correct, well-formed 'unsupported_metric' entry
    (all of ITS required fields present) but forgot the top-level 'mode'
    envelope Section 9 requires around it. The old behavior rejected the
    whole thing as validation_failed over a missing wrapper key, discarding
    a substantively-correct answer. `_normalize_contract_shape` repairs
    exactly this class of near-miss (envelope only, never content) before
    validation runs."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/dcgm-exporter/thermal.md", "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [],
    })
    # Note: no "mode" key at all -- this is the exact shape observed from a
    # real Gemini call in practice.
    gen_contract_missing_mode = {
        "status": "unsupported_metric",
        "reference_used": "references/dcgm-exporter/thermal.md",
        "requested_measurement": "whether the GPU has been power throttling",
        "explanation": "DCGM_FI_DEV_POWER_VIOLATION's exposed unit is unverified against the live "
                       "datasource, so no query can be safely constructed until that is confirmed.",
    }

    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, MagicMock(parsed=gen_contract_missing_mode)]), \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("Has the GPU been power throttling?", skill_index, settings))

    assert result["status"] == "unsupported_metric"
    assert result["mode"] == "single"
    assert "DCGM_FI_DEV_POWER_VIOLATION" in result["explanation"]


def test_generator_multi_mode_missing_wrapper_is_also_repaired(skill_index, settings):
    """Same repair, for the multi-mode shape: 'results' + 'synthesis'
    present but no top-level 'mode' key."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [
            {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"},
            {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus"},
        ],
        "panic_mode": False, "unresolved_topics": [],
    })
    gen_contract_missing_mode = {
        "results": [
            {"status": "ok", "reference_used": "references/node-exporter/cpu.md",
             "measurement_used": {"type": "raw_metric", "name": "node_load1", "source_metrics": []},
             "data_source": "prometheus", "query": "node_load1",
             "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "x"},
            {"status": "ok", "reference_used": "references/node-exporter/memory.md",
             "measurement_used": {"type": "raw_metric", "name": "node_memory_MemAvailable_bytes", "source_metrics": []},
             "data_source": "prometheus", "query": "node_memory_MemAvailable_bytes",
             "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "y"},
        ],
        "synthesis": None,
    }
    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, MagicMock(parsed=gen_contract_missing_mode)]), \
         patch.object(label_discovery, "discover_labels_for_metrics",
                       return_value={"node_load1": [], "node_memory_MemAvailable_bytes": []}):
        result = _run(pipeline.run_pipeline("CPU and available memory", skill_index, settings))

    assert result["mode"] == "multi"
    assert len(result["results"]) == 2


def test_generator_single_entry_inside_multi_envelope_is_repaired(skill_index, settings):
    """A one-entry result must use the single envelope. This harmless model
    wrapper error should not become an internal validation failure in the UI."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [],
    })
    generated = {
        "mode": "multi",
        "results": [{
            "status": "declined", "reason": "parameter_requires_clarification",
            "explanation": "The requested node scope cannot be mapped safely.",
            "clarification": "Which runtime label identifies these nodes?",
        }],
        "synthesis": None,
    }
    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, MagicMock(parsed=generated)]), \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("CPU utilization for node-01 and node-02", skill_index, settings))

    assert result == {"mode": "single", **generated["results"][0]}


def test_out_of_scope_action_missing_requested_action_still_fails_loudly(skill_index, settings):
    """The companion real bug from the same test run: the Router
    misclassified a plain data request ('Show me memory.') as
    out_of_scope_action and omitted the required 'requested_action' field.
    Unlike the missing-'mode' case above, this must NOT be silently
    repaired -- 'requested_action' is CONTENT (SKILL.md Section 9 requires
    it to restate the actual mutating action requested), and fabricating
    one to make the contract pass would hide a genuine misclassification
    instead of surfacing it. This test locks in that validation_failed
    remains the correct outcome here, and that the reason is legible."""
    router_resp = MagicMock(parsed={
        "gate_stop": {"status": "out_of_scope_action", "explanation": "Not a data request."},
        "matched_references": [], "panic_mode": False, "unresolved_topics": [],
    })
    with patch.object(llm_client, "call_llm_json", return_value=router_resp):
        result = _run(pipeline.run_pipeline("Show me memory.", skill_index, settings))

    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert "requested_action" in result["explanation"]


def test_multi_datasource_question_where_both_sides_match_uses_multi_mode(skill_index, settings):
    """Two DIFFERENT matched references (both real, both prometheus here)
    for one compound question -- the Generator alone decides mode, and the
    pipeline must pass its multi-result output through untouched when there
    are no unresolved topics to merge in."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [
            {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"},
            {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus"},
        ],
        "panic_mode": False, "unresolved_topics": [],
    })
    gen_contract = {
        "mode": "multi",
        "results": [
            {"status": "ok", "reference_used": "references/node-exporter/cpu.md",
             "measurement_used": {"type": "raw_metric", "name": "node_load1", "source_metrics": []},
             "data_source": "prometheus", "query": "node_load1",
             "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "x"},
            {"status": "ok", "reference_used": "references/node-exporter/memory.md",
             "measurement_used": {"type": "raw_metric", "name": "node_memory_MemAvailable_bytes", "source_metrics": []},
             "data_source": "prometheus", "query": "node_memory_MemAvailable_bytes",
             "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "y"},
        ],
        "synthesis": "Load average and available memory for the requested node.",
    }

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, MagicMock(parsed=gen_contract)]), \
         patch.object(label_discovery, "discover_labels_for_metrics",
                       return_value={"node_load1": [], "node_memory_MemAvailable_bytes": []}):
        result = _run(pipeline.run_pipeline("load average and available memory", skill_index, settings))

    assert result["mode"] == "multi"
    assert len(result["results"]) == 2
    assert result["synthesis"] == "Load average and available memory for the requested node."


def test_dependency_flag_off_keeps_independent_compound_contract_unchanged(skill_index, settings):
    """The new Router metadata is ignored while the opt-in flag is off."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [
            {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus",
             "intent_id": "cpu", "depends_on": []},
            {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus",
             "intent_id": "memory", "depends_on": []},
        ],
        "panic_mode": False, "unresolved_topics": [],
    })
    expected = {
        "mode": "multi",
        "results": [
            {"status": "ok", "reference_used": "references/node-exporter/cpu.md",
             "measurement_used": {"type": "raw_metric", "name": "node_load1", "source_metrics": []},
             "data_source": "prometheus", "query": "node_load1", "query_type": "instant",
             "time_range": {"time": "now"}, "explanation": "cpu"},
            {"status": "ok", "reference_used": "references/node-exporter/memory.md",
             "measurement_used": {"type": "raw_metric", "name": "node_memory_MemAvailable_bytes", "source_metrics": []},
             "data_source": "prometheus", "query": "node_memory_MemAvailable_bytes", "query_type": "instant",
             "time_range": {"time": "now"}, "explanation": "memory"},
        ],
        "synthesis": None,
    }
    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, MagicMock(parsed=expected)]) as mock_call, \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={
             "node_load1": [], "node_memory_MemAvailable_bytes": [],
         }):
        result = _run(pipeline.run_pipeline("CPU and available memory", skill_index, settings))

    assert mock_call.call_count == 2
    assert result == expected


def _dependency_settings(settings):
    return settings.model_copy(update={"dependent_query_resolution_enabled": True})


def _dependent_router_response():
    return MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [
            {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus",
             "intent_id": "top_cpu", "depends_on": []},
            {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus",
             "intent_id": "memory_for_top_cpu", "depends_on": ["top_cpu"]},
        ],
        "panic_mode": False,
        "unresolved_topics": [],
    })


def _root_cpu_contract():
    return {
        "mode": "single", "resolution_id": "top_cpu", "status": "ok",
        "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
        "data_source": "prometheus",
        "query": 'topk(1, 100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))',
        "query_type": "instant", "time_range": {"time": "now"}, "explanation": "top CPU node",
    }


def _dependent_memory_contract():
    return {
        "mode": "single", "resolution_id": "memory_for_top_cpu", "status": "ok",
        "reference_used": "references/node-exporter/memory.md",
        "measurement_used": {"type": "raw_metric", "name": "node_memory_MemAvailable_bytes", "source_metrics": []},
        "data_source": "prometheus", "query": 'node_memory_MemAvailable_bytes{node_id="node-02"}',
        "query_type": "instant", "time_range": {"time": "now"}, "explanation": "memory on selected node",
    }


def _instant_outcome(metric, value):
    return prometheus_client.ExecutionOutcome(
        status="success",
        raw_data={"resultType": "vector", "result": [
            {"metric": metric, "value": [1735689600, str(value)]},
        ]},
    )


def test_dependency_plan_scopes_dependent_query_and_synthesizes(skill_index, settings):
    enabled = _dependency_settings(settings)
    with patch.object(llm_client, "call_llm_json", side_effect=[
        _dependent_router_response(), MagicMock(parsed=_root_cpu_contract()), MagicMock(parsed=_dependent_memory_contract()),
    ]) as mock_llm, \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={
             "node_cpu_seconds_total": ["node_id", "mode"],
             "node_memory_MemAvailable_bytes": ["node_id"],
         }), \
         patch.object(prometheus_client, "query_instant", side_effect=[
             _instant_outcome({"node_id": "node-02"}, 38.7),
            _instant_outcome({
                "__name__": "node_memory_MemAvailable_bytes",
                "cluster": "simulated",
                "instance": "node-02:9200",
                "job": "simulated_fleet",
                "node_id": "node-02",
            }, 155649351721),
         ]):
        outcome = _run(pipeline.run_dependency_aware_pipeline(
            "Which node has the highest CPU utilization, and how much memory is available on that node?",
            skill_index, enabled,
        ))

    assert outcome.already_executed is True
    result = outcome.contract
    assert mock_llm.call_count == 3
    assert result["results"][1]["query"] == 'node_memory_MemAvailable_bytes{node_id="node-02"}'
    assert "node_id=node-02" in result["synthesis"]
    assert "38.7%" in result["synthesis"]
    assert "144.96 GB" in result["synthesis"]


def test_dependency_generator_stage_requires_instant_current_top_n(settings):
    instructions = pipeline._build_generator_instructions(
        _dependency_settings(settings), "read_query", dependency_stage=True
    )
    assert "instant query for that ranking" in instructions


def test_dependent_execution_failure_keeps_partial_result_and_null_synthesis(skill_index, settings):
    enabled = _dependency_settings(settings)
    with patch.object(llm_client, "call_llm_json", side_effect=[
        _dependent_router_response(), MagicMock(parsed=_root_cpu_contract()), MagicMock(parsed=_dependent_memory_contract()),
    ]), \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={
             "node_cpu_seconds_total": ["node_id", "mode"],
             "node_memory_MemAvailable_bytes": ["node_id"],
         }), \
         patch.object(prometheus_client, "query_instant", side_effect=[
             _instant_outcome({"node_id": "node-02"}, 38.7),
             prometheus_client.ExecutionOutcome(status="endpoint_error", error="backend timeout"),
         ]):
        outcome = _run(pipeline.run_dependency_aware_pipeline("top CPU and memory on that node", skill_index, enabled))

    assert outcome.already_executed is True
    result = outcome.contract
    assert result["results"][0]["execution"]["execution_status"] == "success"
    assert result["results"][1]["execution"]["execution_status"] == "endpoint_error"
    assert result["synthesis"] is None


def test_opensearch_side_of_generator_context_only_built_when_matched(skill_index, settings):
    """field_discovery must not be hit at all for a pure-Prometheus request
    -- confirms datasource-scoped context building, not a blanket call."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [],
    })
    gen_contract = {
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_load1", "source_metrics": []},
        "data_source": "prometheus", "query": "node_load1",
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "test",
    }
    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, MagicMock(parsed=gen_contract)]), \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={"node_load1": []}), \
         patch.object(field_discovery, "discover_attributes_for_all_known_patterns") as mock_field_discovery:
        _run(pipeline.run_pipeline("load average", skill_index, settings))

    assert not mock_field_discovery.called


# ---- Alert-rule creation (SKILL.md Section 12) ----------------------------------


def _alert_settings(skills_root, **overrides):
    from app.config import Settings
    defaults = dict(
        gemini_api_key="test-key-not-real", skills_root=skills_root,
        prometheus_url="http://localhost:9090", opensearch_url="http://localhost:9600",
        alert_rule_creation_enabled=True, grafana_url="http://localhost:3000",
        grafana_service_account_token="glsa_test", grafana_default_folder_uid="alerts-folder",
        grafana_default_datasource_uid="prom-uid",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_alert_rule_creation_disabled_by_default_forces_out_of_scope_action(skill_index, settings):
    """Defense-in-depth: even if the Router somehow returns action_intent
    'propose_alert_rule' while the feature flag is off, the pipeline must
    force a deterministic out_of_scope_action response -- the EXACT
    behavior this request would have gotten before Section 12 existed --
    and must NEVER call the Generator (only 1 LLM call)."""
    disabled = settings.model_copy(update={
        "alert_rule_creation_enabled": False,
        "dependent_query_resolution_enabled": False,
    })
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [], "action_intent": "propose_alert_rule",
    })
    assert disabled.alert_rule_creation_enabled is False
    with patch.object(llm_client, "call_llm_json", return_value=router_resp) as mock_call:
        result = _run(pipeline.run_pipeline("alert me if CPU exceeds 90%", skill_index, disabled))

    assert result["mode"] == "single"
    assert result["status"] == "out_of_scope_action"
    assert mock_call.call_count == 1  # Generator never ran


def test_explicit_alert_creation_is_refused_when_router_omits_action_intent(skill_index, settings):
    """The disabled boundary cannot rely on optional Router output fields."""
    disabled = settings.model_copy(update={
        "alert_rule_creation_enabled": False,
        "dependent_query_resolution_enabled": False,
    })
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/dcgm-exporter/thermal.md", "data_source": "prometheus"}],
        "panic_mode": False,
        "unresolved_topics": [],
        # Intentionally no action_intent: this is the live failure mode.
    })

    with patch.object(llm_client, "call_llm_json", return_value=router_resp) as mock_call:
        result = _run(pipeline.run_pipeline(
            "Create an alert when power draw exceeds 500 watts for 5 minutes.",
            skill_index,
            disabled,
        ))

    assert result["status"] == "out_of_scope_action"
    assert mock_call.call_count == 1  # Router ran once; Generator never ran.


def test_disabled_feature_blocks_a_leaked_generator_alert_proposal(skill_index, settings):
    """Section 9's generic schema must not bypass the disabled flag."""
    disabled = settings.model_copy(update={
        "alert_rule_creation_enabled": False,
        "dependent_query_resolution_enabled": False,
    })
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": False,
        "unresolved_topics": [],
    })
    leaked_contract = {
        "mode": "single", "status": "alert_rule_proposed",
        "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
        "data_source": "prometheus",
        "alert_rule": {"title": "leaked", "condition_query": "node_cpu_seconds_total", "comparison": {"operator": ">", "threshold": 90}, "for_duration": "5m"},
        "explanation": "leaked generator output",
    }

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, MagicMock(parsed=leaked_contract)]), \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={"node_cpu_seconds_total": []}):
        result = _run(pipeline.run_pipeline("ordinary read question", skill_index, disabled))

    assert result["status"] == "out_of_scope_action"


def test_router_prompt_omits_alert_addendum_when_flag_disabled(skill_index, settings):
    """The highest-risk regression surface: when the flag is off, the
    Router's system prompt must be byte-for-byte the base instructions --
    no mention of action_intent or alert-rule creation at all."""
    disabled = settings.model_copy(update={
        "alert_rule_creation_enabled": False,
        "dependent_query_resolution_enabled": False,
    })
    instructions = pipeline._build_router_instructions(disabled)
    assert instructions == pipeline._ROUTER_INSTRUCTIONS
    assert "action_intent" not in instructions


def test_router_prompt_includes_alert_addendum_when_flag_enabled(skill_index, settings):
    enabled = _alert_settings(settings.skills_root)
    instructions = pipeline._build_router_instructions(enabled)
    assert "action_intent" in instructions
    assert "propose_alert_rule" in instructions
    # Base instructions are still present verbatim, only extended:
    assert instructions.startswith(pipeline._ROUTER_INSTRUCTIONS)


def test_generator_prompt_omits_alert_addendum_for_ordinary_read_question_even_when_enabled(skill_index, settings):
    """Flag on, but THIS request wasn't tagged as alert-rule creation --
    the Generator must get the plain, unmodified instructions."""
    enabled = _alert_settings(settings.skills_root)
    instructions = pipeline._build_generator_instructions(enabled, "read_query")
    assert instructions == pipeline._GENERATOR_INSTRUCTIONS


def test_generator_prompt_includes_alert_addendum_only_when_both_enabled_and_intent_set(skill_index, settings):
    enabled = _alert_settings(settings.skills_root)
    instructions = pipeline._build_generator_instructions(enabled, "propose_alert_rule")
    assert "alert_rule_proposed" in instructions
    assert instructions.startswith(pipeline._GENERATOR_INSTRUCTIONS)


def test_alert_rule_proposed_gets_default_folder_and_null_datasource_uid_injected(skill_index, settings):
    """SKILL.md Section 12.5: folder/datasource_uid are never trusted from
    the Generator -- this pipeline must inject the deployment's configured
    default folder and force datasource_uid to null before validation,
    regardless of what the Generator supplied for either."""
    enabled = _alert_settings(settings.skills_root)
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [], "action_intent": "propose_alert_rule",
    })
    gen_contract = {
        "mode": "single", "status": "alert_rule_proposed",
        "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
        "data_source": "prometheus",
        "alert_rule": {
            "title": "High CPU utilization",
            "condition_query": '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100)',
            "comparison": {"operator": ">", "threshold": 90},
            "for_duration": "5m",
            "folder": None,  # deliberately omitted by the Generator
            "datasource_uid": "should-be-overwritten",  # deliberately wrong
        },
        "explanation": "Derived from the verified idle-based CPU utilization expression. Proposal only.",
    }
    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, MagicMock(parsed=gen_contract)]), \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={"node_cpu_seconds_total": []}):
        result = _run(pipeline.run_pipeline("alert me if CPU exceeds 90% for 5m", skill_index, enabled))

    assert result["status"] == "alert_rule_proposed"
    assert result["alert_rule"]["folder"] == "alerts-folder"
    assert result["alert_rule"]["datasource_uid"] is None


def test_alert_rule_proposed_missing_threshold_is_not_auto_created(skill_index, settings):
    """A malformed/incomplete alert_rule_proposed (no comparison object at
    all) must fail deterministic validation rather than being papered
    over -- this pipeline never invents a threshold either."""
    enabled = _alert_settings(settings.skills_root)
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [], "action_intent": "propose_alert_rule",
    })
    gen_contract = {
        "mode": "single", "status": "alert_rule_proposed",
        "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
        "data_source": "prometheus",
        "alert_rule": {
            "title": "High CPU utilization",
            "condition_query": '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100)',
            "for_duration": "5m",
            "folder": None,
            "datasource_uid": None,
        },
        "explanation": "Missing comparison -- should have been declined/parameter_requires_clarification instead.",
    }
    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, MagicMock(parsed=gen_contract)]), \
         patch.object(label_discovery, "discover_labels_for_metrics", return_value={"node_cpu_seconds_total": []}):
        result = _run(pipeline.run_pipeline("alert me on CPU", skill_index, enabled))

    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
