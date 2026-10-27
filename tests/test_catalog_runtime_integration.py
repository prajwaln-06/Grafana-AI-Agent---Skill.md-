"""
tests/test_catalog_runtime_integration.py

End-to-end integration of the metric lifecycle with the REAL runtime (no
patching of pipeline._get_catalog):

    reviews.json + catalog.json + (mocked) Prometheus
        -> CatalogRuntime snapshot
        -> pipeline._get_catalog / SkillIndex.metric_directory
        -> run_pipeline (LLM mocked, label discovery mocked)

Uses a tmp copy of the real catalog.json, the real skills tree and a real
review store; Prometheus and the LLM are mocked, so nothing here needs a live
service.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import llm_client, pipeline, validator
from app.catalog import reconciler as rc
from app.catalog import reviews as rv
from app.catalog import runtime as rt
from app.catalog.loader import CatalogLoadError
from app.catalog.schema import Catalog, CatalogEntry
from app.config import Settings
from app.skill_index import SkillIndex

NEW_METRIC = "node_pressure_cpu_waiting_seconds_total"
VENDOR_METRIC = "node_cpu_seconds_total"
REF = "references/node-exporter/cpu.md"
OVERVIEW = "references/node-exporter/overview.md"
QUESTION = "cpu pressure waiting on node-1"
APPROVED, UNAVAILABLE, PENDING, REJECTED = (
    "approved", "approved_unavailable", "discovered_pending_review", "rejected")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fresh():
    pipeline.reset_catalog_cache()
    yield
    pipeline.reset_catalog_cache()


@pytest.fixture
def stack(tmp_path, skills_root):
    shutil.copy(skills_root.parent / "app" / "catalog" / "catalog.json", tmp_path / "catalog.json")
    settings = Settings(
        gemini_api_key="x", skills_root=skills_root,
        catalog_path=tmp_path / "catalog.json", catalog_reviews_path=tmp_path / "reviews.json",
        prometheus_url="http://prom.test:9090", opensearch_url="http://localhost:9600",
        catalog_runtime_reconciliation_enabled=True,
    )
    runtime = rt.get_runtime(settings)
    index = SkillIndex.load(skills_root, settings.catalog_path, catalog_provider=lambda: runtime.catalog())
    vendor_names = {e.name for e in json.loads((tmp_path / "catalog.json").read_text())["metrics"]
                    for e in [CatalogEntry.from_dict(e)]}
    return tmp_path, settings, runtime, index, vendor_names


def _resp(data):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"status": "success", "data": data}
    return r


def _prom(vendor_names, extra=(NEW_METRIC,), fail=None, drop=()):
    names = sorted((set(vendor_names) | set(extra)) - set(drop))
    metadata = {NEW_METRIC: [{"type": "counter", "help": "Seconds spent waiting for CPU"}]}

    def _get(url, timeout=None):
        if fail:
            raise fail
        return _resp(names if url.endswith("/api/v1/label/__name__/values") else metadata)

    s = MagicMock()
    s.get.side_effect = _get
    return s


def _refresh(runtime, session):
    with patch.object(rc, "_get_session", return_value=session):
        return runtime.refresh()


def _decide(tmp_path, skills_root, vendor_names, *, approve=(), reject=()):
    """Records decisions through the real review API, then persists them."""
    path = tmp_path / "reviews.json"
    store = rv.load_reviews(path)
    for name in approve:
        store = rv.approve(
            store, name=name, category="CPU Pressure", exporter="node-exporter", reference_path=REF,
            discovered=rc.RuntimeMetricInfo("counter", "Seconds spent waiting for CPU"),
            vendor_names=vendor_names, skills_root=skills_root, replace=True).store
    for name in reject:
        store = rv.reject(store, name=name, discovered=rc.RuntimeMetricInfo("counter", "x"),
                          vendor_names=vendor_names, replace=True).store
    rv.save_reviews(store, path)


def _router():
    return MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": REF, "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": []})


def _generator(metric):
    return MagicMock(parsed={
        "mode": "single", "status": "ok", "reference_used": REF,
        "measurement_used": {"type": "raw_metric", "name": metric, "source_metrics": []},
        "data_source": "prometheus", "query": metric,
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "test"})


def _ask(index, settings, metric):
    with patch.object(llm_client, "call_llm_json", side_effect=[_router(), _generator(metric)]), \
         patch("app.label_discovery.discover_label_details_for_metrics", return_value={}):
        return _run(pipeline.run_pipeline(QUESTION, index, settings))


def _status(catalog, name):
    e = catalog.get(name)
    return e.status if e else None


# ---- approved discoveries become eligible only after reconcile --------------------------------


def test_pending_discovery_is_ineligible_then_approval_plus_refresh_makes_it_eligible(stack, skills_root):
    tmp, settings, runtime, index, vendor = stack
    assert _refresh(runtime, _prom(vendor)).status == rt.PUBLISHED
    assert _status(pipeline._get_catalog(settings), NEW_METRIC) == PENDING

    blocked = _ask(index, settings, NEW_METRIC)
    assert blocked["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert NEW_METRIC not in index.metric_directory(OVERVIEW)

    _decide(tmp, skills_root, vendor, approve=[NEW_METRIC])
    assert _status(pipeline._get_catalog(settings), NEW_METRIC) == PENDING  # not before the refresh
    assert _refresh(runtime, _prom(vendor)).status == rt.PUBLISHED

    assert _status(pipeline._get_catalog(settings), NEW_METRIC) == APPROVED
    assert NEW_METRIC in index.metric_directory(OVERVIEW)
    allowed = _ask(index, settings, NEW_METRIC)
    assert allowed["status"] == "ok" and allowed["measurement_used"]["name"] == NEW_METRIC


def test_approved_discovery_that_disappears_stays_valid_as_approved_unavailable(stack, skills_root):
    tmp, settings, runtime, index, vendor = stack
    _decide(tmp, skills_root, vendor, approve=[NEW_METRIC])
    _refresh(runtime, _prom(vendor))
    _refresh(runtime, _prom(vendor, drop=[NEW_METRIC]))
    assert _status(pipeline._get_catalog(settings), NEW_METRIC) == UNAVAILABLE
    assert NEW_METRIC in index.metric_directory(OVERVIEW)
    assert _ask(index, settings, NEW_METRIC)["status"] == "ok"  # same contract as an unavailable vendor metric


def test_pending_discoveries_never_appear_in_retrieval_or_the_known_metric_directory(stack):
    _, settings, runtime, index, vendor = stack
    _refresh(runtime, _prom(vendor))
    catalog = pipeline._get_catalog(settings)
    assert _status(catalog, NEW_METRIC) == PENDING
    from app.catalog import search
    assert NEW_METRIC not in {r.entry.name for r in search.search(catalog, "cpu pressure waiting seconds", top_n=100)}
    assert NEW_METRIC not in index.metric_directory(OVERVIEW)


# ---- rejections stay in force --------------------------------------------------------------------


def test_rejection_of_a_vendor_metric_survives_repeated_refreshes_and_blocks_generation(stack, skills_root):
    tmp, settings, runtime, index, vendor = stack
    assert _ask(index, settings, VENDOR_METRIC)["status"] == "ok"
    _decide(tmp, skills_root, vendor, reject=[VENDOR_METRIC])
    for _ in range(3):  # present at runtime every time -- rediscovery must not undo it
        _refresh(runtime, _prom(vendor))
        assert _status(pipeline._get_catalog(settings), VENDOR_METRIC) == REJECTED
        assert VENDOR_METRIC not in index.metric_directory(OVERVIEW)
    assert _ask(index, settings, VENDOR_METRIC)["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS


def test_rejection_of_a_discovered_metric_is_not_undone_by_later_refreshes(stack, skills_root):
    tmp, settings, runtime, index, vendor = stack
    _decide(tmp, skills_root, vendor, reject=[NEW_METRIC])
    for _ in range(3):
        _refresh(runtime, _prom(vendor))
        assert _status(pipeline._get_catalog(settings), NEW_METRIC) == REJECTED
    assert _ask(index, settings, NEW_METRIC)["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS


def test_clearing_a_rejection_restores_the_metric_on_the_next_refresh(stack, skills_root):
    tmp, settings, runtime, index, vendor = stack
    _decide(tmp, skills_root, vendor, reject=[VENDOR_METRIC])
    _refresh(runtime, _prom(vendor))
    store = rv.clear(rv.load_reviews(tmp / "reviews.json"), name=VENDOR_METRIC).store
    rv.save_reviews(store, tmp / "reviews.json")
    _refresh(runtime, _prom(vendor))
    assert _status(pipeline._get_catalog(settings), VENDOR_METRIC) == APPROVED


# ---- the validator enforces status even when the name is "known" ------------------------------------


@pytest.mark.parametrize("status", [PENDING, REJECTED])
def test_validator_rejects_pending_and_rejected_even_if_the_name_is_in_known_metrics(status):
    catalog = Catalog("1.0", "t", (CatalogEntry(
        name=NEW_METRIC, type="counter", category="CPU", priority="Medium",
        exporter="node-exporter", status=status, reference_path=REF),))
    contract = _generator(NEW_METRIC).parsed
    result = validator.validate_contract(
        contract, known_metrics={NEW_METRIC}, known_references={REF}, known_datasources={"prometheus"},
        catalog_status_by_metric=pipeline._catalog_status_by_metric(catalog))
    assert not result.passed and status in result.reason


def test_status_validation_is_active_with_either_flag_and_off_only_when_both_are_off(skills_root):
    def active(**kw):
        return pipeline._status_validation_active(Settings(gemini_api_key="x", skills_root=skills_root, **kw))
    assert active() is False
    assert active(catalog_metric_status_validation_enabled=True) is True
    assert active(catalog_runtime_reconciliation_enabled=True) is True


def test_level2_never_offers_pending_or_rejected_metrics_even_if_their_names_are_known():
    entries = tuple(CatalogEntry(name=n, type="gauge", category="CPU", priority="Medium",
                                 exporter="node-exporter", status=s, reference_path=REF)
                    for n, s in (("m_ok", APPROVED), ("m_gone", UNAVAILABLE), ("m_pending", PENDING), ("m_rej", REJECTED)))
    base = pipeline.GeneratorContext(known_prometheus_metrics={e.name for e in entries})
    ctx = pipeline._build_level2_generator_context(base, [{"reference_path": REF}], Catalog("1.0", "t", entries))
    assert {m["name"] for m in ctx.metric_metadata} == {"m_ok", "m_gone"}


# ---- failure behavior ----------------------------------------------------------------------------------


def test_discovery_failure_preserves_the_last_good_snapshot_for_the_pipeline(stack, skills_root):
    tmp, settings, runtime, index, vendor = stack
    _decide(tmp, skills_root, vendor, approve=[NEW_METRIC])
    _refresh(runtime, _prom(vendor))
    good = runtime.snapshot()
    out = _refresh(runtime, _prom(vendor, fail=requests.exceptions.ConnectionError("down")))
    assert out.status == rt.DISCOVERY_FAILED and runtime.snapshot() is good
    assert pipeline._get_catalog(settings) is good.catalog
    assert _ask(index, settings, NEW_METRIC)["status"] == "ok"  # nothing flipped to unavailable/empty


def test_cold_start_with_prometheus_down_serves_the_static_catalog_and_approves_nothing_new(stack):
    _, settings, runtime, index, vendor = stack
    out = _refresh(runtime, _prom(vendor, fail=requests.exceptions.Timeout()))
    assert out.status == rt.DISCOVERY_FAILED
    catalog = pipeline._get_catalog(settings)
    assert {m.status for m in catalog.metrics} == {APPROVED}          # vendor metrics, as before reconciliation existed
    assert NEW_METRIC not in {m.name for m in catalog.metrics}        # a pending discovery can never slip in
    assert _ask(index, settings, VENDOR_METRIC)["status"] == "ok"


def test_malformed_review_store_after_a_good_snapshot_keeps_serving_it(stack, skills_root):
    tmp, settings, runtime, index, vendor = stack
    _decide(tmp, skills_root, vendor, approve=[NEW_METRIC])
    _refresh(runtime, _prom(vendor))
    (tmp / "reviews.json").write_text('{"schema_version": 1, "decisions": [{"name": "broken"}]}')
    assert _refresh(runtime, _prom(vendor)).status == rt.INPUTS_INVALID
    assert _status(pipeline._get_catalog(settings), NEW_METRIC) == APPROVED
    assert _ask(index, settings, NEW_METRIC)["status"] == "ok"


def test_malformed_review_store_at_cold_start_degrades_like_a_corrupt_catalog_json(stack):
    tmp, settings, runtime, index, vendor = stack
    (tmp / "reviews.json").write_text("{broken")
    assert pipeline._get_catalog(settings) is None                    # pipeline's documented "no catalog" degradation
    with pytest.raises(CatalogLoadError):                              # fails loudly, as a corrupt catalog.json already does
        index.metric_directory(OVERVIEW)
    # ...and never silently runs with the vendor catalog while ignoring decisions on file:
    assert runtime.snapshot() is None


# ---- visibility without a manual cache reset ---------------------------------------------------------------


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_worker_publishes_and_a_review_becomes_visible_via_request_refresh_with_no_cache_reset(stack, skills_root):
    tmp, settings, runtime, index, vendor = stack
    settings = settings.model_copy(update={"catalog_runtime_refresh_seconds": 3600})
    runtime._settings = settings
    with patch.object(rc, "_get_session", return_value=_prom(vendor)):
        runtime.start_worker()
        try:
            assert _wait_for(lambda: (s := runtime.snapshot()) is not None and s.source == "reconciled")
            assert _status(pipeline._get_catalog(settings), NEW_METRIC) == PENDING
            _decide(tmp, skills_root, vendor, approve=[NEW_METRIC], reject=[VENDOR_METRIC])
            runtime.request_refresh()                                  # what /api/v1/admin/reload-skill does
            assert _wait_for(lambda: _status(pipeline._get_catalog(settings), NEW_METRIC) == APPROVED)
        finally:
            runtime.stop_worker()
    catalog = pipeline._get_catalog(settings)
    assert _status(catalog, VENDOR_METRIC) == REJECTED
    assert index.metric_directory(OVERVIEW).keys() == {
        m.name for m in catalog.metrics if m.exporter == "node-exporter" and m.status in {APPROVED, UNAVAILABLE}}


def test_there_is_a_single_catalog_cache_both_consumers_read_the_same_snapshot(stack, skills_root):
    tmp, settings, runtime, index, vendor = stack
    _decide(tmp, skills_root, vendor, approve=[NEW_METRIC], reject=[VENDOR_METRIC])
    _refresh(runtime, _prom(vendor))
    catalog = pipeline._get_catalog(settings)
    assert catalog is runtime.catalog() is rt.get_runtime(settings).catalog()
    for exporter_overview, exporter in ((OVERVIEW, "node-exporter"), ("references/dcgm-exporter/overview.md", "dcgm-exporter")):
        usable = {m.name for m in catalog.metrics if m.exporter == exporter and m.status in {APPROVED, UNAVAILABLE}}
        assert set(index.metric_directory(exporter_overview)) == usable


def test_reload_skill_makes_new_decisions_visible_without_discovery_when_reconciliation_is_off(tmp_path, skills_root):
    from app.agent import ObservabilityQueryBuilderAgent
    shutil.copy(skills_root.parent / "app" / "catalog" / "catalog.json", tmp_path / "catalog.json")
    settings = Settings(gemini_api_key="x", skills_root=skills_root, catalog_path=tmp_path / "catalog.json",
                        catalog_reviews_path=tmp_path / "reviews.json")
    assert settings.catalog_runtime_reconciliation_enabled is False
    vendor = {m["name"] for m in json.loads((tmp_path / "catalog.json").read_text())["metrics"]}
    agent = ObservabilityQueryBuilderAgent(name="app", description="t")
    with patch("app.agent.get_settings", return_value=settings), \
         patch.object(rc, "_get_session", side_effect=AssertionError("no discovery when disabled")):
        index = agent._get_skill_index()
        assert VENDOR_METRIC in index.metric_directory(OVERVIEW)
        _decide(tmp_path, skills_root, vendor, reject=[VENDOR_METRIC])
        assert VENDOR_METRIC in index.metric_directory(OVERVIEW)      # nothing changes by itself...
        agent.reload_skill()                                          # ...the documented reload does it
        assert VENDOR_METRIC not in agent._get_skill_index().metric_directory(OVERVIEW)
        assert _status(pipeline._get_catalog(settings), VENDOR_METRIC) == REJECTED


# ---- defaults and behavior preservation ---------------------------------------------------------------------


def test_defaults_leave_runtime_reconciliation_off_and_the_catalog_identical_to_before(skills_root):
    s = Settings(gemini_api_key="x", skills_root=skills_root)
    assert s.catalog_runtime_reconciliation_enabled is False
    assert s.catalog_runtime_refresh_seconds == 300.0
    assert s.catalog_metric_status_validation_enabled is False
    assert s.catalog_discovery_include_patterns == [] and s.catalog_discovery_exclude_patterns == []
    # No review store -> the effective catalog is exactly catalog.json's entries.
    pipeline.reset_catalog_cache()
    from app.catalog.loader import load_catalog
    real = load_catalog(skills_root.parent / "app" / "catalog" / "catalog.json")
    effective = pipeline._get_catalog(s.model_copy(update={
        "catalog_reviews_path": Path("/nonexistent/reviews.json")}))
    assert {m.name: (m.status, m.reference_path, m.exporter) for m in effective.metrics} == {
        m.name: (m.status, m.reference_path, m.exporter) for m in real.metrics}
