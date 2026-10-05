"""
app/catalog/vendor.py

Vendor-source Catalog construction: the format-agnostic half of the
Catalog-source migration.

    Vendor document
          |
          v
    vendor parser (format-specific, e.g. app/catalog/vendor_docx.py)
          |
          v
    VendorMetricRecord            <-- this module defines this
          |
          v
    build_catalog_from_vendor_records()  <-- and this
          |
          v
    CatalogEntry / Catalog

Nothing in this module knows or cares what document format a
VendorMetricRecord came from. A future vendor source (CSV, a different
DOCX layout, an API response) only needs its own small parser that
produces the same VendorMetricRecord shape; Catalog construction itself
does not change.

This module deliberately does NOT:
  - read any file from disk (that's vendor_docx.py's job for the DOCX
    format specifically);
  - decide `reference_path` from vendor `category` -- reference_path is
    resolved by a metric-NAME join against
    app.catalog.generator.build_markdown_reference_index(), never a
    vendor-category mapping (see module docstring rationale: our curated
    semantic reference groupings are deliberately not a mirror of the
    vendor's own taxonomy);
  - invent a `unit` value. `VendorMetricRecord.unit` is whatever
    app/catalog/vendor_docx.py's `_extract_unit` deterministically found
    (or None) in the vendor document's own "What it Measures" text --
    this module only carries that value through onto CatalogEntry
    verbatim, never derives or guesses one itself. A missing/
    unrecognized unit stays None, which is valid per schema.py and
    renders as "n/a" in the Generator prompt (CatalogEntry.unit is
    informational-only there -- see this module's Batch 1 handoff
    notes);
  - touch keywords/priority beyond the same fixed defaults
    generator.py's Markdown path already uses (`Priority.REVIEW`,
    `keywords=()`) -- classifying those is rules.py's (Phase 5) job,
    applied uniformly to a catalog regardless of which path produced it,
    never re-invented here for the vendor path specifically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.catalog.generator import CATALOG_VERSION
from app.catalog.schema import Catalog, CatalogEntry, CatalogStatus, Priority


class VendorParseError(Exception):
    """Raised for a STRUCTURAL problem with a vendor document that makes it
    impossible to interpret at all -- e.g. a table whose header row does
    not match any known vendor-table shape, so this module has no idea
    which exporter (if any) the table's rows belong to. This is the
    "fail loudly, cannot proceed" case.

    Distinct from a single malformed DATA row (see RejectedVendorRow
    below), which this module skips deterministically and reports,
    rather than aborting the whole document over one bad row -- the same
    "a data-quality problem in one record must not corrupt the whole
    catalog" principle reconciler.py already applies to a runtime metric
    with an unresolvable type (see its `undetermined_type_skipped`).
    """


@dataclass(frozen=True)
class VendorMetricRecord:
    """One metric, normalized from a vendor document into the shape
    Catalog construction needs, independent of what document format it
    came from.

    Deliberately minimal -- per the frozen migration architecture, "do
    not invent unnecessary fields." `category` is the vendor's own
    category text, verbatim; it is retrieval/ranking metadata only and is
    NEVER used to derive `reference_path` (see module docstring).
    """

    name: str
    type: str
    category: str
    help: str
    exporter: str
    unit: str | None = None


@dataclass(frozen=True)
class RejectedVendorRow:
    """One vendor-document data row that could not be turned into a
    VendorMetricRecord, kept for reporting rather than silently dropped.
    `raw_cells` preserves the row exactly as read (e.g. the known
    ['Filesystem', '`', '', '', ''] stray row in the Node Exporter table)
    so a human reviewing generation output can see exactly what was
    rejected and why."""

    raw_cells: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class VendorParseResult:
    """Everything one vendor-document parse produced: the valid records,
    plus every row that was deterministically rejected as malformed.
    Mirrors reconciler.py's ReconciliationReport convention of surfacing
    what-was-skipped-and-why alongside the successful result, rather than
    only returning the happy-path data."""

    records: tuple[VendorMetricRecord, ...] = field(default_factory=tuple)
    rejected_rows: tuple[RejectedVendorRow, ...] = field(default_factory=tuple)


def build_catalog_from_vendor_records(
    records: "tuple[VendorMetricRecord, ...] | list[VendorMetricRecord]",
    markdown_reference_index: dict[str, str],
    generated_at: str | None = None,
) -> Catalog:
    """Converts normalized vendor records into a candidate Catalog.

    `markdown_reference_index` is the metric-name -> reference_path map
    from app.catalog.generator.build_markdown_reference_index() (the
    existing Markdown metric-discovery logic, reused rather than
    duplicated). A record whose name has no entry in that index gets
    `reference_path=None` -- a valid, expected outcome for a vendor-only
    metric with no semantic Markdown yet, NOT an error.

    Every entry gets the same fixed defaults generator.py's Markdown path
    already establishes for a freshly-generated (not yet reconciled/
    classified) vendor-approved entry:
      - status:   CatalogStatus.APPROVED (this is the vendor/reference-
                  universe side of the hybrid model; reconciler.py, not
                  this function, is what may later demote an entry to
                  approved_unavailable based on live Prometheus data).
      - priority: Priority.REVIEW (Phase 5/rules.py's job to classify).
      - keywords: () (also Phase 5/rules.py's job).
      - unit:     record.unit, carried through verbatim from the vendor
                  parser's own deterministic extraction (see module
                  docstring) -- never derived or guessed here.
      - dimensions: () (not populated from vendor data; see schema.py).

    Raises CatalogSchemaError (via CatalogEntry/Catalog construction) if
    a record's own fields are invalid -- e.g. an empty name, or a `type`
    that doesn't normalize to one of schema.MetricType's values -- the
    same fail-loud behavior schema.py already gives every other Catalog
    construction path. This function does not pre-validate records for
    that; parser-level validation (rejecting a row like the stray
    backtick before it becomes a VendorMetricRecord at all) is the
    parser's job, not this one's.
    """
    entries = tuple(
        CatalogEntry(
            name=record.name,
            type=record.type.strip("`").strip().lower(),
            category=record.category.strip(),
            priority=Priority.REVIEW.value,
            exporter=record.exporter,
            status=CatalogStatus.APPROVED.value,
            help=record.help.strip(),
            unit=record.unit,
            keywords=(),
            reference_path=markdown_reference_index.get(record.name),
            dimensions=(),
        )
        for record in records
    )

    if generated_at is None:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return Catalog(
        catalog_version=CATALOG_VERSION,
        generated_at=generated_at,
        metrics=entries,
    )
