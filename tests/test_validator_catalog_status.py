"""Catalog-status validation (validator.validate_contract's optional
`catalog_status_by_metric`). It must only ever ADD a rejection for
`discovered_pending_review` / `rejected` metrics -- never relax
`known_metrics`, and never do anything when no status data is supplied."""

from app import validator

REF = "references/node-exporter/cpu.md"
KNOWN_REFS = {REF, "references/node-exporter/overview.md"}
KNOWN_METRICS = {"node_load1", "node_cpu_seconds_total"}
KNOWN_DS = {"prometheus"}


def _ok_entry(metric="node_load1", source_metrics=None, query=None, **overrides):
    entry = {
        "status": "ok",
        "reference_used": REF,
        "measurement_used": {
            "type": "derived_measurement" if source_metrics else "raw_metric",
            "name": metric,
            "source_metrics": source_metrics or [],
        },
        "data_source": "prometheus",
        "query": query or metric,
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"},
        "explanation": "test",
    }
    entry.update(overrides)
    return entry


def _validate(contract, catalog_status_by_metric=None, known_metrics=KNOWN_METRICS):
    kwargs = dict(known_metrics=known_metrics, known_references=KNOWN_REFS, known_datasources=KNOWN_DS)
    if catalog_status_by_metric is not None:
        kwargs["catalog_status_by_metric"] = catalog_status_by_metric
    return validator.validate_contract(contract, **kwargs)


def _single(entry):
    return {"mode": "single", **entry}


def test_pending_review_metric_is_rejected_when_status_supplied():
    r = _validate(_single(_ok_entry()), {"node_load1": "discovered_pending_review"})
    assert not r.passed
    assert "node_load1" in r.reason and "discovered_pending_review" in r.reason


def test_rejected_metric_is_rejected_when_status_supplied():
    r = _validate(_single(_ok_entry()), {"node_load1": "rejected"})
    assert not r.passed and "rejected" in r.reason


def test_approved_and_approved_unavailable_metrics_pass():
    for status in ("approved", "approved_unavailable"):
        r = _validate(_single(_ok_entry()), {"node_load1": status})
        assert r.passed, (status, r.reason)


def test_metric_without_a_catalog_entry_passes():
    """No catalog opinion is never a rejection."""
    r = _validate(_single(_ok_entry()), {"some_other_metric": "rejected"})
    assert r.passed, r.reason


def test_no_status_data_is_a_no_op_even_for_a_would_be_pending_metric():
    assert _validate(_single(_ok_entry())).passed
    assert _validate(_single(_ok_entry()), {}).passed
    assert _validate(_single(_ok_entry()), None).passed


def test_status_never_relaxes_known_metrics():
    """An 'approved' status must not make a metric outside the Metric
    Directory valid."""
    entry = _ok_entry(metric="node_totally_made_up")
    r = _validate(_single(entry), {"node_totally_made_up": "approved"})
    assert not r.passed and "looks fabricated" in r.reason


def test_derived_measurement_with_a_pending_source_metric_is_rejected():
    entry = _ok_entry(
        metric="cpu_vs_load", source_metrics=["node_cpu_seconds_total", "node_load1"],
        query="rate(node_cpu_seconds_total[5m]) / node_load1",
    )
    known = KNOWN_METRICS | {"cpu_vs_load"}
    r = _validate(_single(entry), {"node_load1": "discovered_pending_review"}, known_metrics=known)
    assert not r.passed and "node_load1" in r.reason


def test_multi_mode_reports_which_result_was_rejected():
    contract = {
        "mode": "multi",
        "results": [
            _ok_entry(metric="node_cpu_seconds_total"),
            _ok_entry(metric="node_load1"),
        ],
        "synthesis": None,
    }
    r = _validate(contract, {"node_load1": "rejected"})
    assert not r.passed and r.reason.startswith("results[1]:")


# ---- alert-rule path shares the same policy ---------------------------------------


def _alert_entry(metric="node_cpu_seconds_total"):
    return {
        "status": "alert_rule_proposed",
        "reference_used": REF,
        "measurement_used": {"type": "raw_metric", "name": metric, "source_metrics": []},
        "data_source": "prometheus",
        "alert_rule": {
            "title": "High CPU utilization",
            "condition_query": f'100 - (avg(rate({metric}{{mode="idle"}}[1m])) * 100)',
            "comparison": {"operator": ">", "threshold": 90},
            "for_duration": "5m",
            "folder": "alerts-folder",
            "datasource_uid": None,
        },
        "explanation": "Proposal only.",
    }


def test_alert_proposal_on_a_pending_metric_is_rejected():
    r = _validate(_single(_alert_entry()), {"node_cpu_seconds_total": "discovered_pending_review"})
    assert not r.passed and "node_cpu_seconds_total" in r.reason


def test_alert_proposal_passes_for_approved_or_unknown_metric_or_no_status():
    assert _validate(_single(_alert_entry()), {"node_cpu_seconds_total": "approved"}).passed
    assert _validate(_single(_alert_entry()), {"unrelated": "rejected"}).passed
    assert _validate(_single(_alert_entry())).passed
