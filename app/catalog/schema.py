"""
app/catalog/schema.py

Pure data definitions for the metric catalog: what a catalog entry is, what
values its fields may take, and how a full catalog.json document is shaped.

No I/O lives here (that's loader.py's job) and no generation/retrieval logic
lives here (that's generator.py / search.py, later phases). This mirrors
app/skill_index.py's own separation between parsing structures
(RoutingRow, SkillMetadata) and the class that reads a file off disk
(SkillIndex.load).

Schema, per the frozen catalog-architecture spec:

    {
      "catalog_version": "1.0",
      "generated_at": "...",
      "metrics": [
        {
          "name": "...",
          "type": "gauge",
          "help": "...",
          "unit": "...",
          "keywords": [],
          "category": "...",
          "priority": "...",
          "exporter": "...",
          "status": "approved",
          "reference_path": null,
          "dimensions": []
        }
      ]
    }

Two things this schema deliberately does NOT carry, on purpose, per the
frozen architecture:

  - Runtime Prometheus label keys. `dimensions` is a small, curated list of
    *semantic* dimensions (e.g. "cpu", "mode") for retrieval/documentation
    purposes only -- never live label-key names. Actual label keys remain
    exclusively the responsibility of app/label_discovery.py at query time.
    Never populate `dimensions` from a live /api/v1/series or /api/v1/labels
    call; that would silently turn the catalog into the static label-key
    database the architecture explicitly forbids.

  - Metric-specific semantic prose (confusable-metric writeups, "use X
    instead of Y" rules, per-metric PromQL overrides). That content lives in
    the Markdown reference a `reference_path` points to, never inline in the
    catalog itself -- keeping the catalog machine-oriented, per the frozen
    architecture's explicit instruction not to put large semantic prose
    into catalog.json.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CatalogSchemaError(ValueError):
    """Raised for structural problems with catalog *content* -- a required
    field missing, an enum value outside the allowed set, a duplicate metric
    name. Distinct from CatalogLoadError (loader.py), which covers the file
    itself being unreadable/malformed JSON. Mirrors the SkillIndexError
    convention in app/skill_index.py: fail loudly at construction time
    rather than let a malformed entry silently reach search/generation.
    """


class MetricType(str, Enum):
    """Prometheus metric types. Matches Prometheus's own type system
    exactly -- this is not a catalog-invented taxonomy."""

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    SUMMARY = "summary"


class CatalogStatus(str, Enum):
    """The four states from the frozen catalog-status model (source-of-truth
    section of the spec). Reconciliation logic (reconciler.py, Phase 4) is
    what transitions an entry between these; schema.py only defines what the
    valid values are.

    approved:                   supported and currently available/usable.
    approved_unavailable:       supported by the approved universe but
                                 currently absent from Prometheus.
    discovered_pending_review:  Prometheus exposes it, but it is not (yet)
                                 in the approved vendor universe -- must
                                 NEVER be silently promoted to approved.
    rejected:                   explicitly excluded.
    """

    APPROVED = "approved"
    APPROVED_UNAVAILABLE = "approved_unavailable"
    DISCOVERED_PENDING_REVIEW = "discovered_pending_review"
    REJECTED = "rejected"


class Priority(str, Enum):
    """A ranking signal ONLY -- never a hard eligibility filter. search.py
    (Phase 6) must never use priority to exclude an otherwise-relevant
    metric; it may only use it to order/weight candidates. REVIEW marks a
    metric priority-generation couldn't confidently classify (kept distinct
    from a silently-wrong guess, same "prefer an explicit review state over
    inventing a misleading value" principle the spec applies to category)."""

    HIGH = "High"
    MEDIUM = "Medium"
    REVIEW = "Review"


_VALID_TYPES = {t.value for t in MetricType}
_VALID_STATUSES = {s.value for s in CatalogStatus}
_VALID_PRIORITIES = {p.value for p in Priority}

# Category has no closed enum: the set of categories is expected to grow
# (new exporters, new domains) without a code change, the same way SKILL.md's
# routing table can gain a new row without app/skill_index.py changing. The
# one hard rule is that it must be a non-empty string -- "uncategorized" is
# the explicit fallback value generator.py should use for a metric it
# couldn't confidently classify (see category-rules discussion, rules.py,
# Phase 5), never a blank string standing in for "unknown".
UNCATEGORIZED = "uncategorized"


@dataclass(frozen=True)
class CatalogEntry:
    """One metric's catalog record. Frozen (immutable) for the same reason
    RoutingRow is frozen in skill_index.py: this is loaded-once, shared,
    read-only data -- nothing in the request path should be able to mutate
    a shared CatalogEntry out from under a concurrent request.
    """

    name: str
    type: str
    category: str
    priority: str
    exporter: str
    status: str
    help: str = ""
    unit: str | None = None
    keywords: tuple[str, ...] = field(default_factory=tuple)
    reference_path: str | None = None
    dimensions: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise CatalogSchemaError(
                "CatalogEntry.name is required and must be a non-empty string."
            )
        if self.type not in _VALID_TYPES:
            raise CatalogSchemaError(
                f"CatalogEntry {self.name!r}: type {self.type!r} is not valid. "
                f"Must be one of {sorted(_VALID_TYPES)}."
            )
        if not self.category or not self.category.strip():
            raise CatalogSchemaError(
                f"CatalogEntry {self.name!r}: category is required and must be "
                f"non-empty -- use {UNCATEGORIZED!r} as the explicit fallback "
                f"for a metric that couldn't be confidently classified, never "
                f"a blank string."
            )
        if self.priority not in _VALID_PRIORITIES:
            raise CatalogSchemaError(
                f"CatalogEntry {self.name!r}: priority {self.priority!r} is not "
                f"valid. Must be one of {sorted(_VALID_PRIORITIES)}."
            )
        if not self.exporter or not self.exporter.strip():
            raise CatalogSchemaError(
                f"CatalogEntry {self.name!r}: exporter is required and must be "
                f"a non-empty string."
            )
        if self.status not in _VALID_STATUSES:
            raise CatalogSchemaError(
                f"CatalogEntry {self.name!r}: status {self.status!r} is not "
                f"valid. Must be one of {sorted(_VALID_STATUSES)}."
            )
        if self.reference_path is not None and not self.reference_path.strip():
            raise CatalogSchemaError(
                f"CatalogEntry {self.name!r}: reference_path must be either a "
                f"non-empty path string or null -- not an empty string."
            )

    # ---- (de)serialization -------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CatalogEntry":
        """Builds one CatalogEntry from a JSON-decoded dict. Missing optional
        fields fall back to this class's own defaults; missing *required*
        fields surface as a CatalogSchemaError from __post_init__ (via the
        empty-string/None sentinel below) rather than a confusing KeyError,
        so a malformed catalog.json fails with a message that names the
        offending metric.
        """
        try:
            name = data["name"]
        except KeyError as e:
            raise CatalogSchemaError(
                "Catalog entry is missing required field 'name'."
            ) from e

        return cls(
            name=name,
            type=data.get("type", ""),
            category=data.get("category", ""),
            priority=data.get("priority", ""),
            exporter=data.get("exporter", ""),
            status=data.get("status", ""),
            help=data.get("help") or "",
            unit=data.get("unit"),
            keywords=tuple(data.get("keywords") or ()),
            reference_path=data.get("reference_path"),
            dimensions=tuple(data.get("dimensions") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        """Inverse of from_dict; used by generator.py (Phase 2+) to write
        catalog.json and by tests to round-trip an entry."""
        return {
            "name": self.name,
            "type": self.type,
            "help": self.help,
            "unit": self.unit,
            "keywords": list(self.keywords),
            "category": self.category,
            "priority": self.priority,
            "exporter": self.exporter,
            "status": self.status,
            "reference_path": self.reference_path,
            "dimensions": list(self.dimensions),
        }


@dataclass(frozen=True)
class Catalog:
    """The full catalog.json document, loaded and validated. Build via
    Catalog.from_dict (used internally by loader.py's load_catalog) or
    directly in tests; nothing here reads from disk.
    """

    catalog_version: str
    generated_at: str
    metrics: tuple[CatalogEntry, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.catalog_version or not self.catalog_version.strip():
            raise CatalogSchemaError(
                "Catalog.catalog_version is required and must be non-empty."
            )
        if not self.generated_at or not self.generated_at.strip():
            raise CatalogSchemaError(
                "Catalog.generated_at is required and must be non-empty."
            )
        seen: dict[str, CatalogEntry] = {}
        for entry in self.metrics:
            if entry.name in seen:
                raise CatalogSchemaError(
                    f"Duplicate metric name in catalog: {entry.name!r} appears "
                    f"more than once. Each metric must have exactly one entry."
                )
            seen[entry.name] = entry

    # ---- (de)serialization -------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Catalog":
        try:
            catalog_version = data["catalog_version"]
            generated_at = data["generated_at"]
        except KeyError as e:
            raise CatalogSchemaError(
                f"Catalog document is missing required top-level field: {e}"
            ) from e

        raw_metrics = data.get("metrics", [])
        if not isinstance(raw_metrics, list):
            raise CatalogSchemaError(
                "Catalog document's 'metrics' field must be a list."
            )
        entries = tuple(CatalogEntry.from_dict(m) for m in raw_metrics)
        return cls(
            catalog_version=catalog_version,
            generated_at=generated_at,
            metrics=entries,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_version": self.catalog_version,
            "generated_at": self.generated_at,
            "metrics": [m.to_dict() for m in self.metrics],
        }

    # ---- lookup helpers -------------------------------------------------
    # Small, deliberately dumb accessors -- no ranking/search behavior here.
    # Candidate ranking/scoring is search.py's job (Phase 6), not schema.py's.

    def get(self, name: str) -> CatalogEntry | None:
        """O(1) exact-name lookup would be nice, but Phase 1 keeps this
        linear and simple since the current catalog is 43 entries; revisit
        with an index dict only if Phase 17's scale testing shows it
        matters at 500-1000 entries."""
        for entry in self.metrics:
            if entry.name == name:
                return entry
        return None

    def by_status(self, status: str) -> tuple[CatalogEntry, ...]:
        return tuple(m for m in self.metrics if m.status == status)

    def by_category(self, category: str) -> tuple[CatalogEntry, ...]:
        return tuple(m for m in self.metrics if m.category == category)

    def by_exporter(self, exporter: str) -> tuple[CatalogEntry, ...]:
        return tuple(m for m in self.metrics if m.exporter == exporter)
