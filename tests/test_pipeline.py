import asyncio
from unittest.mock import MagicMock, patch

from app import field_discovery, label_discovery, llm_client, pipeline


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
         patch.object(label_discovery, "discover_label_details_for_metrics",
                       return_value={"node_load1": label_discovery.LabelDetails(keys=["instance", "job"], values={})}):
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
         patch.object(label_discovery, "discover_label_details_for_metrics",
                       return_value={"node_load1": label_discovery.LabelDetails(keys=["node_id", "instance", "job", "cluster"], values={})}):
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
         patch.object(label_discovery, "discover_label_details_for_metrics",
                       return_value={"node_load1": label_discovery.LabelDetails(keys=["instance", "job"], values={})}):
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
         patch.object(label_discovery, "discover_label_details_for_metrics", return_value={"node_load1": label_discovery.LabelDetails(keys=[], values={})}):
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
         patch.object(label_discovery, "discover_label_details_for_metrics", return_value={}):
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
         patch.object(label_discovery, "discover_label_details_for_metrics",
                       return_value={"node_load1": label_discovery.LabelDetails(keys=[], values={}), "node_memory_MemAvailable_bytes": label_discovery.LabelDetails(keys=[], values={})}):
        result = _run(pipeline.run_pipeline("CPU and available memory", skill_index, settings))

    assert result["mode"] == "multi"
    assert len(result["results"]) == 2


# ---- Corrective patch: `mode` holding a status value, no `mode` repair -----
#
# Live failure C traced: "How can I tell if the system is overloaded?" --
# the Generator, confused by SKILL.md Section 9's ambiguous_metric/
# unsupported_metric examples not showing "mode": "single" explicitly (now
# fixed), produced `{"mode": "ambiguous_metric", ...}` with no separate
# "status" key at all. `_normalize_contract_shape` previously left any
# contract with a pre-existing "mode" key untouched (regardless of its
# value), so this failed validation with "'mode' must be 'single' or
# 'multi'; got 'ambiguous_metric'." -- discarding a substantively-correct
# answer over the same class of envelope confusion as the missing-mode
# case above.


def test_normalize_contract_shape_repairs_known_status_value_placed_in_mode():
    """Unit-level check of the normalizer itself, independent of the full
    pipeline: a known status value in `mode`, no `status` key, gets
    repaired to mode:"single" + status:<value>, with every other field
    preserved unchanged."""
    contract = {
        "mode": "ambiguous_metric",
        "reference_used": "references/node-exporter/cpu.md",
        "candidates": [
            {"name": "node_load1", "purpose": "1-minute load average"},
            {"name": "node_cpu_seconds_total", "purpose": "CPU time by mode"},
        ],
        "clarification": "Do you mean system load average or CPU utilization?",
        "explanation": "Both plausibly represent 'overloaded'.",
    }
    result = pipeline._normalize_contract_shape(contract)
    assert result["mode"] == "single"
    assert result["status"] == "ambiguous_metric"
    assert result["candidates"] == contract["candidates"]
    assert result["clarification"] == contract["clarification"]


def test_normalize_contract_shape_does_not_touch_a_genuinely_unrecognized_mode():
    """A `mode` value that is neither 'single'/'multi' nor a recognized
    status (truly malformed input, not this specific near-miss) must be
    left untouched -- the deterministic validator still reports it, rather
    than this function silently guessing at unrecognized input."""
    contract = {"mode": "not_a_real_mode_or_status", "status": "ok"}
    assert pipeline._normalize_contract_shape(contract) == contract

    contract_no_status = {"mode": "not_a_real_mode_or_status", "explanation": "x"}
    assert pipeline._normalize_contract_shape(contract_no_status) == contract_no_status


def test_normalize_contract_shape_does_not_touch_a_status_value_in_mode_if_status_already_present():
    """If `status` is already present, a `mode` value that happens to also
    look like a status is left alone -- this is a different, more
    ambiguous malformation than the one observed live (which always had NO
    separate status key), so it's deliberately not guessed at."""
    contract = {"mode": "ambiguous_metric", "status": "ok", "explanation": "x"}
    assert pipeline._normalize_contract_shape(contract) == contract


def test_generator_ambiguous_metric_status_value_in_mode_is_repaired_end_to_end(skill_index, settings):
    """Full pipeline regression test for the live failure: Router matches
    a CPU/load-ambiguous reference, Generator emits the malformed
    {"mode": "ambiguous_metric", ...} shape (no "status" key), and the
    final result must be the correctly-repaired ambiguous_metric response,
    not a validation_failed."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [],
    })
    gen_contract_status_in_mode = {
        "mode": "ambiguous_metric",
        "reference_used": "references/node-exporter/cpu.md",
        "candidates": [
            {"name": "node_load1", "purpose": "1-minute load average"},
            {"name": "node_cpu_seconds_total", "purpose": "CPU time spent by mode"},
        ],
        "clarification": "Do you mean system load average or CPU utilization percentage?",
        "explanation": "'Overloaded' could plausibly mean either high load average or high CPU busy time.",
    }
    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, MagicMock(parsed=gen_contract_status_in_mode)]), \
         patch.object(label_discovery, "discover_label_details_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline(
            "How can I tell if the system is overloaded?", skill_index, settings))

    assert result["mode"] == "single"
    assert result["status"] == "ambiguous_metric"
    assert result["clarification"]


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
         patch.object(label_discovery, "discover_label_details_for_metrics",
                       return_value={"node_load1": label_discovery.LabelDetails(keys=[], values={}), "node_memory_MemAvailable_bytes": label_discovery.LabelDetails(keys=[], values={})}):
        result = _run(pipeline.run_pipeline("load average and available memory", skill_index, settings))

    assert result["mode"] == "multi"
    assert len(result["results"]) == 2
    assert result["synthesis"] == "Load average and available memory for the requested node."


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
         patch.object(label_discovery, "discover_label_details_for_metrics", return_value={"node_load1": label_discovery.LabelDetails(keys=[], values={})}), \
         patch.object(field_discovery, "discover_attributes_for_all_known_patterns") as mock_field_discovery, \
         patch("app.opensearch_catalog.runtime.build_request_context") as mock_os_context:
        _run(pipeline.run_pipeline("load average", skill_index, settings))

    assert not mock_field_discovery.called
    assert not mock_os_context.called  # the OpenSearch registry/field view is never built for Prometheus-only requests


# ---- Alert-rule creation (SKILL.md Section 12) ----------------------------------


def _alert_settings(skills_root, **overrides):
    from app.config import Settings
    defaults = dict(
        gemini_api_key="test-key-not-real", skills_root=skills_root,
        prometheus_url="http://localhost:9090", opensearch_url="http://localhost:9600",
        alert_rule_creation_enabled=True, grafana_url="http://localhost:3000",
        grafana_service_account_token="glsa_test", grafana_default_folder_uid="alerts-folder",
        grafana_default_datasource_uid="prom-uid",
        # Isolate from .env: these tests only care about alert-rule-creation
        # flag behavior; the dependent-query-resolution flag is independent and
        # must not leak in from the deployment's .env to cause false failures.
        dependent_query_resolution_enabled=False,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_alert_rule_creation_disabled_by_default_forces_out_of_scope_action(skill_index, settings):
    """Defense-in-depth: even if the Router somehow returns action_intent
    'propose_alert_rule' while the feature flag is off, the pipeline must
    force a deterministic out_of_scope_action response -- the EXACT
    behavior this request would have gotten before Section 12 existed --
    and must NEVER call the Generator (only 1 LLM call)."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [], "action_intent": "propose_alert_rule",
    })
    assert settings.alert_rule_creation_enabled is False
    with patch.object(llm_client, "call_llm_json", return_value=router_resp) as mock_call:
        result = _run(pipeline.run_pipeline("alert me if CPU exceeds 90%", skill_index, settings))

    assert result["mode"] == "single"
    assert result["status"] == "out_of_scope_action"
    assert mock_call.call_count == 1  # Generator never ran


def test_router_prompt_omits_alert_addendum_when_flag_disabled(skill_index, settings):
    """The highest-risk regression surface: when the flag is off, the
    Router's system prompt must be byte-for-byte the base instructions --
    no mention of action_intent or alert-rule creation at all."""
    instructions = pipeline._build_router_instructions(settings)
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
         patch.object(label_discovery, "discover_label_details_for_metrics",
                       return_value={"node_cpu_seconds_total": label_discovery.LabelDetails(keys=["mode"], values={})}):
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
         patch.object(label_discovery, "discover_label_details_for_metrics", return_value={"node_cpu_seconds_total": label_discovery.LabelDetails(keys=[], values={})}):
        result = _run(pipeline.run_pipeline("alert me on CPU", skill_index, enabled))

    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS


# ---- Dependency-aware compound query resolution (opt-in) -----------------------


def _dependency_settings(skills_root, **overrides):
    from app.config import Settings
    defaults = dict(
        gemini_api_key="test-key-not-real", skills_root=skills_root,
        prometheus_url="http://localhost:9090", opensearch_url="http://localhost:9600",
        dependent_query_resolution_enabled=True,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_dependency_plan_absent_when_no_intent_declares_a_dependency():
    """The overwhelmingly common case: intent_id present on everything (a
    deployment with the flag on tags every request), but nothing actually
    depends on anything -- must return None so run_pipeline falls straight
    through to the ordinary flat path."""
    matched = [
        {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus", "intent_id": "q1"},
        {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus", "intent_id": "q2"},
    ]
    assert pipeline._extract_dependency_plan(matched) is None


def test_dependency_plan_falls_back_on_missing_intent_id():
    matched = [
        {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus", "intent_id": "q1"},
        {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus", "depends_on": ["q1"]},
    ]
    assert pipeline._extract_dependency_plan(matched) is None


def test_dependency_plan_falls_back_on_duplicate_intent_id():
    matched = [
        {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus",
         "intent_id": "q1", "depends_on": []},
        {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus",
         "intent_id": "q1", "depends_on": ["q1"]},
    ]
    assert pipeline._extract_dependency_plan(matched) is None


def test_dependency_plan_falls_back_on_self_dependency():
    matched = [
        {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus",
         "intent_id": "q1", "depends_on": ["q1"]},
    ]
    assert pipeline._extract_dependency_plan(matched) is None


def test_dependency_plan_falls_back_on_unknown_depends_on():
    matched = [
        {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus",
         "intent_id": "q1", "depends_on": []},
        {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus",
         "intent_id": "q2", "depends_on": ["ghost"]},
    ]
    assert pipeline._extract_dependency_plan(matched) is None


def test_dependency_plan_falls_back_on_cycle():
    matched = [
        {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus",
         "intent_id": "q1", "depends_on": ["q2"]},
        {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus",
         "intent_id": "q2", "depends_on": ["q1"]},
    ]
    assert pipeline._extract_dependency_plan(matched) is None


def test_dependency_plan_resolves_a_valid_chain():
    q1 = {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus",
          "intent_id": "q1", "depends_on": []}
    q2 = {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus",
          "intent_id": "q2", "depends_on": ["q1"]}
    plan = pipeline._extract_dependency_plan([q1, q2])
    assert plan is not None
    assert plan.roots == [q1]
    assert plan.dependent_order == [q2]


def test_router_prompt_includes_dependency_addendum_only_when_enabled(skill_index, settings):
    enabled = _dependency_settings(settings.skills_root)
    assert pipeline._build_router_instructions(settings) == pipeline._ROUTER_INSTRUCTIONS
    assert "DEPENDENT QUERY RESOLUTION IS ENABLED" in pipeline._build_router_instructions(enabled)


def test_generator_prompt_includes_dependency_addendum_only_when_enabled(settings):
    enabled = _dependency_settings(settings.skills_root)
    assert pipeline._build_generator_instructions(settings, "read_query") == pipeline._GENERATOR_INSTRUCTIONS
    assert "DEPENDENT QUERY RESOLUTION IS ENABLED" in pipeline._build_generator_instructions(enabled, "read_query")


def test_flag_disabled_ignores_dependency_metadata_entirely(skill_index, settings):
    """With the flag off, even a Router response that DOES carry
    intent_id/depends_on must be treated exactly like the ordinary flat
    path always has been -- one flat Generator call across every matched
    reference, no staged execution, no ALREADY_EXECUTED_MARKER."""
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [
            {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus",
             "intent_id": "q1", "depends_on": []},
            {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus",
             "intent_id": "q2", "depends_on": ["q1"]},
        ],
        "panic_mode": False, "unresolved_topics": [],
    })
    gen_contract = {
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_load1", "source_metrics": []},
        "data_source": "prometheus", "query": "node_load1",
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "test",
    }
    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, MagicMock(parsed=gen_contract)]) as mock_call, \
         patch.object(label_discovery, "discover_label_details_for_metrics",
                       return_value={"node_load1": label_discovery.LabelDetails(keys=[], values={})}):
        result = _run(pipeline.run_pipeline("cpu and memory on node-1", skill_index, settings))

    assert mock_call.call_count == 2
    assert pipeline.ALREADY_EXECUTED_MARKER not in result
    assert result["status"] == "ok"


def test_dependency_aware_end_to_end_happy_path(skill_index):
    """'Which node has the highest CPU utilization, and how much memory is
    available on that node?' -- the root resolves and executes in-process,
    and the dependent's single Generator call is fed the root's resolved
    `instance` label/value and produces a properly filtered query."""
    from app import prometheus_client

    enabled = _dependency_settings(skill_index.skills_root)

    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [
            {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus",
             "intent_id": "q1", "depends_on": []},
            {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus",
             "intent_id": "q2", "depends_on": ["q1"]},
        ],
        "panic_mode": False, "unresolved_topics": [],
    })
    root_contract = {
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
        "data_source": "prometheus",
        "query": 'topk(1, 100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))',
        "query_type": "instant", "time_range": {"time": "now"}, "explanation": "root",
    }
    dependent_contract = {
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/memory.md",
        "measurement_used": {"type": "raw_metric", "name": "node_memory_MemAvailable_bytes", "source_metrics": []},
        "data_source": "prometheus", "query": 'node_memory_MemAvailable_bytes{instance="node-7:9100"}',
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "dependent",
    }

    label_details = {
        "node_cpu_seconds_total": label_discovery.LabelDetails(keys=["instance", "mode"], values={}),
        "node_memory_MemAvailable_bytes": label_discovery.LabelDetails(keys=["instance"], values={}),
    }
    root_outcome = prometheus_client.ExecutionOutcome(
        status="success",
        raw_data={"resultType": "vector", "result": [{"metric": {"instance": "node-7:9100"}, "value": [1735689600, "92.5"]}]},
    )
    dep_outcome = prometheus_client.ExecutionOutcome(
        status="success",
        raw_data={"resultType": "matrix", "result": [{"metric": {"instance": "node-7:9100"}, "values": [[1735689600, "2147483648"]]}]},
    )

    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, MagicMock(parsed=root_contract), MagicMock(parsed=dependent_contract)]) as mock_call, \
         patch.object(label_discovery, "discover_label_details_for_metrics", return_value=label_details), \
         patch.object(prometheus_client, "query_instant", return_value=root_outcome), \
         patch.object(prometheus_client, "query_range", return_value=dep_outcome):
        result = _run(pipeline.run_pipeline(
            "which node has the highest CPU utilization, and how much memory is available on that node?",
            skill_index, enabled,
        ))

    assert mock_call.call_count == 3  # router + root generator + one dependent generator
    assert result[pipeline.ALREADY_EXECUTED_MARKER] is True
    assert result["mode"] == "multi"
    assert [r["status"] for r in result["results"]] == ["ok", "ok"]
    assert result["results"][0]["execution"]["execution_status"] == "success"
    assert result["results"][1]["execution"]["execution_status"] == "success"
    assert result["synthesis"]  # deterministic, non-empty best-effort summary


def test_dependent_query_that_drops_the_resolved_filter_is_never_executed(skill_index):
    """If the dependent's own Generator call forgets to filter on the
    parent's resolved entity value, the pipeline must refuse to run it as
    an unfiltered query -- no query_range/query_instant call happens for
    that entry at all, and it surfaces as an ordinary execution-time
    endpoint_error instead."""
    from app import prometheus_client

    enabled = _dependency_settings(skill_index.skills_root)
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [
            {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus",
             "intent_id": "q1", "depends_on": []},
            {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus",
             "intent_id": "q2", "depends_on": ["q1"]},
        ],
        "panic_mode": False, "unresolved_topics": [],
    })
    root_contract = {
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
        "data_source": "prometheus",
        "query": 'topk(1, 100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))',
        "query_type": "instant", "time_range": {"time": "now"}, "explanation": "root",
    }
    # Forgets the instance="node-7:9100" filter entirely -- unsafe.
    unfiltered_dependent_contract = {
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/memory.md",
        "measurement_used": {"type": "raw_metric", "name": "node_memory_MemAvailable_bytes", "source_metrics": []},
        "data_source": "prometheus", "query": "node_memory_MemAvailable_bytes",
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "dependent",
    }
    label_details = {
        "node_cpu_seconds_total": label_discovery.LabelDetails(keys=["instance", "mode"], values={}),
        "node_memory_MemAvailable_bytes": label_discovery.LabelDetails(keys=["instance"], values={}),
    }
    root_outcome = prometheus_client.ExecutionOutcome(
        status="success",
        raw_data={"resultType": "vector", "result": [{"metric": {"instance": "node-7:9100"}, "value": [1735689600, "92.5"]}]},
    )

    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, MagicMock(parsed=root_contract), MagicMock(parsed=unfiltered_dependent_contract)]), \
         patch.object(label_discovery, "discover_label_details_for_metrics", return_value=label_details), \
         patch.object(prometheus_client, "query_instant", return_value=root_outcome), \
         patch.object(prometheus_client, "query_range") as mock_range:
        result = _run(pipeline.run_pipeline(
            "which node has the highest CPU utilization, and how much memory is available on that node?",
            skill_index, enabled,
        ))

    assert not mock_range.called  # the unsafe query was never actually run
    dependent_entry = result["results"][1]
    assert dependent_entry["status"] == "ok"
    assert dependent_entry["execution"]["execution_status"] == "endpoint_error"
    assert "unfiltered" in dependent_entry["execution"]["error"]


def test_dependent_whose_parent_failed_to_execute_gets_a_deterministic_fallback_entry(skill_index):
    """If the root's execution doesn't succeed, the dependent Generator
    call is still made (informed that no upstream value is available) --
    and if it can't produce something that validates, the pipeline injects
    a deterministic unsupported_metric entry rather than failing the whole
    response."""
    from app import prometheus_client

    enabled = _dependency_settings(skill_index.skills_root)
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [
            {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus",
             "intent_id": "q1", "depends_on": []},
            {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus",
             "intent_id": "q2", "depends_on": ["q1"]},
        ],
        "panic_mode": False, "unresolved_topics": [],
    })
    root_contract = {
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
        "data_source": "prometheus",
        "query": 'topk(1, 100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))',
        "query_type": "instant", "time_range": {"time": "now"}, "explanation": "root",
    }
    # Deliberately broken (missing required 'query') so validation fails.
    broken_dependent_contract = {
        "mode": "single", "status": "ok", "reference_used": "references/node-exporter/memory.md",
        "measurement_used": {"type": "raw_metric", "name": "node_memory_MemAvailable_bytes", "source_metrics": []},
        "data_source": "prometheus", "time_range": {"from": "now-1h", "to": "now", "step": "60s"},
        "explanation": "dependent",
    }
    label_details = {
        "node_cpu_seconds_total": label_discovery.LabelDetails(keys=["instance", "mode"], values={}),
        "node_memory_MemAvailable_bytes": label_discovery.LabelDetails(keys=["instance"], values={}),
    }
    root_outcome = prometheus_client.ExecutionOutcome(status="timeout", error="Prometheus request timed out.")

    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, MagicMock(parsed=root_contract), MagicMock(parsed=broken_dependent_contract)]), \
         patch.object(label_discovery, "discover_label_details_for_metrics", return_value=label_details), \
         patch.object(prometheus_client, "query_instant", return_value=root_outcome):
        result = _run(pipeline.run_pipeline(
            "which node has the highest CPU utilization, and how much memory is available on that node?",
            skill_index, enabled,
        ))

    assert result[pipeline.ALREADY_EXECUTED_MARKER] is True
    root_entry, dependent_entry = result["results"]
    assert root_entry["execution"]["execution_status"] == "timeout"
    assert dependent_entry["status"] == "unsupported_metric"
    assert result["synthesis"] is None  # nothing in this response could be described


def test_dependency_path_falls_back_to_flat_when_root_needs_clarification(skill_index):
    """A root that comes back ambiguous must never be partially executed --
    the whole request instead re-resolves through the ordinary flat path
    across every matched reference, exactly as if the flag were off."""
    enabled = _dependency_settings(skill_index.skills_root)
    router_resp = MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [
            {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus",
             "intent_id": "q1", "depends_on": []},
            {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus",
             "intent_id": "q2", "depends_on": ["q1"]},
        ],
        "panic_mode": False, "unresolved_topics": [],
    })
    ambiguous_root_contract = {
        "mode": "single", "status": "ambiguous_metric",
        "reference_used": "references/node-exporter/cpu.md",
        "candidates": [{"name": "node_cpu_seconds_total", "purpose": "CPU time by mode"},
                       {"name": "node_load1", "purpose": "1-minute load average"}],
        "clarification": "Which CPU mode do you mean?",
        "explanation": "Multiple CPU-related metrics could answer this.",
    }
    flat_fallback_contract = {
        "mode": "multi",
        "results": [
            {"status": "ambiguous_metric", "reference_used": "references/node-exporter/cpu.md",
             "candidates": [{"name": "node_cpu_seconds_total", "purpose": "CPU time by mode"},
                            {"name": "node_load1", "purpose": "1-minute load average"}],
             "clarification": "Which CPU mode do you mean?",
             "explanation": "Multiple CPU-related metrics could answer this."},
            {"status": "ok", "reference_used": "references/node-exporter/memory.md",
             "measurement_used": {"type": "raw_metric", "name": "node_memory_MemAvailable_bytes", "source_metrics": []},
             "data_source": "prometheus", "query": "node_memory_MemAvailable_bytes",
             "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "test"},
        ],
        "synthesis": None,
    }
    label_details = {
        "node_cpu_seconds_total": label_discovery.LabelDetails(keys=["instance", "mode"], values={}),
        "node_memory_MemAvailable_bytes": label_discovery.LabelDetails(keys=["instance"], values={}),
    }
    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, MagicMock(parsed=ambiguous_root_contract), MagicMock(parsed=flat_fallback_contract)]), \
         patch.object(label_discovery, "discover_label_details_for_metrics", return_value=label_details):
        result = _run(pipeline.run_pipeline(
            "which node has the highest CPU utilization, and how much memory is available on that node?",
            skill_index, enabled,
        ))

    assert pipeline.ALREADY_EXECUTED_MARKER not in result
    assert result["mode"] == "multi"
    assert result["results"][0]["status"] == "ambiguous_metric"


# ---- Dependency-aware resolution: stage scope, provenance, fail-safe -----------
#
# Regression coverage for the "root generator over-produces -> count mismatch ->
# flat fallback -> unfiltered dependent query, null synthesis" failure. The
# mocked Generator here is a callable that sees the real prompt, so these tests
# can check what each stage is TOLD, and can feed it misbehaving contracts a
# live LLM was observed to produce. Uses the real label key (`node_id`).

import pytest  # noqa: E402

_Q = "which node has the highest CPU utilization, and how much memory is available on that node?"
_CPU_REF = "references/node-exporter/cpu.md"
_MEM_REF = "references/node-exporter/memory.md"
_OVERVIEW_REF = "references/node-exporter/overview.md"
_ROOT_QUERY = 'topk(1, 100 - (avg by (node_id) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))'
_NODE_LABELS = {
    "node_cpu_seconds_total": label_discovery.LabelDetails(keys=["node_id", "mode"], values={}),
    "node_memory_MemAvailable_bytes": label_discovery.LabelDetails(keys=["node_id"], values={}),
}


class _ScriptedLLM:
    """Stands in for llm_client.call_llm_json. `responders[i]` answers call i
    (a dict, or a callable taking the prompt). A call beyond the script raises
    IndexError, so any unexpected extra Generator call (e.g. a flat fallback)
    fails the test loudly instead of being absorbed."""

    def __init__(self, *responders):
        self.responders = list(responders)
        self.prompts: list[str] = []

    def __call__(self, **kwargs):
        self.prompts.append(kwargs["prompt"])
        responder = self.responders[len(self.prompts) - 1]
        return MagicMock(parsed=responder(kwargs["prompt"]) if callable(responder) else responder)

    @property
    def call_count(self):
        return len(self.prompts)


def _router_resp():
    return {
        "gate_stop": None,
        "matched_references": [
            {"reference_path": _CPU_REF, "data_source": "prometheus", "intent_id": "cpu_util", "depends_on": []},
            {"reference_path": _MEM_REF, "data_source": "prometheus", "intent_id": "mem_avail",
             "depends_on": ["cpu_util"]},
        ],
        "panic_mode": False, "unresolved_topics": [],
    }


def _cpu_contract(**overrides):
    contract = {
        "mode": "single", "status": "ok", "reference_used": _CPU_REF,
        "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
        "data_source": "prometheus", "query": _ROOT_QUERY, "query_type": "instant",
        "time_range": {"time": "now"}, "explanation": "root",
    }
    contract.update(overrides)
    return contract


def _mem_contract(query='node_memory_MemAvailable_bytes{node_id="node-00"}', **overrides):
    contract = {
        "mode": "single", "status": "ok", "reference_used": _MEM_REF,
        "measurement_used": {"type": "raw_metric", "name": "node_memory_MemAvailable_bytes", "source_metrics": []},
        "data_source": "prometheus", "query": query,
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "dependent",
    }
    contract.update(overrides)
    return contract


def _vector_outcome(*nodes_and_values):
    from app import prometheus_client
    return prometheus_client.ExecutionOutcome(status="success", raw_data={
        "resultType": "vector",
        "result": [{"metric": {"node_id": n}, "value": [1735689600, str(v)]} for n, v in nodes_and_values],
    })


def _matrix_outcome(node, value):
    from app import prometheus_client
    return prometheus_client.ExecutionOutcome(status="success", raw_data={
        "resultType": "matrix",
        "result": [{"metric": {"node_id": node}, "values": [[1735689600, str(value)]]}],
    })


def _run_compound(skill_index, llm, *, root_outcome=None, dep_outcome=None, real_catalog=False):
    """Runs the compound question through the real pipeline with the Router/
    Generator scripted and Prometheus mocked. Returns (result, llm,
    query_instant mock, query_range mock) -- root queries are instant,
    dependent (memory) queries are range, so each mock is attributable."""
    from pathlib import Path
    from app import prometheus_client

    overrides = {}
    if real_catalog:
        overrides["catalog_path"] = Path(__file__).resolve().parent.parent / "app" / "catalog" / "catalog.json"
    else:
        overrides["catalog_path"] = skill_index.skills_root.parent / "tests" / "__no_catalog_for_this_fixture__.json"
    enabled = _dependency_settings(skill_index.skills_root, **overrides)
    # pipeline._get_catalog caches module-globally and ignores a later
    # settings.catalog_path, so reset around every run -- otherwise a
    # real-catalog run leaks into every later test that expects no catalog.
    pipeline.reset_catalog_cache()
    try:
        with patch.object(llm_client, "call_llm_json", new=llm), \
             patch.object(label_discovery, "discover_label_details_for_metrics", return_value=_NODE_LABELS), \
             patch.object(prometheus_client, "query_instant",
                           return_value=root_outcome or _vector_outcome(("node-00", 38.72))) as inst, \
             patch.object(prometheus_client, "query_range",
                           return_value=dep_outcome or _matrix_outcome("node-00", 3.2 * 1024 ** 3)) as rng:
            result = _run(pipeline.run_pipeline(_Q, skill_index, enabled))
    finally:
        pipeline.reset_catalog_cache()
    return result, llm, inst, rng


def _dependent_filter_from_prompt(prompt: str) -> str:
    """A scripted dependent Generator that builds its query from the entity
    the pipeline put in its prompt -- proves the filter value traveled from
    the executed root result, rather than being baked into the test."""
    import re
    m = re.search(r'resolved: node_id="([^"]+)"', prompt)
    assert m, "dependent prompt carried no resolved node_id"
    return f'node_memory_MemAvailable_bytes{{node_id="{m.group(1)}"}}'


@pytest.mark.parametrize("real_catalog", [False, True], ids=["no-catalog", "real-catalog"])
def test_compound_question_resolves_one_root_then_one_dependent_filtered_on_returned_node(skill_index, real_catalog):
    """Scenarios 1, 2, 7: exactly one root result for the one root intent,
    one dependent result filtered on the node the root query actually
    returned (here node-03 -- not a value the test hardcodes into the
    dependent contract), and a valid multi response with a real synthesis."""
    from app import validator

    llm = _ScriptedLLM(
        _router_resp(),
        _cpu_contract(),
        lambda prompt: _mem_contract(_dependent_filter_from_prompt(prompt)),
    )
    result, llm, inst, rng = _run_compound(
        skill_index, llm, root_outcome=_vector_outcome(("node-03", 38.72)),
        dep_outcome=_matrix_outcome("node-03", 3.2 * 1024 ** 3), real_catalog=real_catalog,
    )

    assert llm.call_count == 3  # router + ONE root generator + ONE dependent generator; no flat fallback
    assert inst.call_count == 1 and inst.call_args.args[1] == _ROOT_QUERY
    assert rng.call_count == 1
    assert rng.call_args.args[1] == 'node_memory_MemAvailable_bytes{node_id="node-03"}'

    assert result[pipeline.ALREADY_EXECUTED_MARKER] is True
    assert result["mode"] == "multi"
    assert [r["reference_used"] for r in result["results"]] == [_CPU_REF, _MEM_REF]
    assert [r["execution"]["execution_status"] for r in result["results"]] == ["success", "success"]
    assert [s["labels"]["node_id"] for r in result["results"] for s in r["execution"]["series"]] == ["node-03"] * 2

    # Scenario 7: meaningful, non-null synthesis that says WHICH measurement each number is.
    synthesis = result["synthesis"]
    assert synthesis
    assert "node_cpu_seconds_total" in synthesis and "38.7%" in synthesis
    assert "node_memory_MemAvailable_bytes" in synthesis and "3.20 GB" in synthesis
    assert synthesis.count("node_id=node-03") == 2
    # ...and the response is a valid multi contract per the deterministic validator.
    final = {k: v for k, v in result.items() if k != pipeline.ALREADY_EXECUTED_MARKER}
    assert validator.validate_contract(final).passed


def test_root_and_dependent_prompts_are_scoped_to_their_own_intent(skill_index):
    """Root cause: both stages used to see the full compound question with no
    statement of which part they owned. Each must now be told."""
    llm = _ScriptedLLM(
        _router_resp(), _cpu_contract(),
        lambda prompt: _mem_contract(_dependent_filter_from_prompt(prompt)),
    )
    _run_compound(skill_index, llm)

    root_prompt, dependent_prompt = llm.prompts[1], llm.prompts[2]
    assert "ROOT STAGE" in root_prompt
    assert f"intent 'cpu_util' -> {_CPU_REF}" in root_prompt
    assert "exactly 1 result object" in root_prompt and 'mode "single"' in root_prompt
    assert _MEM_REF not in root_prompt.split("STAGE SCOPE")[1]  # scope block never names the dependent part

    assert "DEPENDENT STAGE" in dependent_prompt
    assert f"intent 'mem_avail' -> {_MEM_REF}" in dependent_prompt
    assert "exactly ONE result object" in dependent_prompt
    assert 'node_id="node-00"' in dependent_prompt  # entity resolved from the executed root


def test_root_overproducing_two_results_fails_safely_with_no_flat_fallback(skill_index):
    """Scenario 3. Both entries claim the root's own reference, so the
    (exporter-wide) validator PASSES this contract -- the exact 'root Generator
    call produced 2 result object(s) for 1 root intent(s)' log. It must not
    execute, must not trigger a flat re-resolution (which would produce the
    unfiltered memory query), and must not look like a success."""
    overproduced = {
        "mode": "multi", "synthesis": None,
        "results": [
            {k: v for k, v in _cpu_contract().items() if k != "mode"},
            {k: v for k, v in _mem_contract("node_memory_MemAvailable_bytes", reference_used=_CPU_REF).items()
             if k != "mode"},
        ],
    }
    llm = _ScriptedLLM(_router_resp(), overproduced)  # a 3rd call would raise IndexError
    result, llm, inst, rng = _run_compound(skill_index, llm)

    assert llm.call_count == 2
    assert not inst.called and not rng.called  # nothing was queried at all
    assert result["mode"] == "single"
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert pipeline.ALREADY_EXECUTED_MARKER not in result
    assert "results" not in result and "execution" not in result


def test_root_result_claiming_an_unrouted_reference_is_rejected_before_execution(skill_index):
    """The validator accepts the shared overview.md as a 'known reference',
    so provenance for the root has to be checked against the routed one."""
    llm = _ScriptedLLM(_router_resp(), _cpu_contract(reference_used=_OVERVIEW_REF))
    result, llm, inst, rng = _run_compound(skill_index, llm)

    assert llm.call_count == 2
    assert not inst.called and not rng.called
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS


@pytest.mark.parametrize("malformed, expected_fragment", [
    pytest.param({"mode": "multi", "synthesis": None, "results": [
        {"reference_used": _CPU_REF, "query": _ROOT_QUERY}, {"reference_used": _MEM_REF, "query": "x"}]},
        "'status' field", id="results-missing-status"),
    pytest.param({**_cpu_contract(), "mode": "bogus"}, "'mode' must be", id="invalid-mode"),
    pytest.param({"mode": "multi", "synthesis": None, "results": [
        {k: v for k, v in _cpu_contract().items() if k != "mode"}]},
        "2+ result objects", id="multi-with-one-result"),
    pytest.param(_cpu_contract(reference_used="references/node-exporter/not-a-real-file.md"),
                 "not one of the references", id="unverified-reference_used"),
])
def test_malformed_root_contract_is_rejected_and_nothing_executes(skill_index, malformed, expected_fragment):
    """Scenario 5 (root stage): the observed malformed shapes are rejected by
    the unchanged validator; the dependency path surfaces that as
    validation_failed and never falls back, executes, or calls the dependent."""
    llm = _ScriptedLLM(_router_resp(), malformed)
    result, llm, inst, rng = _run_compound(skill_index, llm)

    assert llm.call_count == 2
    assert not inst.called and not rng.called
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert expected_fragment in result["explanation"]


@pytest.mark.parametrize("malformed", [
    pytest.param({k: v for k, v in _mem_contract().items() if k != "status"}, id="missing-status"),
    pytest.param({**_mem_contract(), "mode": "bogus"}, id="invalid-mode"),
    pytest.param(_mem_contract(reference_used="references/node-exporter/not-a-real-file.md"),
                 id="unverified-reference_used"),
    pytest.param(_mem_contract(reference_used=_OVERVIEW_REF), id="reference-not-the-routed-one"),
    pytest.param({"mode": "multi", "synthesis": None, "results": [
        {k: v for k, v in _cpu_contract(reference_used=_OVERVIEW_REF).items() if k != "mode"},
        {k: v for k, v in _mem_contract().items() if k != "mode"}]}, id="multi-result-upstream-first"),
])
def test_malformed_dependent_contract_becomes_unsupported_and_never_executes(skill_index, malformed):
    """Scenario 5 (dependent stage). The multi case is the old silent
    `results[0]`: the first entry there is the UPSTREAM measurement."""
    llm = _ScriptedLLM(_router_resp(), _cpu_contract(), malformed)
    result, llm, inst, rng = _run_compound(skill_index, llm)

    assert inst.call_count == 1  # the root ran...
    assert not rng.called         # ...the dependent never did
    assert result["mode"] == "multi"
    assert [r["status"] for r in result["results"]] == ["ok", "unsupported_metric"]
    assert result["results"][1].get("execution") is None
    assert result["synthesis"] and "node_memory_MemAvailable_bytes" not in result["synthesis"]


@pytest.mark.parametrize("bad_query", [
    pytest.param("node_memory_MemAvailable_bytes", id="no-filter"),
    pytest.param('node_memory_MemAvailable_bytes{node_id="node-99"}', id="filter-on-a-different-node"),
    pytest.param('node_memory_MemAvailable_bytes{node_id=~".+"}', id="regex-match-all"),
])
def test_dependent_without_the_resolved_equality_filter_cannot_execute(skill_index, bad_query):
    """Scenario 4."""
    llm = _ScriptedLLM(_router_resp(), _cpu_contract(), _mem_contract(bad_query))
    result, llm, inst, rng = _run_compound(skill_index, llm)

    assert not rng.called
    dependent = result["results"][1]
    assert dependent["execution"]["execution_status"] == "endpoint_error"
    assert "unfiltered" in dependent["execution"]["error"]


@pytest.mark.parametrize("root_outcome", [
    pytest.param(__import__("app.prometheus_client", fromlist=["x"]).ExecutionOutcome(
        status="timeout", error="Prometheus request timed out."), id="root-endpoint-failed"),
    pytest.param(__import__("app.prometheus_client", fromlist=["x"]).ExecutionOutcome(
        status="success", raw_data={"resultType": "vector", "result": []}), id="root-empty-result"),
    pytest.param(_vector_outcome(("node-00", 38.7), ("node-01", 91.0), ("node-02", 12.0), ("node-03", 40.1)),
                 id="root-returned-four-nodes-so-no-single-winner"),
])
def test_unresolved_root_never_leads_to_an_unfiltered_dependent_query(skill_index, root_outcome):
    """Scenario 6 (+ the arbitrary-`series[0]` defect). The third script entry
    is an UNFILTERED memory query: if the pipeline ever asked the dependent
    Generator in this state, it would run it. It must not even be asked."""
    llm = _ScriptedLLM(_router_resp(), _cpu_contract(), _mem_contract("node_memory_MemAvailable_bytes"))
    result, llm, inst, rng = _run_compound(skill_index, llm, root_outcome=root_outcome)

    assert llm.call_count == 2          # router + root only: the dependent Generator was never called
    assert not rng.called               # and no memory query of any kind ran
    assert [r["status"] for r in result["results"]] == ["ok", "unsupported_metric"]
    assert "never run across every instance" in result["results"][1]["explanation"]


def test_root_needing_clarification_cannot_end_in_an_executed_unfiltered_dependent(skill_index):
    """The pause path re-resolves flat (no dependency context). If that flat
    answer would EXECUTE, it is the unfiltered dependent query -- fail instead."""
    ambiguous_root = {
        "mode": "single", "status": "ambiguous_metric", "reference_used": _CPU_REF,
        "candidates": [{"name": "node_cpu_seconds_total", "purpose": "CPU time by mode"},
                       {"name": "node_load1", "purpose": "1-minute load average"}],
        "clarification": "Which CPU measurement do you mean?", "explanation": "ambiguous",
    }
    flat_that_would_execute = {
        "mode": "multi", "synthesis": None,
        "results": [{k: v for k, v in _cpu_contract().items() if k != "mode"},
                    {k: v for k, v in _mem_contract("node_memory_MemAvailable_bytes").items() if k != "mode"}],
    }
    llm = _ScriptedLLM(_router_resp(), ambiguous_root, flat_that_would_execute)
    result, llm, inst, rng = _run_compound(skill_index, llm)

    assert llm.call_count == 3
    assert not inst.called and not rng.called
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert pipeline.ALREADY_EXECUTED_MARKER not in result


def test_ordinary_single_query_is_unchanged_with_dependency_flag_on(skill_index):
    """Scenario 8: a plain question with the flag on takes the flat path --
    two LLM calls, no stage-scope text, no staging marker."""
    router = {
        "gate_stop": None, "panic_mode": False, "unresolved_topics": [],
        "matched_references": [{"reference_path": _CPU_REF, "data_source": "prometheus",
                                "intent_id": "q1", "depends_on": []}],
    }
    llm = _ScriptedLLM(router, _cpu_contract(query='100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100)'))
    from app import prometheus_client
    enabled = _dependency_settings(
        skill_index.skills_root,
        catalog_path=skill_index.skills_root.parent / "tests" / "__no_catalog_for_this_fixture__.json")
    with patch.object(llm_client, "call_llm_json", new=llm), \
         patch.object(label_discovery, "discover_label_details_for_metrics", return_value=_NODE_LABELS), \
         patch.object(prometheus_client, "query_instant") as inst:
        result = _run(pipeline.run_pipeline("What is the CPU utilization of the system?", skill_index, enabled))

    assert llm.call_count == 2
    assert "STAGE SCOPE" not in llm.prompts[1]
    assert not inst.called  # flat path never executes in-pipeline (agent.py does)
    assert pipeline.ALREADY_EXECUTED_MARKER not in result
    assert result["mode"] == "single" and result["status"] == "ok"


# ---- unit coverage for the new deterministic helpers ---------------------------


def _executed_entry(*series_labels, status="ok", execution_status="success"):
    return {"status": status, "execution": {
        "execution_status": execution_status,
        "series": [{"labels": labels, "points": [{"timestamp": "t", "value": 1.5}]} for labels in series_labels],
    }}


def test_resolve_parent_entity_requires_exactly_one_series():
    one = pipeline._resolve_parent_entity("q1", _executed_entry({"instance": "n:9100", "node_id": "node-00"}))
    assert one == (("node_id", "node-00", 1.5), None)  # stable key preferred over `instance`

    for entry in (
        _executed_entry({"node_id": "node-00"}, {"node_id": "node-01"}),   # two candidates: never pick one
        _executed_entry(),                                                    # no series
        _executed_entry({}),                                                  # no labels to join on
        _executed_entry({"node_id": "node-00"}, execution_status="timeout"),
        _executed_entry({"node_id": "node-00"}, status="unsupported_metric"),
        None,
    ):
        resolved, reason = pipeline._resolve_parent_entity("q1", entry)
        assert resolved is None and reason


def test_index_executed_entries_verifies_count_and_reference_provenance():
    roots = [{"intent_id": "cpu_util", "reference_path": _CPU_REF}]
    ok_entry = {"status": "ok", "reference_used": _CPU_REF}
    indexed = pipeline._index_executed_entries({"mode": "single", **ok_entry}, roots)
    assert list(indexed) == ["cpu_util"] and indexed["cpu_util"]["reference_used"] == _CPU_REF
    assert pipeline._index_executed_entries({"mode": "multi", "results": [ok_entry, ok_entry]}, roots) is None
    assert pipeline._index_executed_entries(
        {"mode": "single", "status": "ok", "reference_used": _MEM_REF}, roots) is None
    # entries with no reference_used at all (e.g. declined/unmapped) carry nothing to contradict
    assert pipeline._index_executed_entries({"mode": "single", "status": "unmapped"}, roots) is not None


def test_stage_scope_texts_name_only_their_own_intents():
    two_roots = [{"intent_id": "a", "reference_path": _CPU_REF}, {"intent_id": "b", "reference_path": _MEM_REF}]
    text = pipeline._root_stage_scope_text(two_roots)
    assert "exactly 2 result object(s)" in text and 'mode "multi"' in text
    dep = pipeline._dependent_stage_scope_text({"intent_id": "mem_avail", "reference_path": _MEM_REF})
    assert "mem_avail" in dep and _MEM_REF in dep and "never mode \"multi\"" in dep
