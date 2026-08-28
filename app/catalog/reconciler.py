"""
app/catalog/reconciler.py

Phase 4: reconciles a vendor-approved candidate catalog against a LIVE
Prometheus instance's runtime-discovered metric names, per the frozen
hybrid source-of-truth model:

    Vendor/reference universe  +  Prometheus runtime discovery  ->  Catalog

Neither side is absolute truth. This module's only job is the arrow on
the right: given *a* Catalog of candidate vendor-approved entries (see
"Decoupling from Phase 2" below) and what Prometheus actually reports
right now, decide each metric's `status` among the four frozen values
(approved / approved_unavailable / discovered_pending_review / rejected)
-- never inventing a fifth, and never silently promoting a
runtime-discovered unknown metric to "approved".

---- Decoupling from Phase 2 (explicit, per architectural clarification)

`reconcile()` takes `vendor_catalog: Catalog` -- a plain Catalog object --
and has no idea, and must never need to know, how it was produced. Today
that Catalog happens to come from generator.py's Markdown-parsing (Phase
2), because that is the correct BOOTSTRAP mechanism for the 43 metrics
this project already had fully written, hand-authored Markdown for.

That is explicitly NOT meant to be the permanent way every future metric
enters the catalog. Requiring a full "### `metric_name`" Markdown section
with Category/Purpose/Type bullets for every new metric before it can be
catalogued would defeat the point of the hybrid model -- the whole reason
`discovered_pending_review` exists as a status is to let a metric
Prometheus is already exposing enter the catalog on minimal, runtime-
sourced metadata (name + Prometheus's own reported type/HELP, see
discover_runtime_metadata() below), pending a human's review, WITHOUT
anyone first writing Markdown for it.

So, concretely, two independent paths feed a metric into the catalog:

  1. Markdown-documented, vendor-approved metrics (today's 43) --
     generator.py (Phase 2) -> vendor_catalog -> reconcile() confirms/
     denies availability against runtime discovery.
  2. Metrics Prometheus is exposing that are NOT yet in the vendor-
     approved catalog at all -- reconcile() itself builds a minimal entry
     for these directly from runtime discovery (name, type, help), status
     `discovered_pending_review`, category `UNCATEGORIZED`, no Markdown
     required. A human reviewing that status is what may eventually
     promote it to "approved" (by adding it to the vendor-approved side,
     however that side is produced by then -- generator.py, a future
     lightweight vendor manifest, or direct curation) or set it to
     "rejected" -- reconcile() itself never performs that promotion.

Phase 2's Markdown generator therefore remains the *migration* path for
already-documented metrics; path 2 is the intended *ongoing* onboarding
path for new metrics going forward, and this module is written so that
replacing path 1's source (e.g. with a lightweight vendor manifest that
carries only name/type/help/category without Level-2 semantic Markdown)
requires zero changes here -- reconcile() only ever consumes a Catalog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from app.catalog.schema import (
    UNCATEGORIZED,
    Catalog,
    CatalogEntry,
    CatalogStatus,
    MetricType,
    Priority,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15
_VALID_TYPES = {t.value for t in MetricType}
_UNKNOWN_EXPORTER = "unknown"

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


# ---- runtime discovery ---------------------------------------------------
# Both functions follow prometheus_client.py / label_discovery.py's own
# established convention exactly: return None (never an empty
# collection) on any connection/parse failure, so a caller can never
# mistake "discovery failed" for "confirmed, nothing is there."


def discover_runtime_metric_names(
    base_url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> set[str] | None:
    """GET /api/v1/label/__name__/values -- every metric name Prometheus
    currently exposes across all scrape targets. This is the runtime half
    of the hybrid model; the vendor-approved half is `vendor_catalog`,
    supplied by the caller of reconcile()."""
    url = base_url.rstrip("/") + "/api/v1/label/__name__/values"
    try:
        resp = _get_session().get(url, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except (requests.exceptions.RequestException, ValueError):
        return None
    if body.get("status") != "success":
        return None
    data = body.get("data")
    if not isinstance(data, list):
        return None
    return set(data)


@dataclass(frozen=True)
class RuntimeMetricInfo:
    """Prometheus's own reported type/HELP for one metric name, from
    /api/v1/metadata -- exactly what lets a discovered_pending_review
    entry (see module docstring, path 2) get real metadata without a
    human writing anything."""

    type: str | None = None
    help: str | None = None


def discover_runtime_metadata(
    base_url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, RuntimeMetricInfo] | None:
    """GET /api/v1/metadata -- Prometheus's own type + HELP text per metric
    name, when the running Prometheus/exporters expose it (most exporters,
    including node_exporter and dcgm-exporter, do). Returns None (never
    {}) on any connection/parse failure. If a metric name has metadata
    from more than one target, the first entry is used -- in practice
    every target scraping the same exporter reports the same type/HELP for
    a given metric name, so this is a stable choice, not an arbitrary one.
    """
    url = base_url.rstrip("/") + "/api/v1/metadata"
    try:
        resp = _get_session().get(url, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except (requests.exceptions.RequestException, ValueError):
        return None
    if body.get("status") != "success":
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None

    result: dict[str, RuntimeMetricInfo] = {}
    for name, entries in data.items():
        if not entries:
            continue
        first = entries[0] if isinstance(entries, list) else entries
        if not isinstance(first, dict):
            continue
        result[name] = RuntimeMetricInfo(type=first.get("type"), help=first.get("help"))
    return result


# ---- reconciliation -------------------------------------------------------


class ReconciliationSkipped(Exception):
    """Raised instead of returning a corrupted Catalog when runtime
    discovery itself failed (discover_runtime_metric_names returned None).
    A failed discovery must NEVER be interpreted as "nothing exists at
    runtime" -- doing so would silently flip every approved vendor metric
    to approved_unavailable on a mere Prometheus hiccup. Mirrors the same
    fail-without-guessing principle label_discovery.py and
    prometheus_client.py already apply. Callers should catch this and keep
    the previous catalog in place -- the same "don't tear down a working
    state for a broken read" behavior CatalogIndex.reload() already gives
    a bad catalog.json edit.
    """


@dataclass(frozen=True)
class ReconciliationReport:
    """The reconciled Catalog, plus what changed and what reconcile()
    deliberately declined to decide -- surfaced explicitly so a human
    reviewer (see the reference doc's "Manual Testing Checkpoints",
    After Batch 2: "Start manual inspection of... Verify candidates make
    semantic sense") sees both, not just the resulting catalog.json.
    """

    catalog: Catalog
    newly_discovered: tuple[str, ...] = field(default_factory=tuple)
    became_unavailable: tuple[str, ...] = field(default_factory=tuple)
    became_available_again: tuple[str, ...] = field(default_factory=tuple)
    kept_rejected: tuple[str, ...] = field(default_factory=tuple)
    undetermined_type_skipped: tuple[str, ...] = field(default_factory=tuple)


def reconcile(
    vendor_catalog: Catalog,
    runtime_names: set[str] | None,
    runtime_metadata: dict[str, RuntimeMetricInfo] | None = None,
    previous_catalog: Catalog | None = None,
    generated_at: str | None = None,
) -> ReconciliationReport:
    """Reconciles `vendor_catalog` (see module docstring: the intended,
    approved-universe side of the hybrid model, produced however the
    caller likes) against `runtime_names` (Prometheus's own, right-now
    metric-name universe).

    `previous_catalog`, if given, is read for exactly one purpose: to
    preserve a human's explicit prior "rejected" curation. A metric a
    human has rejected stays rejected even if it reappears at runtime or
    in a refreshed vendor catalog -- otherwise "rejected" would be a
    decision that silently undoes itself on the very next reconciliation
    run, which would make manual curation pointless.

    Status decisions:
      - vendor-approved metric, previously rejected            -> rejected (preserved)
      - vendor-approved metric, present at runtime              -> approved
      - vendor-approved metric, absent at runtime                -> approved_unavailable
      - runtime-only metric (not in vendor_catalog at all),
        previously rejected                                     -> rejected (preserved)
      - runtime-only metric, type resolvable from runtime
        metadata, never rejected                                 -> discovered_pending_review
      - runtime-only metric, type NOT resolvable                  -> excluded from the
                                                                       output catalog and
                                                                       listed in
                                                                       `undetermined_type_skipped`
                                                                       rather than guessed

    Category for any newly-created (path 2) entry is always
    `UNCATEGORIZED` and priority is always `Priority.REVIEW` -- Phase 5
    (rules.py) is what may refine these, never this module.

    Raises ReconciliationSkipped, touching nothing, if `runtime_names` is
    None.
    """
    if runtime_names is None:
        raise ReconciliationSkipped(
            "Runtime metric-name discovery failed or was not attempted; "
            "refusing to reconcile rather than silently guessing every "
            "vendor-approved metric is now unavailable. Keep the previous "
            "catalog in place and retry discovery."
        )

    previous_status: dict[str, str] = {}
    if previous_catalog is not None:
        previous_status = {m.name: m.status for m in previous_catalog.metrics}

    runtime_metadata = runtime_metadata or {}

    reconciled: list[CatalogEntry] = []
    newly_discovered: list[str] = []
    became_unavailable: list[str] = []
    became_available_again: list[str] = []
    kept_rejected: list[str] = []
    undetermined_type_skipped: list[str] = []

    # ---- vendor-approved side ------------------------------------------
    for entry in vendor_catalog.metrics:
        prior = previous_status.get(entry.name)
        present = entry.name in runtime_names

        if prior == CatalogStatus.REJECTED.value:
            new_status = CatalogStatus.REJECTED.value
            kept_rejected.append(entry.name)
        elif present:
            new_status = CatalogStatus.APPROVED.value
            if prior == CatalogStatus.APPROVED_UNAVAILABLE.value:
                became_available_again.append(entry.name)
        else:
            new_status = CatalogStatus.APPROVED_UNAVAILABLE.value
            if prior != CatalogStatus.APPROVED_UNAVAILABLE.value:
                became_unavailable.append(entry.name)

        reconciled.append(
            entry if entry.status == new_status else _with_status(entry, new_status)
        )

    vendor_names = {e.name for e in vendor_catalog.metrics}

    # ---- runtime-only side: discovered_pending_review (path 2) ---------
    for name in sorted(runtime_names - vendor_names):
        if previous_status.get(name) == CatalogStatus.REJECTED.value:
            kept_rejected.append(name)
            reconciled.append(
                _minimal_entry(
                    name,
                    runtime_metadata.get(name),
                    status=CatalogStatus.REJECTED.value,
                    fallback_type=_prior_type(previous_catalog, name),
                )
            )
            continue

        info = runtime_metadata.get(name)
        raw_type = (info.type if info else None) or ""
        if raw_type not in _VALID_TYPES:
            undetermined_type_skipped.append(name)
            logger.info(
                "Skipping runtime-discovered metric %r: Prometheus metadata "
                "did not report a type in %s (got %r). Not adding it as "
                "discovered_pending_review without a valid type rather "
                "than guessing one.",
                name,
                sorted(_VALID_TYPES),
                raw_type or None,
            )
            continue

        reconciled.append(
            _minimal_entry(
                name, info, status=CatalogStatus.DISCOVERED_PENDING_REVIEW.value
            )
        )
        newly_discovered.append(name)

    if generated_at is None:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    catalog = Catalog(
        catalog_version=vendor_catalog.catalog_version,
        generated_at=generated_at,
        metrics=tuple(reconciled),
    )
    return ReconciliationReport(
        catalog=catalog,
        newly_discovered=tuple(newly_discovered),
        became_unavailable=tuple(became_unavailable),
        became_available_again=tuple(became_available_again),
        kept_rejected=tuple(kept_rejected),
        undetermined_type_skipped=tuple(undetermined_type_skipped),
    )


def _with_status(entry: CatalogEntry, new_status: str) -> CatalogEntry:
    """Returns a copy of `entry` with only `status` changed -- CatalogEntry
    is frozen, so reconciliation cannot mutate in place, matching the same
    immutability RoutingRow/CatalogEntry already rely on elsewhere."""
    return CatalogEntry(
        name=entry.name,
        type=entry.type,
        category=entry.category,
        priority=entry.priority,
        exporter=entry.exporter,
        status=new_status,
        help=entry.help,
        unit=entry.unit,
        keywords=entry.keywords,
        reference_path=entry.reference_path,
        dimensions=entry.dimensions,
    )


def _prior_type(previous_catalog: Catalog | None, name: str) -> str | None:
    if previous_catalog is None:
        return None
    prior_entry = previous_catalog.get(name)
    return prior_entry.type if prior_entry else None


def _minimal_entry(
    name: str,
    info: RuntimeMetricInfo | None,
    status: str,
    fallback_type: str | None = None,
) -> CatalogEntry:
    """Builds a minimal, runtime-sourced-only CatalogEntry for a metric
    with no vendor-side (Markdown) documentation at all -- see module
    docstring, path 2. `exporter` is deliberately the explicit "unknown"
    sentinel rather than a guess from the metric name's prefix: this
    module has no reliable, deterministic way to know which exporter
    produced an arbitrary runtime metric name, and inventing one by
    pattern-matching the name would be exactly the kind of guess the
    frozen architecture repeatedly warns against.
    """
    raw_type = (info.type if info else None) or fallback_type or ""
    resolved_type = raw_type if raw_type in _VALID_TYPES else fallback_type
    return CatalogEntry(
        name=name,
        type=resolved_type or "gauge",
        category=UNCATEGORIZED,
        priority=Priority.REVIEW.value,
        exporter=_UNKNOWN_EXPORTER,
        status=status,
        help=(info.help if info and info.help else ""),
    )
