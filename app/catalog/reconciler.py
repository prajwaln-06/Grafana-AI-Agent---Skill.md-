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
that Catalog is built from the vendor metric document
(scripts/generate_production_catalog.py -> vendor_docx.py -> vendor.py).
It was originally bootstrapped from the project's hand-authored Markdown
by generator.py (Phase 2); that path is now legacy/non-production.

Neither source is meant to be a permanent gate on onboarding new metrics.
Requiring a full "### `metric_name`" Markdown section
with Category/Purpose/Type bullets for every new metric before it can be
catalogued would defeat the point of the hybrid model -- the whole reason
`discovered_pending_review` exists as a status is to let a metric
Prometheus is already exposing enter the catalog on minimal, runtime-
sourced metadata (name + Prometheus's own reported type/HELP, see
discover_runtime_metadata() below), pending a human's review, WITHOUT
anyone first writing Markdown for it.

So, concretely, two independent paths feed a metric into the catalog:

  1. Vendor-approved metrics (today's 43) -- vendor_docx.py / vendor.py
     -> vendor_catalog -> reconcile() confirms/denies availability against
     runtime discovery.
  2. Metrics Prometheus is exposing that are NOT yet in the vendor-
     approved catalog at all -- reconcile() itself builds a minimal entry
     for these directly from runtime discovery (name, type, help), status
     `discovered_pending_review`, category `UNCATEGORIZED`, no Markdown
     required. A human reviewing that status is what may eventually
     promote it to "approved" (by adding it to the vendor-approved side,
     however that side is produced by then -- generator.py, a future
     lightweight vendor manifest, or direct curation) or set it to
     "rejected" -- reconcile() itself never performs that promotion.

Phase 2's Markdown generator was therefore only the *migration* path for
already-documented metrics. The production source has since moved to the
vendor document with zero changes to this module -- exactly the decoupling
described above. Path 2 is the intended *ongoing* onboarding path for new
metrics, and reconcile() still only ever consumes a Catalog.

---- Runtime status

reconcile() and the discover_* helpers are implemented and unit-tested
(tests/test_reconciler.py), but nothing in app/ calls them: no startup hook,
scheduled job, or endpoint runs reconciliation or persists its result. The
committed catalog.json therefore contains only `approved` entries (the
vendor builder's default); `approved_unavailable` and
`discovered_pending_review` can only appear once this is wired in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any, Mapping

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
# Public alias: the exporter sentinel for a runtime-discovered metric whose
# exporter nobody has stated yet. app/catalog/reviews.py refuses to accept an
# approval that still carries it.
UNKNOWN_EXPORTER = _UNKNOWN_EXPORTER

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
    from more than one target, the first entry's HELP is used. If those
    entries disagree about the TYPE, the type is reported as undetermined
    (None) instead of picking one -- see the warning logged below.
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
        reported_types = {
            e.get("type")
            for e in (entries if isinstance(entries, list) else [entries])
            if isinstance(e, dict)
        }
        if len(reported_types) > 1:
            # Prometheus's documented response lists *unique* metadata objects
            # per metric name across targets, so two targets can disagree.
            # HELP text disagreeing is harmless, but a disagreement about the
            # TYPE means this module cannot know which is right -- report it
            # as undetermined (reconcile() then skips it) rather than letting
            # "first entry wins" silently pick one.
            logger.warning(
                "Prometheus metadata for %r reports conflicting types %s; "
                "treating its type as undetermined.",
                name,
                sorted(str(t) for t in reported_types),
            )
            result[name] = RuntimeMetricInfo(type=None, help=first.get("help"))
            continue
        result[name] = RuntimeMetricInfo(type=first.get("type"), help=first.get("help"))
    return result


# ---- discovery scope ------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryScope:
    """Explicit, configurable rules for which *runtime-only* metric names are
    allowed into the review queue (`discovered_pending_review`).

    Why this exists: a Prometheus server also exposes its own and every other
    scrape target's metrics (go_*, process_*, scrape_*, ...). Without a scope,
    each of those becomes a pending review item. The rules are shell-style
    globs (case-sensitive, like Prometheus metric names), supplied by the
    deployment -- nothing is hard-coded here, and the default (no patterns)
    is "everything is in scope", i.e. exactly the previous behavior.

      - If `include` is non-empty, a name must match at least one include glob.
      - A name matching any `exclude` glob is out of scope (exclude wins).

    The scope ONLY limits which previously-unknown metrics get queued for
    review. It never affects availability of vendor/approved metrics, and it
    never un-rejects anything: a metric a human rejected stays rejected
    regardless of scope.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, patterns in (("include", self.include), ("exclude", self.exclude)):
            for pattern in patterns:
                if not isinstance(pattern, str) or not pattern.strip():
                    raise ValueError(
                        f"DiscoveryScope {label} patterns must be non-empty "
                        f"strings, got {pattern!r}."
                    )

    @classmethod
    def from_settings(cls, settings: Any) -> "DiscoveryScope":
        return cls(
            include=tuple(settings.catalog_discovery_include_patterns),
            exclude=tuple(settings.catalog_discovery_exclude_patterns),
        )

    def allows(self, name: str) -> bool:
        if any(fnmatchcase(name, p) for p in self.exclude):
            return False
        if self.include and not any(fnmatchcase(name, p) for p in self.include):
            return False
        return True


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
    # Runtime-only metrics left out of the review queue by a DiscoveryScope.
    out_of_scope_skipped: tuple[str, ...] = field(default_factory=tuple)
    # How many metric names the (successful) runtime discovery returned --
    # lets a caller tell "discovery succeeded and found nothing" (0) apart
    # from a discovery failure (which raises ReconciliationSkipped instead).
    runtime_name_count: int = 0


def reconcile(
    vendor_catalog: Catalog,
    runtime_names: set[str] | None,
    runtime_metadata: dict[str, RuntimeMetricInfo] | None = None,
    previous_catalog: Catalog | None = None,
    generated_at: str | None = None,
    discovery_scope: DiscoveryScope | None = None,
    rejections: Mapping[str, RuntimeMetricInfo] | None = None,
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

    `rejections`, if given (even as an empty mapping), is the AUTHORITATIVE
    set of explicit human rejections ({name: stored type/HELP snapshot}, see
    app/catalog/reviews.py): exactly those names are rejected, and
    `previous_catalog` is then used only to report availability transitions,
    never to carry a rejection forward. That is what lets a rejection that a
    human has cleared actually go away, while one that has not been cleared
    can never be undone by rediscovery or by the vendor catalog. When
    `rejections` is None the original behavior applies (a status of rejected
    in `previous_catalog` is preserved).

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

    `discovery_scope`, if given, limits only which *runtime-only, never-
    rejected* metrics are queued as `discovered_pending_review`; those it
    excludes are listed in `out_of_scope_skipped`. It is checked before the
    type check, so out-of-scope names never clutter `undetermined_type_
    skipped`, and it never changes any vendor/approved/rejected outcome.

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

    def is_rejected(name: str) -> bool:
        if rejections is not None:
            return name in rejections
        return previous_status.get(name) == CatalogStatus.REJECTED.value

    reconciled: list[CatalogEntry] = []
    newly_discovered: list[str] = []
    became_unavailable: list[str] = []
    became_available_again: list[str] = []
    kept_rejected: list[str] = []
    undetermined_type_skipped: list[str] = []
    out_of_scope_skipped: list[str] = []

    # ---- vendor-approved side ------------------------------------------
    for entry in vendor_catalog.metrics:
        prior = previous_status.get(entry.name)
        present = entry.name in runtime_names

        if is_rejected(entry.name):
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
        if is_rejected(name):
            kept_rejected.append(name)
            stored = rejections.get(name) if rejections is not None else None
            reconciled.append(
                _minimal_entry(
                    name,
                    runtime_metadata.get(name),
                    status=CatalogStatus.REJECTED.value,
                    fallback_type=(stored.type if stored else None)
                    or _prior_type(previous_catalog, name),
                )
            )
            continue

        if discovery_scope is not None and not discovery_scope.allows(name):
            out_of_scope_skipped.append(name)
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
        out_of_scope_skipped=tuple(out_of_scope_skipped),
        runtime_name_count=len(runtime_names),
    )


def run_reconciliation(
    vendor_catalog: Catalog,
    base_url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    previous_catalog: Catalog | None = None,
    discovery_scope: DiscoveryScope | None = None,
    generated_at: str | None = None,
    rejections: Mapping[str, RuntimeMetricInfo] | None = None,
) -> ReconciliationReport:
    """Discovery + reconcile() in one call -- the single orchestration entry
    point shared by the offline CLI and (later) the runtime refresh, so the
    two can never disagree about how discovery failure is handled.

    Both discovery requests must succeed. If either returns None, this
    raises ReconciliationSkipped naming which one failed and returns/changes
    nothing: a half-successful discovery (names but no metadata, or the
    reverse) would silently drop every pending discovery or misreport
    availability, which is partial state this function refuses to produce.

    A *successful* discovery that finds zero names is NOT a failure: it
    returns a report whose `runtime_name_count` is 0 (and every approved
    metric becomes approved_unavailable, per reconcile()).
    """
    names = discover_runtime_metric_names(base_url, timeout=timeout)
    if names is None:
        raise ReconciliationSkipped(
            f"Metric-name discovery failed against {base_url} "
            f"(/api/v1/label/__name__/values); nothing was reconciled."
        )
    metadata = discover_runtime_metadata(base_url, timeout=timeout)
    if metadata is None:
        raise ReconciliationSkipped(
            f"Metric-metadata discovery failed against {base_url} "
            f"(/api/v1/metadata); nothing was reconciled."
        )
    return reconcile(
        vendor_catalog,
        runtime_names=names,
        runtime_metadata=metadata,
        previous_catalog=previous_catalog,
        generated_at=generated_at,
        discovery_scope=discovery_scope,
        rejections=rejections,
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
