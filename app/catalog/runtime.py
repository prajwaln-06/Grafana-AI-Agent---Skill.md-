"""
app/catalog/runtime.py

The runtime catalog: the ONE place the effective catalog is built, cached and
refreshed.

    vendor catalog.json + review decisions (reviews.json) + live Prometheus
                                  |
                              reconciler
                                  |
                  rules.apply_rules (keywords/priority, in memory)
                                  |
                       published Snapshot (immutable)
                                  |
        pipeline._get_catalog  /  SkillIndex.metric_directory

Both request-path consumers read this snapshot, so they cannot disagree.

---- Snapshot lifecycle

  * First use (or worker start) publishes a STATIC snapshot synchronously:
    vendor entries + approved review records, with rejections applied and no
    network. Static means "no availability information", the same position
    every deployment was in before reconciliation existed (vendor metrics are
    `approved`), so requests never wait for discovery and a pending discovery
    can never appear in it.
  * When Settings.catalog_runtime_reconciliation_enabled is true, a background
    thread additionally calls refresh() at startup and every
    Settings.catalog_runtime_refresh_seconds: live discovery, reconcile(),
    enrichment, then one atomic publish.
  * refresh() either publishes a complete, valid snapshot or changes nothing.
    A discovery failure (never read as "empty inventory"), an unreadable/
    malformed vendor catalog or review store, or any unexpected error keeps
    the last good snapshot and is logged. Overlapping refreshes are refused
    (the second returns BUSY immediately). Readers take one reference to an
    immutable Snapshot, so a refresh in progress is invisible to them.
  * If the very first static build fails (bad catalog.json / reviews.json),
    catalog() returns None -- the pipeline's documented "no catalog" degradation
    -- and does not retry per request; a later successful refresh recovers.

---- Why an in-process thread (trade-offs)

The service is one Uvicorn process (run_server.py, Dockerfile). A thread needs
no new moving part and publishes straight to the memory readers use. A
separate scheduled job would have to hand its result over through a file or
service and have the server reload it -- more machinery, plus a second place
for state to disagree. The costs accepted here: with several worker processes
each would discover independently (N times the Prometheus calls, snapshots that
can differ for up to one interval), and discovery stops if the process is
stopped. Decisions made with scripts/review_catalog.py are read at the next
refresh, or immediately via request_refresh() (the admin reload endpoint).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from app.catalog import rules as catalog_rules
from app.catalog.generator import CatalogGenerationError, build_markdown_category_index
from app.catalog.loader import CatalogLoadError, load_catalog
from app.catalog.reconciler import (
    DiscoveryScope,
    ReconciliationReport,
    ReconciliationSkipped,
    run_reconciliation,
)
from app.catalog.reviews import ReviewError, ReviewStore, build_approved_universe, load_reviews
from app.catalog.schema import Catalog, CatalogStatus

logger = logging.getLogger(__name__)

PUBLISHED = "published"
BUSY = "busy"
DISCOVERY_FAILED = "discovery_failed"
INPUTS_INVALID = "inputs_invalid"
ERROR = "error"

_MAX_LOGGED_NAMES = 20


# ---- loading the on-disk inputs (shared with the CLIs) -----------------------


@dataclass(frozen=True)
class CatalogInputs:
    """The two on-disk inputs, loaded and validated together.

    `universe` is the approved metric universe (vendor entries + approved
    review records, vendor winning on a duplicate name); `superseded` lists
    approval records a vendor entry overrode (kept on disk, never applied).
    """

    vendor_catalog: Catalog
    store: ReviewStore
    universe: Catalog
    superseded: tuple[str, ...]

    @property
    def vendor_names(self) -> frozenset[str]:
        return frozenset(e.name for e in self.vendor_catalog.metrics)


def load_inputs(catalog_path: Path, reviews_path: Path) -> CatalogInputs:
    """Raises CatalogLoadError (bad vendor catalog) or ReviewError (bad review
    store) -- never returns a partial result."""
    vendor = load_catalog(catalog_path)
    store = load_reviews(reviews_path)
    universe, superseded = build_approved_universe(vendor, store)
    if superseded:
        logger.warning(
            "Approval record(s) %s are superseded by vendor catalog entries of the "
            "same name; the vendor definition is used. The records remain in the "
            "review store and can be removed with `review_catalog clear`.",
            list(superseded),
        )
    return CatalogInputs(vendor, store, universe, superseded)


def reconcile_inputs(
    inputs: CatalogInputs,
    base_url: str,
    timeout: float,
    *,
    scope: DiscoveryScope | None = None,
    previous_catalog: Catalog | None = None,
) -> ReconciliationReport:
    """Live discovery against `base_url`, reconciled with the approved
    universe and the store's rejections. Raises ReconciliationSkipped if
    discovery fails (see reconciler.run_reconciliation)."""
    return run_reconciliation(
        inputs.universe,
        base_url,
        timeout=timeout,
        previous_catalog=previous_catalog,
        discovery_scope=scope,
        rejections=inputs.store.rejections(),
    )


def apply_static_decisions(inputs: CatalogInputs) -> Catalog:
    """The effective catalog with NO live discovery: the approved universe with
    every explicit rejection applied. No availability information, nothing
    pending (pending status can only come from discovery)."""
    rejected = inputs.store.rejections().keys()
    rejected_status = CatalogStatus.REJECTED.value
    return Catalog(
        catalog_version=inputs.universe.catalog_version,
        generated_at=inputs.universe.generated_at,
        metrics=tuple(
            replace(e, status=rejected_status) if e.name in rejected else e
            for e in inputs.universe.metrics
        ),
    )


# ---- the stateful runtime -----------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    catalog: Catalog  # enriched (keywords/priority); what the pipeline consumes
    published_at: str
    source: str  # "static" | "reconciled"
    report: ReconciliationReport | None = None


@dataclass(frozen=True)
class RefreshOutcome:
    status: str
    detail: str = ""
    report: ReconciliationReport | None = None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _names(names, limit: int = _MAX_LOGGED_NAMES) -> str:
    ordered = sorted(names)
    suffix = f" (+{len(ordered) - limit} more)" if len(ordered) > limit else ""
    return ", ".join(ordered[:limit]) + suffix


class CatalogRuntime:
    def __init__(self, settings) -> None:
        self._settings = settings
        self.key = self.source_key(settings)
        self._snapshot: Snapshot | None = None
        self._init_failed = False
        self._state_lock = threading.Lock()  # guards _snapshot/_init_failed, held only briefly
        self._refresh_lock = threading.Lock()  # refused (not awaited) when contended
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def source_key(settings) -> tuple:
        return (str(settings.catalog_path), str(settings.catalog_reviews_path), str(settings.skills_root))

    # ---- reading (request path: never blocks on I/O after the first build) ----

    def snapshot(self) -> Snapshot | None:
        snap = self._snapshot  # one atomic read; Snapshot is immutable
        if snap is not None or self._init_failed:
            return snap
        built = self._build_static_snapshot()
        with self._state_lock:
            if self._snapshot is None and built is not None:
                self._snapshot = built
            elif built is None and self._snapshot is None:
                self._init_failed = True
            return self._snapshot

    def catalog(self) -> Catalog | None:
        snap = self.snapshot()
        return snap.catalog if snap is not None else None

    def has_snapshot(self) -> bool:
        return self._snapshot is not None

    # ---- building ---------------------------------------------------------------

    def _enrich(self, catalog: Catalog) -> Catalog:
        # The same load-time enrichment pipeline._get_catalog always applied:
        # keywords/priority rules plus Markdown-category keyword vocabulary.
        # The Markdown vocabulary is additive: if the skill package's Markdown
        # cannot be parsed, publish the catalog WITHOUT it (logged) rather than
        # lose the whole catalog -- the "no enrichment, not a crash" degradation
        # pipeline._get_catalog documented but did not actually guarantee.
        try:
            index = build_markdown_category_index(self._settings.skills_root)
        except (CatalogGenerationError, OSError) as e:
            logger.warning("Markdown category enrichment skipped: %s", e)
            index = {}
        return catalog_rules.apply_rules(catalog, markdown_category_index=index)

    def _build_static_snapshot(self) -> Snapshot | None:
        try:
            inputs = load_inputs(self._settings.catalog_path, self._settings.catalog_reviews_path)
            return Snapshot(self._enrich(apply_static_decisions(inputs)), _now(), "static")
        except (CatalogLoadError, ReviewError, ValueError, OSError) as e:
            logger.warning(
                "Catalog integration (Router narrowing / Generator candidates) disabled: "
                "failed to build the catalog from %s and %s: %s",
                self._settings.catalog_path, self._settings.catalog_reviews_path, e,
            )
            return None
        except Exception:
            logger.exception("Unexpected error building the initial catalog snapshot")
            return None

    def _publish(self, snapshot: Snapshot) -> None:
        with self._state_lock:
            self._snapshot = snapshot
            self._init_failed = False

    # ---- refreshing ---------------------------------------------------------------

    def refresh(self, *, discover: bool = True) -> RefreshOutcome:
        """Builds and publishes a new snapshot, or changes nothing. With
        discover=False only the on-disk inputs are re-read (no Prometheus):
        that is a static snapshot, so it is refused while a reconciled one is
        published -- it would silently throw the availability data away."""
        if not self._refresh_lock.acquire(blocking=False):
            logger.info("Catalog refresh skipped: another refresh is still running.")
            return RefreshOutcome(BUSY, "another refresh is in progress")
        try:
            return self._refresh_locked(discover)
        except Exception as e:  # never let a refresh take down its caller
            logger.exception("Catalog refresh failed unexpectedly; keeping the previous snapshot.")
            return RefreshOutcome(ERROR, f"{type(e).__name__}: {e}")
        finally:
            self._refresh_lock.release()

    def _refresh_locked(self, discover: bool) -> RefreshOutcome:
        previous = self._snapshot
        try:
            inputs = load_inputs(self._settings.catalog_path, self._settings.catalog_reviews_path)
            scope = DiscoveryScope.from_settings(self._settings)
        except (CatalogLoadError, ReviewError, ValueError, OSError) as e:
            logger.error(
                "Catalog refresh failed: inputs are unreadable or invalid (%s). %s",
                e, "Keeping the previous snapshot." if previous else "No snapshot is published.",
            )
            return RefreshOutcome(INPUTS_INVALID, str(e))

        if not discover:
            if previous is not None and previous.source == "reconciled":
                return RefreshOutcome(BUSY, "refusing to replace a reconciled snapshot with a static one")
            self._publish(Snapshot(self._enrich(apply_static_decisions(inputs)), _now(), "static"))
            return RefreshOutcome(PUBLISHED, "static")

        try:
            report = reconcile_inputs(
                inputs,
                self._settings.prometheus_url,
                self._settings.prometheus_timeout_seconds,
                scope=scope,
                previous_catalog=previous.catalog if previous else None,
            )
        except ReconciliationSkipped as e:
            logger.warning("Catalog refresh: %s Keeping the previous snapshot.", e)
            return RefreshOutcome(DISCOVERY_FAILED, str(e))

        snapshot = Snapshot(self._enrich(report.catalog), _now(), "reconciled", report)
        self._publish(snapshot)
        self._log_transitions(previous, snapshot)
        return RefreshOutcome(PUBLISHED, "reconciled", report)

    def _log_transitions(self, previous: Snapshot | None, current: Snapshot) -> None:
        report = current.report
        was_pending = set()
        if previous is not None:
            was_pending = {m.name for m in previous.catalog.by_status(CatalogStatus.DISCOVERED_PENDING_REVIEW.value)}
        newly_pending = set(report.newly_discovered) - was_pending
        counts = {s.value: len(current.catalog.by_status(s.value)) for s in CatalogStatus}
        logger.info(
            "Catalog snapshot published: %d runtime names, statuses=%s, %d out of scope, "
            "%d with undetermined type.",
            report.runtime_name_count, counts, len(report.out_of_scope_skipped),
            len(report.undetermined_type_skipped),
        )
        if newly_pending:
            logger.info("Catalog: %d new metric(s) pending review: %s",
                        len(newly_pending), _names(newly_pending))
        if report.became_unavailable:
            logger.warning("Catalog: approved metric(s) became unavailable: %s",
                           _names(report.became_unavailable))
        if report.became_available_again:
            logger.info("Catalog: approved metric(s) available again: %s",
                        _names(report.became_available_again))

    # ---- background worker --------------------------------------------------------

    @property
    def worker_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_worker(self) -> None:
        """Publishes the static snapshot (synchronously, so requests have a
        catalog immediately) then starts the refresh thread. Idempotent."""
        if self.worker_running:
            return
        self.snapshot()
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._worker_loop, name="catalog-runtime-refresh", daemon=True
        )
        self._thread.start()

    def stop_worker(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("Catalog refresh worker did not stop within %.1fs.", timeout)
        self._thread = None

    def request_refresh(self) -> RefreshOutcome | None:
        """Make new review decisions visible now. With the worker running this
        only wakes it (discovery never runs on the caller's thread) and
        returns None; otherwise it re-reads the on-disk inputs synchronously
        -- a few small file reads, no network."""
        if self.worker_running:
            self._wake.set()
            return None
        return self.refresh(discover=False)

    def _worker_loop(self) -> None:
        interval = float(self._settings.catalog_runtime_refresh_seconds)
        logger.info("Catalog refresh worker started (interval=%.0fs).", interval)
        while not self._stop.is_set():
            try:
                self.refresh(discover=True)
            except Exception:  # refresh() already guards; this keeps the loop alive regardless
                logger.exception("Catalog refresh worker iteration failed.")
            self._wake.wait(timeout=interval)
            self._wake.clear()
        logger.info("Catalog refresh worker stopped.")


# ---- process-wide instance ------------------------------------------------------

_runtime: CatalogRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime(settings) -> CatalogRuntime:
    """The process-wide CatalogRuntime, created on first use. Like the cache it
    replaces, a later call with different paths keeps the existing runtime --
    unless that runtime never managed to build a snapshot, in which case a
    runtime for the new settings replaces it."""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = CatalogRuntime(settings)
        elif _runtime.key != CatalogRuntime.source_key(settings) and not _runtime.has_snapshot():
            _runtime.stop_worker()
            _runtime = CatalogRuntime(settings)
        return _runtime


def reset_runtime() -> None:
    """Stops the worker and discards the runtime and its snapshot. For tests
    and process shutdown; ordinary operation uses request_refresh() instead."""
    global _runtime
    with _runtime_lock:
        runtime, _runtime = _runtime, None
    if runtime is not None:
        runtime.stop_worker()
