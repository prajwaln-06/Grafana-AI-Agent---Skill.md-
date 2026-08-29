from app import validator

REF = "references/node-exporter/cpu.md"
OVERVIEW_REF = "references/node-exporter/overview.md"
KNOWN_REFS = {REF, OVERVIEW_REF}
KNOWN_METRICS = {"node_load1", "node_cpu_seconds_total"}
KNOWN_DS = {"prometheus"}


def _ok_entry(**overrides):
    entry = {
        "status": "ok",
        "reference_used": REF,
        "measurement_used": {"type": "raw_metric", "name": "node_load1", "source_metrics": []},
        "data_source": "prometheus",
        "query": "node_load1",
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"},
        "explanation": "test",
    }
    entry.update(overrides)
    return entry


def _validate(entry, labels_by_metric=None):
    contract = {"mode": "single", **entry}
    return validator.validate_contract(
        contract, known_metrics=KNOWN_METRICS, labels_by_metric=labels_by_metric or {},
        known_references=KNOWN_REFS, known_datasources=KNOWN_DS,
    )


# ---- structural / top-level shape ------------------------------------------------


def test_missing_mode_fails():
    r = validator.validate_contract({"status": "ok"})
    assert not r.passed and "mode" in r.reason


def test_invalid_mode_value_fails():
    r = validator.validate_contract({"mode": "sideways", "status": "ok"})
    assert not r.passed


def test_multi_with_fewer_than_two_results_fails():
    r = validator.validate_contract({"mode": "multi", "results": [_ok_entry()], "synthesis": None},
                                     known_metrics=KNOWN_METRICS, known_references=KNOWN_REFS,
                                     known_datasources=KNOWN_DS)
    assert not r.passed and "2+" in r.reason


def test_multi_missing_synthesis_key_fails():
    r = validator.validate_contract({"mode": "multi", "results": [_ok_entry(), _ok_entry()]},
                                     known_metrics=KNOWN_METRICS, known_references=KNOWN_REFS,
                                     known_datasources=KNOWN_DS)
    assert not r.passed and "synthesis" in r.reason


def test_multi_with_two_valid_entries_passes():
    r = validator.validate_contract(
        {"mode": "multi", "results": [_ok_entry(), {"status": "unmapped", "explanation": "no logs domain"}],
         "synthesis": None},
        known_metrics=KNOWN_METRICS, known_references=KNOWN_REFS, known_datasources=KNOWN_DS,
    )
    assert r.passed, r.reason


def test_unknown_status_fails():
    r = _validate({"status": "definitely_not_a_real_status", "explanation": "x"})
    assert not r.passed and "Unknown status" in r.reason


# ---- ok / panic_mode_best_effort ------------------------------------------------


def test_valid_ok_entry_passes():
    r = _validate(_ok_entry(), labels_by_metric={"node_load1": ["instance", "job"]})
    assert r.passed, r.reason


def test_ok_entry_missing_required_field_fails():
    entry = _ok_entry()
    del entry["explanation"]
    r = _validate(entry)
    assert not r.passed and "explanation" in r.reason


def test_panic_mode_without_caveat_fails():
    entry = _ok_entry(status="panic_mode_best_effort")
    r = _validate(entry)
    assert not r.passed and "caveat" in r.reason


def test_panic_mode_with_caveat_passes():
    entry = _ok_entry(status="panic_mode_best_effort", caveat="interpreted broadly")
    r = _validate(entry, labels_by_metric={"node_load1": []})
    assert r.passed, r.reason


def test_reference_used_not_in_known_references_fails():
    entry = _ok_entry(reference_used="references/node-exporter/made-up.md")
    r = _validate(entry)
    assert not r.passed and "made-up.md" in r.reason


def test_datasource_not_in_known_datasources_fails():
    entry = _ok_entry(data_source="loki")
    r = _validate(entry)
    assert not r.passed and "loki" in r.reason


def test_derived_measurement_requires_source_metrics():
    entry = _ok_entry(measurement_used={"type": "derived_measurement", "name": "cpu_util_pct", "source_metrics": []})
    r = _validate(entry)
    assert not r.passed and "derived_measurement" in r.reason


def test_raw_metric_with_nonempty_source_metrics_fails():
    entry = _ok_entry(measurement_used={"type": "raw_metric", "name": "node_load1", "source_metrics": ["node_load5"]})
    r = _validate(entry)
    assert not r.passed and "raw_metric" in r.reason


def test_invalid_measurement_type_fails():
    entry = _ok_entry(measurement_used={"type": "guessed_metric", "name": "node_load1", "source_metrics": []})
    r = _validate(entry)
    assert not r.passed


# ---- Prometheus-specific: shape, fabricated metric, time grammar, Principle 9 ----


def test_prometheus_query_must_be_a_nonempty_string():
    entry = _ok_entry(query="")
    r = _validate(entry)
    assert not r.passed


def test_prometheus_query_must_be_a_string_not_a_dict():
    entry = _ok_entry(query={"not": "a promql string"})
    r = _validate(entry)
    assert not r.passed


def test_fabricated_metric_name_fails():
    entry = _ok_entry(
        measurement_used={"type": "raw_metric", "name": "node_totally_made_up", "source_metrics": []},
        query="node_totally_made_up",
    )
    r = _validate(entry)
    assert not r.passed and "not in any Metric Directory" in r.reason


def test_metric_name_must_actually_appear_in_query_string():
    entry = _ok_entry(query="node_load5")  # measurement_used.name is node_load1, query uses a different metric
    r = _validate(entry)
    assert not r.passed


def test_derived_measurement_all_source_metrics_must_be_known():
    entry = _ok_entry(
        measurement_used={"type": "derived_measurement", "name": "node_load1", "source_metrics": ["node_fabricated"]},
        query="node_load1 / node_fabricated",
    )
    r = _validate(entry)
    assert not r.passed and "node_fabricated" in r.reason


def test_time_range_missing_fails():
    entry = _ok_entry()
    del entry["time_range"]
    r = _validate(entry)
    assert not r.passed and "time_range" in r.reason


def test_time_range_bad_grammar_fails():
    entry = _ok_entry(time_range={"from": "yesterday", "to": "now", "step": "60s"})
    r = _validate(entry)
    assert not r.passed


def test_time_range_now_slash_d_suffix_is_accepted():
    """Regression test: an earlier draft of this validator used a regex that
    didn't accept the '/d' day-rounding suffix time_utils.py actually
    supports, which would have wrongly rejected a perfectly valid query."""
    entry = _ok_entry(time_range={"from": "now-1d/d", "to": "now/d", "step": "1h"})
    r = _validate(entry, labels_by_metric={"node_load1": []})
    assert r.passed, r.reason


def test_time_range_start_not_before_end_fails():
    entry = _ok_entry(time_range={"from": "now", "to": "now-1h", "step": "60s"})
    r = _validate(entry)
    assert not r.passed


def test_step_resolving_to_zero_seconds_fails():
    entry = _ok_entry(time_range={"from": "now-1h", "to": "now", "step": "0s"})
    r = _validate(entry)
    assert not r.passed


def test_invented_label_key_fails_principle_9():
    entry = _ok_entry(query='node_load1{node_id="node-1"}')
    r = _validate(entry, labels_by_metric={"node_load1": ["instance", "job"]})
    assert not r.passed and "node_id" in r.reason and "Principle 9" in r.reason


def test_confirmed_label_key_passes():
    entry = _ok_entry(query='node_load1{instance="node-1:9100"}')
    r = _validate(entry, labels_by_metric={"node_load1": ["instance", "job"]})
    assert r.passed, r.reason


def test_label_key_in_aggregation_clause_is_checked_too():
    entry = _ok_entry(query="sum by (node_id) (node_load1)")
    r = _validate(entry, labels_by_metric={"node_load1": ["instance", "job"]})
    assert not r.passed and "node_id" in r.reason


def test_confirmed_aggregation_label_passes():
    entry = _ok_entry(query="sum by (instance) (node_load1)")
    r = _validate(entry, labels_by_metric={"node_load1": ["instance", "job"]})
    assert r.passed, r.reason


def test_label_discovery_failure_downgrades_to_warning_not_a_failure():
    """If discovery genuinely failed (None, not []) for every metric this
    query touches, the validator cannot tell a real label from an invented
    one -- it must warn, not reject, since rejecting would be a false
    positive caused by our own infrastructure, not the model's output."""
    entry = _ok_entry(query='node_load1{instance="node-1"}')
    r = _validate(entry, labels_by_metric={"node_load1": None})
    assert r.passed
    assert any("DISCOVERY FAILED".lower() not in w.lower() and "could not be confirmed" in w for w in r.warnings)


def test_query_type_instant_uses_time_field_instead_of_range():
    entry = _ok_entry(query_type="instant", time_range={"time": "now"})
    r = _validate(entry, labels_by_metric={"node_load1": []})
    assert r.passed, r.reason


# ---- OpenSearch: structural-only (no domain reference exists yet) ---------------


def test_opensearch_entry_requires_dict_query():
    entry = _ok_entry(data_source="opensearch", query="not a dict", index="logs-*")
    r = validator.validate_contract(
        {"mode": "single", **entry}, known_datasources={"opensearch"}, known_references=KNOWN_REFS,
    )
    assert not r.passed


def test_opensearch_entry_requires_index():
    entry = _ok_entry(data_source="opensearch", query={"query": {"match_all": {}}})
    del entry["time_range"]
    r = validator.validate_contract(
        {"mode": "single", **entry}, known_datasources={"opensearch"}, known_references=KNOWN_REFS,
    )
    assert not r.passed and "index" in r.reason


def test_valid_opensearch_entry_passes_with_a_warning():
    entry = _ok_entry(data_source="opensearch", query={"query": {"match_all": {}}}, index="logs-*")
    del entry["time_range"]
    r = validator.validate_contract(
        {"mode": "single", **entry}, known_datasources={"opensearch"}, known_references=KNOWN_REFS,
    )
    assert r.passed
    assert r.warnings  # flagged as structural-only, not silently treated as fully confirmed


# ---- other statuses ---------------------------------------------------------------


def test_ambiguous_metric_requires_two_or_more_candidates():
    entry = {"status": "ambiguous_metric", "reference_used": REF, "clarification": "which one?",
             "explanation": "x", "candidates": [{"name": "a", "purpose": "p"}]}
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS)
    assert not r.passed


def test_ambiguous_metric_with_two_candidates_passes():
    entry = {"status": "ambiguous_metric", "reference_used": REF, "clarification": "which one?",
             "explanation": "x",
             "candidates": [{"name": "a", "purpose": "p"}, {"name": "b", "purpose": "q"}]}
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS)
    assert r.passed, r.reason


def test_declined_requires_valid_reason_enum():
    entry = {"status": "declined", "reason": "i_dont_like_it", "explanation": "x"}
    r = validator.validate_contract({"mode": "single", **entry})
    assert not r.passed


def test_declined_parameter_requires_clarification_needs_clarification_field():
    entry = {"status": "declined", "reason": "parameter_requires_clarification", "explanation": "x"}
    r = validator.validate_contract({"mode": "single", **entry})
    assert not r.passed and "clarification" in r.reason


def test_declined_nonsensical_input_passes_without_clarification():
    entry = {"status": "declined", "reason": "nonsensical_input", "explanation": "x"}
    r = validator.validate_contract({"mode": "single", **entry})
    assert r.passed, r.reason


def test_unsupported_metric_requires_fields():
    entry = {"status": "unsupported_metric", "reference_used": REF, "explanation": "x"}
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS)
    assert not r.passed  # missing requested_measurement


def test_unmapped_requires_explanation():
    r = validator.validate_contract({"mode": "single", "status": "unmapped"})
    assert not r.passed


def test_out_of_scope_action_requires_fields():
    r = validator.validate_contract({"mode": "single", "status": "out_of_scope_action", "explanation": "x"})
    assert not r.passed  # missing requested_action


# ---- alert_rule_proposed (SKILL.md Section 12) -------------------------------------


def _alert_entry(**overrides):
    entry = {
        "status": "alert_rule_proposed",
        "reference_used": REF,
        "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
        "data_source": "prometheus",
        "alert_rule": {
            "title": "High CPU utilization",
            "condition_query": '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100)',
            "comparison": {"operator": ">", "threshold": 90},
            "for_duration": "5m",
            "folder": "alerts-folder",
            "datasource_uid": None,
        },
        "explanation": "Derived from the verified idle-based CPU utilization expression. Proposal only.",
    }
    entry.update(overrides)
    return entry


def test_valid_alert_rule_proposed_passes():
    r = validator.validate_contract({"mode": "single", **_alert_entry()},
                                     known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    assert r.passed, r.reason


def test_alert_rule_proposed_missing_alert_rule_object_fails():
    entry = _alert_entry()
    del entry["alert_rule"]
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    assert not r.passed and "alert_rule" in r.reason


def test_alert_rule_proposed_reference_not_known_fails():
    entry = _alert_entry(reference_used="references/node-exporter/made-up.md")
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    assert not r.passed and "made-up.md" in r.reason


def test_alert_rule_proposed_non_prometheus_datasource_fails():
    entry = _alert_entry(data_source="opensearch")
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS,
                                     known_datasources={"opensearch"})
    assert not r.passed and "prometheus" in r.reason


def test_alert_rule_proposed_condition_query_must_reference_the_resolved_metric():
    """Section 12.4's core non-fabrication check: a condition_query that
    doesn't even mention the resolved metric can't have been derived from
    that metric's verified base expression."""
    entry = _alert_entry(alert_rule={
        **_alert_entry()["alert_rule"], "condition_query": "some_other_metric_entirely > 90",
    })
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    assert not r.passed and "node_cpu_seconds_total" in r.reason


def test_alert_rule_proposed_missing_comparison_fails():
    alert_rule = {k: v for k, v in _alert_entry()["alert_rule"].items() if k != "comparison"}
    entry = _alert_entry(alert_rule=alert_rule)
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    assert not r.passed and "comparison" in r.reason


def test_alert_rule_proposed_invalid_comparison_operator_fails():
    alert_rule = {**_alert_entry()["alert_rule"], "comparison": {"operator": "roughly", "threshold": 90}}
    entry = _alert_entry(alert_rule=alert_rule)
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    assert not r.passed and "operator" in r.reason


def test_alert_rule_proposed_non_numeric_threshold_fails():
    """Never invent a threshold -- and never silently coerce a
    non-numeric one either."""
    alert_rule = {**_alert_entry()["alert_rule"], "comparison": {"operator": ">", "threshold": "high"}}
    entry = _alert_entry(alert_rule=alert_rule)
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    assert not r.passed and "threshold" in r.reason


def test_alert_rule_proposed_numeric_string_threshold_is_coerced():
    """Defensive coercion: a threshold that came through as '90' or '90%'
    (the LLM ignoring 'JSON number, not string') carries the same
    user-supplied information as 90 -- the non-fabrication rule is about
    not INVENTING a threshold, not about JSON typing. Must accept."""
    for raw in ["90", "90.5", "90%", "  85 ", "72°"]:
        alert_rule = {**_alert_entry()["alert_rule"], "comparison": {"operator": ">", "threshold": raw}}
        entry = _alert_entry(alert_rule=alert_rule)
        r = validator.validate_contract({"mode": "single", **entry},
                                         known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
        assert r.passed, f"expected {raw!r} to coerce, got: {r.reason}"


def test_alert_rule_proposed_missing_threshold_fails():
    alert_rule = {**_alert_entry()["alert_rule"], "comparison": {"operator": ">"}}
    entry = _alert_entry(alert_rule=alert_rule)
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    assert not r.passed and "threshold" in r.reason


def test_alert_rule_proposed_missing_for_duration_fails():
    alert_rule = {k: v for k, v in _alert_entry()["alert_rule"].items() if k != "for_duration"}
    entry = _alert_entry(alert_rule=alert_rule)
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    assert not r.passed and "for_duration" in r.reason


def test_alert_rule_proposed_missing_folder_fails():
    """Section 12.5: folder must have already been supplied by the
    surrounding application (pipeline.py's _apply_alert_rule_defaults) by
    the time this reaches validation -- an empty folder here means that
    deterministic step was skipped or deployment config is missing."""
    alert_rule = {**_alert_entry()["alert_rule"], "folder": None}
    entry = _alert_entry(alert_rule=alert_rule)
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    assert not r.passed and "folder" in r.reason


def test_alert_rule_proposed_nonnull_datasource_uid_fails():
    """Section 12.5: datasource_uid must be null at this stage -- it's the
    surrounding application's job to resolve it, only at confirmation time,
    never the Generator's."""
    alert_rule = {**_alert_entry()["alert_rule"], "datasource_uid": "should-not-be-set-yet"}
    entry = _alert_entry(alert_rule=alert_rule)
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    assert not r.passed and "datasource_uid" in r.reason


def test_alert_rule_proposed_missing_title_fails():
    alert_rule = {k: v for k, v in _alert_entry()["alert_rule"].items() if k != "title"}
    entry = _alert_entry(alert_rule=alert_rule)
    r = validator.validate_contract({"mode": "single", **entry}, known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    assert not r.passed and "title" in r.reason


def test_alert_rule_proposed_is_always_single_mode_in_the_documented_set():
    """Documents the Section 12.5 invariant even though nothing in this
    codebase currently enforces it structurally -- see validator.py's
    _ALWAYS_SINGLE_STATUSES."""
    assert "alert_rule_proposed" in validator._ALWAYS_SINGLE_STATUSES


def test_alert_rule_proposed_is_a_valid_top_level_status():
    assert "alert_rule_proposed" in validator._VALID_STATUSES