"""
tests/test_catalog_runtime.py

app/catalog/runtime.py: static snapshot, reconciled publish, last-known-good
preservation, overlap refusal, reader consistency during a refresh, worker
lifecycle, process-wide instance, and the server lifespan hook. Prometheus is
mocked; nothing touches the network.
"""
from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.catalog import reconciler as rc
from app.catalog import runtime as rt
from app.catalog.schema import Catalog, CatalogEntry, Priority
from app.config import Settings

REF = "references/node-exporter/cpu.md"
T = "2026-10-27T05:00:00Z"


@pytest.fixture(autouse=True)
def _fresh_runtime():
    rt.reset_runtime()
    yield
    rt.reset_runtime()


@pytest.fixture
def env(tmp_path):
    skills = tmp_path / "skills"
    (skills / "references" / "node-exporter").mkdir(parents=True)
    (skills / "references" / "node-exporter" / "overview.md").write_text("o")
    (skills / "references" / "node-exporter" / "cpu.md").write_text("c")
    cat = Catalog("1.0", "t", tuple(
        CatalogEntry(name=n, type="gauge", category="CPU", priority=Priority.REVIEW.value,
                     exporter="node-exporter", status="approved", help="h", reference_path=REF)
        for n in ("node_load1", "node_load5")))
    (tmp_path / "catalog.json").write_text(json.dumps(cat.to_dict(), indent=2) + "\n")
    settings = Settings(
        gemini_api_key="x", catalog_path=tmp_path / "catalog.json",
        catalog_reviews_path=tmp_path / "reviews.json", skills_root=skills,
        prometheus_url="http://prom.test:9090",
    )
    return tmp_path, settings


def _write_reviews(tmp_path, decisions):
    (tmp_path / "reviews.json").write_text(json.dumps({"schema_version": 1, "decisions": decisions}))


def _approval(name="acme_ok"):
    return {"name": name, "decision": "approved", "decided_at": T, "type": "gauge", "help": "ok",
            "category": "Queues", "exporter": "node-exporter", "reference_path": REF}


def _rejection(name):
    return {"name": name, "decision": "rejected", "decided_at": T}


def _resp(data):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"status": "success", "data": data}
    return r


def _session(names, metadata=None, fail=None):
    metadata = metadata or {}
    def _get(url, timeout=None):
        if fail:
            raise fail
        return _resp(list(names) if url.endswith("/api/v1/label/__name__/values") else metadata)
    s = MagicMock()
    s.get.side_effect = _get
    return s


def _status(catalog, name):
    e = catalog.get(name)
    return e.status if e else None


def _refresh(runtime, session, **kw):
    with patch.object(rc, "_get_session", return_value=session):
        return runtime.refresh(**kw)


# ---- static snapshot ----------------------------------------------------------


def test_static_snapshot_is_vendor_plus_decisions_with_rules_applied_and_no_network(env):
    tmp, settings = env
    _write_reviews(tmp, [_approval(), _rejection("node_load5")])
    with patch.object(rc, "_get_session", side_effect=AssertionError("no network for a static build")):
        catalog = rt.CatalogRuntime(settings).catalog()
    assert _status(catalog, "node_load1") == "approved"
    assert _status(catalog, "acme_ok") == "approved"
    assert _status(catalog, "node_load5") == "rejected"
    assert catalog.get("node_load1").priority != Priority.REVIEW.value or catalog.get("node_load1").keywords


def test_missing_review_store_means_the_plain_vendor_catalog(env):
    _, settings = env
    catalog = rt.CatalogRuntime(settings).catalog()
    assert {m.name: m.status for m in catalog.metrics} == {"node_load1": "approved", "node_load5": "approved"}


@pytest.mark.parametrize("breakage", ["bad_reviews", "bad_catalog", "missing_catalog"])
def test_unbuildable_inputs_publish_nothing_and_are_not_retried_per_request(env, breakage):
    tmp, settings = env
    if breakage == "bad_reviews":
        (tmp / "reviews.json").write_text("{broken")
    elif breakage == "bad_catalog":
        (tmp / "catalog.json").write_text("{broken")
    else:
        (tmp / "catalog.json").unlink()
    runtime = rt.CatalogRuntime(settings)
    assert runtime.catalog() is None
    with patch("app.catalog.runtime.load_inputs", side_effect=AssertionError("must not retry")):
        assert runtime.catalog() is None and runtime.catalog() is None


def test_a_later_successful_refresh_recovers_from_a_failed_first_build(env):
    tmp, settings = env
    (tmp / "reviews.json").write_text("{broken")
    runtime = rt.CatalogRuntime(settings)
    assert runtime.catalog() is None
    (tmp / "reviews.json").unlink()
    assert _refresh(runtime, _session(["node_load1"])).status == rt.PUBLISHED
    assert runtime.catalog() is not None


# ---- reconciled publish ------------------------------------------------------------


def test_refresh_publishes_reconciled_statuses_and_pending_discoveries_stay_pending(env):
    tmp, settings = env
    _write_reviews(tmp, [_approval(), _rejection("node_load5")])
    runtime = rt.CatalogRuntime(settings)
    runtime.catalog()  # static first
    out = _refresh(runtime, _session(
        ["node_load1", "node_load5", "acme_new"],  # acme_ok absent
        {"acme_new": [{"type": "counter", "help": "n"}]}))
    assert out.status == rt.PUBLISHED
    cat = runtime.catalog()
    assert _status(cat, "node_load1") == "approved"
    assert _status(cat, "node_load5") == "rejected"
    assert _status(cat, "acme_ok") == "approved_unavailable"
    assert _status(cat, "acme_new") == "discovered_pending_review"
    assert runtime.snapshot().source == "reconciled"
    assert cat.get("node_load1").priority in {p.value for p in Priority}  # enrichment still applied


def test_refresh_without_a_prior_static_build_still_publishes(env):
    _, settings = env
    runtime = rt.CatalogRuntime(settings)
    assert _refresh(runtime, _session(["node_load1"])).status == rt.PUBLISHED
    assert _status(runtime.catalog(), "node_load5") == "approved_unavailable"


def test_successful_but_empty_discovery_publishes_all_unavailable_it_is_not_a_failure(env):
    _, settings = env
    runtime = rt.CatalogRuntime(settings)
    out = _refresh(runtime, _session([]))
    assert out.status == rt.PUBLISHED and out.report.runtime_name_count == 0
    assert {m.status for m in runtime.catalog().metrics} == {"approved_unavailable"}


@pytest.mark.parametrize("fail", [requests.exceptions.ConnectionError("refused"), requests.exceptions.Timeout()])
def test_discovery_failure_keeps_the_last_good_snapshot_and_never_reads_as_empty(env, fail):
    _, settings = env
    runtime = rt.CatalogRuntime(settings)
    _refresh(runtime, _session(["node_load1", "node_load5"]))
    good = runtime.snapshot()
    out = _refresh(runtime, _session([], fail=fail))
    assert out.status == rt.DISCOVERY_FAILED
    assert runtime.snapshot() is good
    assert _status(runtime.catalog(), "node_load1") == "approved"


def test_discovery_failure_with_no_prior_reconciled_snapshot_leaves_the_static_one(env):
    _, settings = env
    runtime = rt.CatalogRuntime(settings)
    static = runtime.snapshot()
    assert _refresh(runtime, _session([], fail=requests.exceptions.ConnectionError())).status == rt.DISCOVERY_FAILED
    assert runtime.snapshot() is static and static.source == "static"


def test_malformed_review_store_on_refresh_keeps_the_last_good_snapshot(env):
    tmp, settings = env
    runtime = rt.CatalogRuntime(settings)
    _refresh(runtime, _session(["node_load1"]))
    good = runtime.snapshot()
    (tmp / "reviews.json").write_text('{"schema_version": 1, "decisions": [{"name": "x"}]}')
    out = _refresh(runtime, _session(["node_load1"]))
    assert out.status == rt.INPUTS_INVALID
    assert runtime.snapshot() is good


def test_conflicting_review_records_on_refresh_fail_closed(env):
    tmp, settings = env
    runtime = rt.CatalogRuntime(settings)
    _refresh(runtime, _session(["node_load1"]))
    good = runtime.snapshot()
    _write_reviews(tmp, [_rejection("node_load1"), _approval("node_load1")])
    assert _refresh(runtime, _session(["node_load1"])).status == rt.INPUTS_INVALID
    assert runtime.snapshot() is good


def test_unexpected_error_during_refresh_keeps_the_snapshot_and_releases_the_lock(env):
    _, settings = env
    runtime = rt.CatalogRuntime(settings)
    _refresh(runtime, _session(["node_load1"]))
    good = runtime.snapshot()
    with patch("app.catalog.runtime.reconcile_inputs", side_effect=RuntimeError("boom")):
        assert runtime.refresh().status == rt.ERROR
    assert runtime.snapshot() is good
    assert _refresh(runtime, _session(["node_load1"])).status == rt.PUBLISHED  # lock was released


def test_invalid_scope_pattern_is_an_inputs_failure_not_a_crash(env):
    _, settings = env
    bad = settings.model_copy(update={"catalog_discovery_exclude_patterns": [" "]})
    runtime = rt.CatalogRuntime(bad)
    assert _refresh(runtime, _session([])).status == rt.INPUTS_INVALID


def test_rejection_and_approval_changes_take_effect_on_the_next_refresh_not_before(env):
    tmp, settings = env
    runtime = rt.CatalogRuntime(settings)
    meta = {"acme_ok": [{"type": "gauge", "help": "ok"}]}
    names = ["node_load1", "node_load5", "acme_ok"]
    _refresh(runtime, _session(names, meta))
    assert _status(runtime.catalog(), "acme_ok") == "discovered_pending_review"
    _write_reviews(tmp, [_approval(), _rejection("node_load5")])
    assert _status(runtime.catalog(), "acme_ok") == "discovered_pending_review"  # unchanged until refresh
    _refresh(runtime, _session(names, meta))
    assert _status(runtime.catalog(), "acme_ok") == "approved"
    assert _status(runtime.catalog(), "node_load5") == "rejected"
    _write_reviews(tmp, [_approval()])  # `clear` the rejection
    _refresh(runtime, _session(names, meta))
    assert _status(runtime.catalog(), "node_load5") == "approved"


def test_a_static_refresh_never_replaces_a_reconciled_snapshot(env):
    _, settings = env
    runtime = rt.CatalogRuntime(settings)
    _refresh(runtime, _session(["node_load1"]))
    reconciled = runtime.snapshot()
    assert runtime.refresh(discover=False).status == rt.BUSY
    assert runtime.snapshot() is reconciled


def test_request_refresh_without_a_worker_rereads_decisions_without_network(env):
    tmp, settings = env
    runtime = rt.CatalogRuntime(settings)
    assert _status(runtime.catalog(), "node_load5") == "approved"
    _write_reviews(tmp, [_rejection("node_load5")])
    with patch.object(rc, "_get_session", side_effect=AssertionError("no network")):
        assert runtime.request_refresh().status == rt.PUBLISHED
    assert _status(runtime.catalog(), "node_load5") == "rejected"


# ---- concurrency ----------------------------------------------------------------------


def test_overlapping_refresh_is_refused_and_readers_keep_the_old_snapshot_meanwhile(env):
    _, settings = env
    runtime = rt.CatalogRuntime(settings)
    _refresh(runtime, _session(["node_load1", "node_load5"]))
    before = runtime.snapshot()
    entered, release = threading.Event(), threading.Event()
    real = rt.reconcile_inputs

    def slow(*a, **kw):
        entered.set()
        assert release.wait(5)
        return real(*a, **kw)

    results = {}
    with patch.object(rc, "_get_session", return_value=_session(["node_load1"])), \
         patch("app.catalog.runtime.reconcile_inputs", side_effect=slow):
        t = threading.Thread(target=lambda: results.setdefault("first", runtime.refresh()))
        t.start()
        assert entered.wait(5)
        assert runtime.refresh().status == rt.BUSY                 # second refresh refused, immediately
        assert runtime.snapshot() is before                        # reader sees the complete old state
        assert _status(runtime.catalog(), "node_load5") == "approved"
        release.set()
        t.join(5)
    assert results["first"].status == rt.PUBLISHED
    after = runtime.snapshot()
    assert after is not before and _status(after.catalog, "node_load5") == "approved_unavailable"


def test_readers_never_observe_a_mixed_catalog_while_refreshes_publish(env):
    _, settings = env
    runtime = rt.CatalogRuntime(settings)
    runtime.snapshot()
    stop, bad = threading.Event(), []

    def reader():
        while not stop.is_set():
            cat = runtime.catalog()
            if cat is None or {m.name for m in cat.metrics} != {"node_load1", "node_load5"}:
                bad.append(cat)
            # within one reference, both entries are from the same publish
            if len({m.status for m in cat.metrics}) > 1 and {m.status for m in cat.metrics} != {"approved", "approved_unavailable"}:
                bad.append(cat)

    t = threading.Thread(target=reader)
    t.start()
    for i in range(30):
        _refresh(runtime, _session(["node_load1", "node_load5"] if i % 2 else []))
    stop.set()
    t.join(5)
    assert not bad
    # Each published snapshot is internally uniform (both present or both absent).
    snap = runtime.snapshot()
    assert len({m.status for m in snap.catalog.metrics}) == 1


# ---- worker ----------------------------------------------------------------------------


def _worker_settings(settings, seconds=0.05):
    return settings.model_copy(update={
        "catalog_runtime_reconciliation_enabled": True, "catalog_runtime_refresh_seconds": seconds})


def test_worker_publishes_static_first_refreshes_repeatedly_and_stops_cleanly(env):
    _, settings = env
    runtime = rt.CatalogRuntime(_worker_settings(settings))
    ticks, enough = [], threading.Event()

    def fake_refresh(discover=True):
        ticks.append(discover)
        if len(ticks) >= 3:
            enough.set()
        return rt.RefreshOutcome(rt.PUBLISHED)

    with patch.object(runtime, "refresh", side_effect=fake_refresh):
        runtime.start_worker()
        assert runtime.has_snapshot()                      # static snapshot is there before any discovery
        assert enough.wait(5)
        runtime.stop_worker()
    assert not runtime.worker_running and all(ticks)


def test_worker_survives_a_failing_refresh_iteration(env):
    _, settings = env
    runtime = rt.CatalogRuntime(_worker_settings(settings))
    calls, enough = [], threading.Event()

    def flaky(discover=True):
        calls.append(1)
        if len(calls) >= 3:
            enough.set()
        raise RuntimeError("boom")

    with patch.object(runtime, "refresh", side_effect=flaky):
        runtime.start_worker()
        assert enough.wait(5)
        assert runtime.worker_running
        runtime.stop_worker()
    assert not runtime.worker_running


def test_start_worker_is_idempotent_and_request_refresh_wakes_it_without_blocking(env):
    _, settings = env
    runtime = rt.CatalogRuntime(_worker_settings(settings, seconds=3600))
    seen, second = [], threading.Event()

    def fake_refresh(discover=True):
        seen.append(1)
        if len(seen) == 2:
            second.set()
        return rt.RefreshOutcome(rt.PUBLISHED)

    with patch.object(runtime, "refresh", side_effect=fake_refresh):
        runtime.start_worker()
        thread = runtime._thread
        runtime.start_worker()
        assert runtime._thread is thread
        assert runtime.request_refresh() is None           # woke the worker; did not discover inline
        assert second.wait(5)                              # interval is an hour: only the wake can explain it
        runtime.stop_worker()


# ---- process-wide instance -------------------------------------------------------------------


def test_get_runtime_is_a_singleton_but_replaces_a_runtime_that_never_built(env, tmp_path):
    tmp, settings = env
    first = rt.get_runtime(settings)
    assert rt.get_runtime(settings) is first
    broken = settings.model_copy(update={"catalog_path": tmp_path / "missing.json"})
    r1 = rt.get_runtime(broken) if first.has_snapshot() is False else None
    # `first` has not built yet, so different settings replace it
    assert r1 is not first
    assert r1.catalog() is None
    assert rt.get_runtime(settings) is not r1               # failed runtime is replaced again


def test_get_runtime_keeps_a_working_runtime_even_if_settings_change(env, tmp_path):
    _, settings = env
    runtime = rt.get_runtime(settings)
    assert runtime.catalog() is not None
    other = settings.model_copy(update={"catalog_path": tmp_path / "other.json"})
    assert rt.get_runtime(other) is runtime


# ---- server lifespan ----------------------------------------------------------------------------


def _lifespan_run(settings):
    import asyncio
    import run_server

    async def go():
        async with run_server._catalog_runtime_lifespan(None):
            return rt._runtime

    with patch("run_server.get_settings", return_value=settings):
        return asyncio.run(go())


def test_lifespan_does_nothing_when_the_flag_is_off(env):
    _, settings = env
    assert _lifespan_run(settings) is None


def test_lifespan_starts_and_stops_the_worker_when_enabled(env):
    _, settings = env
    enabled = _worker_settings(settings, seconds=3600)
    started = threading.Event()
    with patch.object(rt.CatalogRuntime, "refresh", side_effect=lambda discover=True: started.set() or rt.RefreshOutcome(rt.PUBLISHED)):
        import asyncio
        import run_server

        async def go():
            async with run_server._catalog_runtime_lifespan(None):
                assert started.wait(5)
                runtime = rt._runtime
                assert runtime.worker_running
                return runtime

        with patch("run_server.get_settings", return_value=enabled):
            runtime = asyncio.run(go())
    assert not runtime.worker_running


def test_lifespan_survives_a_worker_start_failure(env):
    _, settings = env
    enabled = _worker_settings(settings)
    with patch.object(rt.CatalogRuntime, "start_worker", side_effect=RuntimeError("no threads")):
        assert _lifespan_run(enabled) is not None  # served anyway


# ---- enrichment degradation ------------------------------------------------------------------


def test_unparseable_markdown_degrades_to_no_enrichment_not_to_no_catalog(env):
    # The fixture's skills tree has no metric definitions in its domain
    # Markdown, which makes build_markdown_category_index raise. The catalog
    # must still be published, just without the Markdown vocabulary.
    from app.catalog.generator import CatalogGenerationError, build_markdown_category_index
    _, settings = env
    with pytest.raises(CatalogGenerationError):
        build_markdown_category_index(settings.skills_root)
    catalog = rt.CatalogRuntime(settings).catalog()
    assert catalog is not None and len(catalog.metrics) == 2
