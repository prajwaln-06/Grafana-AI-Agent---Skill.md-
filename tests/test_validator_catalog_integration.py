"""
tests/test_validator_catalog_integration.py

Catalog-related validator tests ported from `main` (tests/test_validator.py,
"Phase 12/13 (Batch 4): catalog status integration" onward): catalog-status
checks, candidate-scope enforcement (Level 1) and the alert-proposal
non-fabrication check. Run against this branch's validator, which now carries
the same `candidate_metric_scope` / `candidate_scope_violation` /
`catalog_status_by_metric` behavior. The non-catalog validator tests on `main`
(label-VALUE checks) belong to a feature this branch does not have and are not
ported -- see the integration report.
"""
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


# ---- Phase 12/13 (Batch 4): catalog status integration -----------------------------
#
# CATALOG_STATUS_BY_METRIC below deliberately covers all four CatalogStatus
# values, so tests can flip a single metric's status without constructing a
# real Catalog object -- validator.py only ever consumes the small
# {name: status_string} mapping app/pipeline.py's _catalog_status_by_metric
# builds, never a Catalog itself.

CATALOG_STATUS_BY_METRIC = {
    "node_load1": "approved",
    "node_cpu_seconds_total": "approved",
    "node_memory_MemFree_bytes": "approved_unavailable",
    "node_filesystem_free_bytes": "discovered_pending_review",
    "node_disk_io_now": "rejected",
}


def _validate_with_catalog_status(entry, catalog_status_by_metric=None, known_metrics=None):
    contract = {"mode": "single", **entry}
    return validator.validate_contract(
        contract,
        known_metrics=KNOWN_METRICS if known_metrics is None else known_metrics,
        known_references=KNOWN_REFS, known_datasources=KNOWN_DS,
        catalog_status_by_metric=catalog_status_by_metric,
    )


# -- Phase 12: ordinary query path ---------------------------------------------------


def test_known_valid_metric_remains_valid_with_catalog_status_supplied():
    """Requirement #4/#5: a known metric with catalog status 'approved'
    passes exactly as it did before Phase 12."""
    r = _validate_with_catalog_status(_ok_entry(), catalog_status_by_metric=CATALOG_STATUS_BY_METRIC)
    assert r.passed, r.reason


def test_unknown_fabricated_metric_remains_rejected_by_known_metrics_first():
    """Requirement #4: known_metrics is still the FIRST gate -- a fabricated
    metric name is rejected on that basis before catalog status is ever
    consulted, regardless of what catalog_status_by_metric contains."""
    entry = _ok_entry(
        measurement_used={"type": "raw_metric", "name": "totally_made_up_metric", "source_metrics": []},
        query="totally_made_up_metric",
    )
    r = _validate_with_catalog_status(entry, catalog_status_by_metric=CATALOG_STATUS_BY_METRIC)
    assert not r.passed and "fabricated" in r.reason


def test_approved_unavailable_catalog_metric_is_consistent_with_intended_semantics():
    """Requirement #6: 'approved_unavailable' is NOT one of the disallowed
    statuses (only discovered_pending_review/rejected are) -- a metric with
    this status, if it's also in known_metrics, must remain valid. This
    mirrors search.py's own DEFAULT_STATUSES allow-list (approved +
    approved_unavailable), which validator.py deliberately does not
    second-guess."""
    known = KNOWN_METRICS | {"node_memory_MemFree_bytes"}
    entry = _ok_entry(
        measurement_used={"type": "raw_metric", "name": "node_memory_MemFree_bytes", "source_metrics": []},
        query="node_memory_MemFree_bytes",
    )
    r = _validate_with_catalog_status(entry, catalog_status_by_metric=CATALOG_STATUS_BY_METRIC, known_metrics=known)
    assert r.passed, r.reason


def test_discovered_pending_review_metric_does_not_become_normally_valid():
    """Requirement #7: even if a discovered_pending_review metric somehow
    also ended up in known_metrics, catalog status must still reject it --
    this is the core Phase 12 guarantee."""
    known = KNOWN_METRICS | {"node_filesystem_free_bytes"}
    entry = _ok_entry(
        measurement_used={"type": "raw_metric", "name": "node_filesystem_free_bytes", "source_metrics": []},
        query="node_filesystem_free_bytes",
    )
    r = _validate_with_catalog_status(entry, catalog_status_by_metric=CATALOG_STATUS_BY_METRIC, known_metrics=known)
    assert not r.passed and "discovered_pending_review" in r.reason


def test_rejected_metric_does_not_become_normally_valid():
    """Requirement #8: same guarantee for 'rejected'."""
    known = KNOWN_METRICS | {"node_disk_io_now"}
    entry = _ok_entry(
        measurement_used={"type": "raw_metric", "name": "node_disk_io_now", "source_metrics": []},
        query="node_disk_io_now",
    )
    r = _validate_with_catalog_status(entry, catalog_status_by_metric=CATALOG_STATUS_BY_METRIC, known_metrics=known)
    assert not r.passed and "rejected" in r.reason


def test_catalog_unavailable_does_not_weaken_known_metrics_validation():
    """Requirement #9: catalog_status_by_metric=None (flag off, or catalog
    failed to load) must not change known_metrics' own behavior at all --
    a fabricated metric is still rejected, a known-good metric still
    passes."""
    good = _validate_with_catalog_status(_ok_entry(), catalog_status_by_metric=None)
    assert good.passed, good.reason

    bad_entry = _ok_entry(
        measurement_used={"type": "raw_metric", "name": "totally_made_up_metric", "source_metrics": []},
        query="totally_made_up_metric",
    )
    bad = _validate_with_catalog_status(bad_entry, catalog_status_by_metric=None)
    assert not bad.passed and "fabricated" in bad.reason


def test_catalog_unavailable_does_not_cause_a_false_rejection():
    """Requirement #9 (converse direction): an empty catalog_status_by_metric
    must never be treated as 'reject everything' -- a legitimately known
    metric with no catalog-status entry at all still passes."""
    r = _validate_with_catalog_status(_ok_entry(), catalog_status_by_metric={})
    assert r.passed, r.reason


def test_existing_validator_behavior_remains_intact_without_catalog_status_kwarg():
    """Requirement #10: calling validate_contract exactly as every
    pre-Phase-12 caller did (no catalog_status_by_metric kwarg at all)
    behaves identically to before this phase existed."""
    r = _validate(_ok_entry(), labels_by_metric={"node_load1": ["instance", "job"]})
    assert r.passed, r.reason


def test_check_catalog_status_helper_is_a_noop_for_metric_with_no_catalog_entry():
    """A metric present in known_metrics but simply absent from
    catalog_status_by_metric (e.g. it predates the catalog, or belongs to a
    domain the catalog has no opinion about) must pass -- absence is not
    itself a disallowed status."""
    r = validator._check_catalog_status(["node_load1"], {"some_other_metric": "rejected"})
    assert r.passed, r.reason


# -- Phase 13: alert-rule proposal path -----------------------------------------------


def test_valid_alert_metric_passes_with_known_metrics_and_catalog_status():
    """Requirement #11."""
    entry = _alert_entry()  # measurement_used.name == "node_cpu_seconds_total"
    r = validator.validate_contract(
        {"mode": "single", **entry},
        known_references=KNOWN_REFS, known_datasources=KNOWN_DS,
        known_metrics=KNOWN_METRICS, catalog_status_by_metric=CATALOG_STATUS_BY_METRIC,
    )
    assert r.passed, r.reason


def test_unknown_alert_metric_is_rejected():
    """Requirement #12: this is the Phase 13 gap fix itself -- previously
    _validate_alert_rule_proposed never checked known_metrics at all."""
    entry = _alert_entry(measurement_used={"type": "raw_metric", "name": "made_up_alert_metric",
                                            "source_metrics": []})
    entry["alert_rule"] = {**entry["alert_rule"], "condition_query": "made_up_alert_metric > 90"}
    r = validator.validate_contract(
        {"mode": "single", **entry},
        known_references=KNOWN_REFS, known_datasources=KNOWN_DS, known_metrics=KNOWN_METRICS,
    )
    assert not r.passed and "fabricated" in r.reason


def test_discovered_pending_review_alert_metric_is_rejected():
    """Requirement #13."""
    known = KNOWN_METRICS | {"node_filesystem_free_bytes"}
    entry = _alert_entry(measurement_used={"type": "raw_metric", "name": "node_filesystem_free_bytes",
                                            "source_metrics": []})
    entry["alert_rule"] = {**entry["alert_rule"], "condition_query": "node_filesystem_free_bytes < 10"}
    r = validator.validate_contract(
        {"mode": "single", **entry},
        known_references=KNOWN_REFS, known_datasources=KNOWN_DS,
        known_metrics=known, catalog_status_by_metric=CATALOG_STATUS_BY_METRIC,
    )
    assert not r.passed and "discovered_pending_review" in r.reason


def test_rejected_alert_metric_is_rejected():
    """Requirement #14."""
    known = KNOWN_METRICS | {"node_disk_io_now"}
    entry = _alert_entry(measurement_used={"type": "raw_metric", "name": "node_disk_io_now",
                                            "source_metrics": []})
    entry["alert_rule"] = {**entry["alert_rule"], "condition_query": "node_disk_io_now > 1000"}
    r = validator.validate_contract(
        {"mode": "single", **entry},
        known_references=KNOWN_REFS, known_datasources=KNOWN_DS,
        known_metrics=known, catalog_status_by_metric=CATALOG_STATUS_BY_METRIC,
    )
    assert not r.passed and "rejected" in r.reason


def test_existing_alert_validation_rules_still_work_without_known_metrics_or_catalog_status():
    """Requirement #15: every existing alert test in this file (above)
    calls validate_contract without known_metrics/catalog_status_by_metric
    at all -- confirm that convention still yields a pass for a
    structurally-valid proposal, i.e. the new checks are additive-only."""
    r = validator.validate_contract({"mode": "single", **_alert_entry()},
                                     known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    assert r.passed, r.reason


def test_alert_and_query_paths_agree_on_rejected_status_for_the_same_metric():
    """Requirement: the two paths must not disagree about whether a
    pending/rejected metric is allowed (both call the same
    _check_catalog_status helper)."""
    known = KNOWN_METRICS | {"node_disk_io_now"}

    query_entry = _ok_entry(
        measurement_used={"type": "raw_metric", "name": "node_disk_io_now", "source_metrics": []},
        query="node_disk_io_now",
    )
    query_result = _validate_with_catalog_status(query_entry, catalog_status_by_metric=CATALOG_STATUS_BY_METRIC,
                                                  known_metrics=known)

    alert_entry = _alert_entry(measurement_used={"type": "raw_metric", "name": "node_disk_io_now",
                                                  "source_metrics": []})
    alert_entry["alert_rule"] = {**alert_entry["alert_rule"], "condition_query": "node_disk_io_now > 1000"}
    alert_result = validator.validate_contract(
        {"mode": "single", **alert_entry},
        known_references=KNOWN_REFS, known_datasources=KNOWN_DS,
        known_metrics=known, catalog_status_by_metric=CATALOG_STATUS_BY_METRIC,
    )

    assert not query_result.passed and not alert_result.passed
    assert "rejected" in query_result.reason and "rejected" in alert_result.reason


def test_multi_metric_derived_measurement_checks_every_source_metric_against_catalog_status():
    """Multi-metric coverage: a derived_measurement's source_metrics are
    checked individually, same as known_metrics already does -- a single
    rejected source metric is enough to fail the whole entry, even if the
    measurement's own top-level name is fine."""
    known = KNOWN_METRICS | {"node_disk_io_now", "node_load5"}
    entry = _ok_entry(
        measurement_used={"type": "derived_measurement", "name": "node_load5",
                           "source_metrics": ["node_disk_io_now"]},
        query="node_load5 / node_disk_io_now",
    )
    r = _validate_with_catalog_status(entry, catalog_status_by_metric=CATALOG_STATUS_BY_METRIC, known_metrics=known)
    assert not r.passed and "node_disk_io_now" in r.reason and "rejected" in r.reason

# -- Batch 2 (Phase 3): candidate-scope enforcement ----------------------------------


def _validate_with_scope(entry, candidate_metric_scope=None, known_metrics=None):
    return validator.validate_contract(
        {"mode": "single", **entry},
        known_metrics=KNOWN_METRICS if known_metrics is None else known_metrics,
        known_references=KNOWN_REFS, known_datasources=KNOWN_DS,
        candidate_metric_scope=candidate_metric_scope,
    )


def test_candidate_in_scope_passes():
    """A metric that's both known AND inside the current candidate scope
    validates exactly as it always did."""
    r = _validate_with_scope(_ok_entry(), candidate_metric_scope={"node_load1", "node_cpu_seconds_total"})
    assert r.passed, r.reason


def test_known_metric_outside_candidate_scope_is_rejected():
    """The core Phase 3 guarantee: node_load1 is a perfectly real,
    known_metrics-valid metric, but if the current attempt's candidate
    scope is {node_cpu_seconds_total} only, node_load1 must be rejected
    -- and flagged as a candidate_scope_violation specifically (so
    pipeline.py knows this is Level-2-retryable, unlike a fabricated
    metric)."""
    r = _validate_with_scope(_ok_entry(), candidate_metric_scope={"node_cpu_seconds_total"})
    assert not r.passed
    assert r.candidate_scope_violation
    assert "node_load1" in r.reason and "candidate scope" in r.reason


def test_fabricated_metric_outside_scope_is_rejected_by_known_metrics_first_not_scope():
    """Ordering requirement: known_metrics is checked BEFORE candidate
    scope, so a fabricated metric is rejected as fabricated (not flagged
    as a retryable candidate_scope_violation) even if it's also, by
    coincidence, absent from the candidate scope."""
    entry = _ok_entry(
        measurement_used={"type": "raw_metric", "name": "totally_made_up_metric", "source_metrics": []},
        query="totally_made_up_metric",
    )
    r = _validate_with_scope(entry, candidate_metric_scope={"node_cpu_seconds_total"})
    assert not r.passed
    assert not r.candidate_scope_violation
    assert "fabricated" in r.reason


def test_empty_candidate_scope_is_a_noop_like_catalog_status():
    """None/empty candidate_metric_scope means no additional restriction
    beyond known_metrics -- exactly what a Level-2 attempt, a
    flag-off deployment, or a Level-1 miss all look like."""
    assert _validate_with_scope(_ok_entry(), candidate_metric_scope=None).passed
    assert _validate_with_scope(_ok_entry(), candidate_metric_scope=set()).passed


def test_candidate_scope_and_catalog_status_are_independent_checks():
    """A metric can be IN the candidate scope but still rejected by
    catalog status (pending/rejected) -- the two checks don't shadow each
    other. Uses candidate_metric_scope alongside catalog_status_by_metric
    directly via validate_contract."""
    known = KNOWN_METRICS | {"node_filesystem_free_bytes"}
    entry = _ok_entry(
        measurement_used={"type": "raw_metric", "name": "node_filesystem_free_bytes", "source_metrics": []},
        query="node_filesystem_free_bytes",
    )
    r = validator.validate_contract(
        {"mode": "single", **entry},
        known_metrics=known, known_references=KNOWN_REFS, known_datasources=KNOWN_DS,
        candidate_metric_scope={"node_filesystem_free_bytes"},
        catalog_status_by_metric=CATALOG_STATUS_BY_METRIC,
    )
    assert not r.passed
    assert not r.candidate_scope_violation  # rejected by catalog status, not scope
    assert "discovered_pending_review" in r.reason


def test_multi_metric_scope_enforcement_checks_every_result_not_just_the_first():
    """Multi-mode: the first result is in scope, the second is not -- the
    whole contract must fail, flagged as a candidate_scope_violation,
    with the violating result's index in the reason."""
    in_scope = _ok_entry(measurement_used={"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
                          query="node_cpu_seconds_total")
    out_of_scope = _ok_entry()  # node_load1
    r = validator.validate_contract(
        {"mode": "multi", "results": [in_scope, out_of_scope], "synthesis": None},
        known_metrics=KNOWN_METRICS, known_references=KNOWN_REFS, known_datasources=KNOWN_DS,
        candidate_metric_scope={"node_cpu_seconds_total"},
    )
    assert not r.passed
    assert r.candidate_scope_violation
    assert "results[1]" in r.reason and "node_load1" in r.reason


def test_derived_measurement_source_metric_outside_scope_is_rejected():
    """Multi-metric within ONE entry: a derived_measurement's source_metrics
    are checked against the candidate scope individually, same as
    known_metrics/catalog-status already do."""
    known = KNOWN_METRICS | {"node_load5"}
    entry = _ok_entry(
        measurement_used={"type": "derived_measurement", "name": "node_load5",
                           "source_metrics": ["node_cpu_seconds_total"]},
        query="node_load5 / node_cpu_seconds_total",
    )
    r = _validate_with_scope(entry, candidate_metric_scope={"node_load5"}, known_metrics=known)
    assert not r.passed
    assert r.candidate_scope_violation
    assert "node_cpu_seconds_total" in r.reason


# -- Batch 2 (Phase 3): candidate-scope enforcement, alert-rule path -----------------


def test_alert_metric_in_candidate_scope_passes():
    entry = _alert_entry()  # measurement_used.name == "node_cpu_seconds_total"
    r = validator.validate_contract(
        {"mode": "single", **entry},
        known_references=KNOWN_REFS, known_datasources=KNOWN_DS, known_metrics=KNOWN_METRICS,
        candidate_metric_scope={"node_cpu_seconds_total"},
    )
    assert r.passed, r.reason


def test_alert_metric_outside_candidate_scope_is_rejected():
    entry = _alert_entry()  # measurement_used.name == "node_cpu_seconds_total"
    r = validator.validate_contract(
        {"mode": "single", **entry},
        known_references=KNOWN_REFS, known_datasources=KNOWN_DS, known_metrics=KNOWN_METRICS,
        candidate_metric_scope={"node_load1"},
    )
    assert not r.passed
    assert r.candidate_scope_violation


def test_check_candidate_scope_helper_directly():
    assert validator._check_candidate_scope(["node_load1"], None).passed
    assert validator._check_candidate_scope(["node_load1"], set()).passed
    assert validator._check_candidate_scope(["node_load1"], {"node_load1"}).passed
    r = validator._check_candidate_scope(["node_load1"], {"node_cpu_seconds_total"})
    assert not r.passed and r.candidate_scope_violation



# ---- status validation OFF / scope absent must not disable unrelated checks ----


def _validate_flag_off(entry, **kw):
    """Exactly what the pipeline sends when catalog status validation is off
    (no catalog_status_by_metric) and no candidate scope is active (Level 3)."""
    return validator.validate_contract(
        {"mode": "single", **entry}, known_metrics=KNOWN_METRICS,
        known_references=KNOWN_REFS, known_datasources=KNOWN_DS, **kw,
    )


def test_flag_off_still_rejects_fabricated_metric():
    r = _validate_flag_off(_ok_entry(
        measurement_used={"type": "raw_metric", "name": "made_up", "source_metrics": []}, query="made_up"))
    assert not r.passed and "fabricated" in r.reason


def test_flag_off_still_rejects_unknown_reference():
    r = _validate_flag_off(_ok_entry(reference_used="references/node-exporter/nope.md"))
    assert not r.passed


def test_flag_off_still_rejects_wrong_datasource():
    r = _validate_flag_off(_ok_entry(data_source="opensearch"))
    assert not r.passed


def test_flag_off_still_rejects_undiscovered_label():
    r = _validate_flag_off(_ok_entry(query='node_load1{bogus="x"}'),
                           labels_by_metric={"node_load1": ["instance", "job"]})
    assert not r.passed


def test_flag_off_alert_path_still_rejects_fabricated_metric():
    entry = _alert_entry(measurement_used={"type": "raw_metric", "name": "made_up", "source_metrics": []})
    r = _validate_flag_off(entry)
    assert not r.passed


# ---- alert condition_query Principle 9 (label keys), ported from main ----

_CPU_KEYS = {"node_cpu_seconds_total": ["cpu", "mode", "instance", "job"]}


def _alert_with_condition(cond):
    e = _alert_entry()
    e["alert_rule"]["condition_query"] = cond
    return e


def test_alert_condition_with_confirmed_label_key_passes():
    r = _validate_flag_off(_alert_entry(), labels_by_metric=_CPU_KEYS)
    assert r.passed, r.reason


def test_alert_condition_with_invented_label_key_is_rejected():
    r = _validate_flag_off(
        _alert_with_condition('100 - avg(rate(node_cpu_seconds_total{bogus="idle"}[1m]))'),
        labels_by_metric=_CPU_KEYS)
    assert not r.passed and "Principle 9" in r.reason


def test_alert_condition_label_check_skipped_without_discovery_data():
    r = _validate_flag_off(
        _alert_with_condition('100 - avg(rate(node_cpu_seconds_total{bogus="idle"}[1m]))'))
    assert r.passed, r.reason


def test_alert_condition_label_check_skipped_when_metric_discovery_failed():
    r = _validate_flag_off(
        _alert_with_condition('100 - avg(rate(node_cpu_seconds_total{bogus="idle"}[1m]))'),
        labels_by_metric={"node_cpu_seconds_total": None})
    assert r.passed, r.reason
