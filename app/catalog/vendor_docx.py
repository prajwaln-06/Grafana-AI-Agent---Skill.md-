"""
app/catalog/vendor_docx.py

DOCX-format-specific half of the vendor-source Catalog migration.
Everything in here is about the attached vendor document's own presentation
shape (two Word tables, a fixed column order, an occasional malformed row)
-- NOT about what a "vendor metric" conceptually is or how it becomes a
CatalogEntry. That format-agnostic side lives entirely in
app/catalog/vendor.py (VendorMetricRecord, build_catalog_from_vendor_records)
and does not import anything from this module.

Isolating the DOCX reading here means a future vendor source in a
different format (CSV export, a differently-shaped DOCX, a JSON feed from
an internal metrics registry) needs only its own small `parse_vendor_*`
function that produces the same VendorMetricRecord shape -- Catalog
construction itself never changes.

Table shape this module understands (see the attached vendor document):

    | Category | <Node Exporter Metric | DCGM Metric> | Prometheus Type |
      What it Measures | Typical User Questions / Typical Query Intent |

Column 0 (Category), column 2 (Prometheus Type), and column 3 (What it
Measures) mean the same thing in both tables. Column 1's HEADER TEXT is
what tells this module which table it is looking at -- and therefore
which exporter every row in that table belongs to -- never a metric-name
heuristic (e.g. "starts with node_" / "starts with DCGM_") and never a
nonexistent dedicated exporter column. Column 4 (Typical User Questions /
Typical Query Intent) is read only far enough to confirm the table's
shape; per the frozen migration architecture, mining it for retrieval
vocabulary is explicitly out of scope for this batch.
"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document

from app.catalog.vendor import RejectedVendorRow, VendorMetricRecord, VendorParseError, VendorParseResult

# Column-1 header text -> exporter. This is the ONLY place a vendor table
# is associated with an exporter name; a row's own metric name is never
# used to guess it (see module docstring).
_TABLE_HEADER_TO_EXPORTER: dict[str, str] = {
    "node exporter metric": "node-exporter",
    "dcgm metric": "dcgm-exporter",
}

_EXPECTED_COLUMN_COUNT = 5

# A syntactically plausible Prometheus metric name: starts with a letter
# or underscore, then letters/digits/underscores/colons. This is a
# deliberately generic shape check (matches Prometheus's own metric-name
# grammar), not a lookup against any specific known-name list -- it is
# what rejects the stray "`" row (a single backtick matches none of this)
# without needing to hardcode "the malformed row is named exactly this."
_METRIC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_:]*$")

# Case-insensitive; matches schema.MetricType's own values.
_VALID_VENDOR_TYPES = {"counter", "gauge", "histogram", "summary"}

# Finds every top-level "(...)" group in a "What it Measures" cell.
_PAREN_RE = re.compile(r"\(([^()]*)\)")

# The exhaustive set of tokens this vendor document uses to state an
# explicit unit in "What it Measures", e.g. "GPU core temperature (°C)".
# Matched case-insensitively against the LAST parenthetical group only
# (see _extract_unit's docstring for why "last" and why an allowlist).
# This is intentionally a closed list, not a generic pattern: extending
# it to recognize a new token is a deliberate, reviewable edit, never an
# automatic inference. Keys are lowercased for lookup; values are the
# canonical unit string this module emits.
_RECOGNIZED_VENDOR_UNITS: dict[str, str] = {
    "%": "%",
    "bytes": "bytes",
    "watts": "Watts",
    "mhz": "MHz",
    "°c": "°C",
}


def _extract_unit(what_it_measures: str) -> str | None:
    """Deterministically extracts an explicit unit from a vendor "What it
    Measures" cell, or returns None if the cell does not explicitly state
    one this module recognizes. Never infers or guesses a unit from the
    metric name, category, or any other field -- only this cell's own
    parenthetical content is ever considered.

    Rule: take the LAST top-level "(...)" group in the text (the vendor
    document's own convention is to state the unit as the final
    parenthetical, e.g. "Used framebuffer (VRAM) memory (bytes)" -- an
    earlier parenthetical, like "(VRAM)" there, is descriptive, not the
    unit). Accept that last group as the unit ONLY if it exactly matches
    (case-insensitive, trimmed) a token in _RECOGNIZED_VENDOR_UNITS.

    Any other last-parenthetical content -- e.g. "(user, system, idle,
    iowait, etc.)" on node_cpu_seconds_total, which is a list of label
    values, not a unit -- is deliberately left unmatched and yields None.
    A cell with no parenthetical at all also yields None. Both are the
    same outcome: "no explicit vendor-stated unit here," never an
    invented one.
    """
    groups = _PAREN_RE.findall(what_it_measures)
    if not groups:
        return None
    candidate = groups[-1].strip()
    return _RECOGNIZED_VENDOR_UNITS.get(candidate.lower())


def _exporter_for_table(header_cells: list[str]) -> str | None:
    """None means "this table's column-1 header doesn't match a known
    vendor-table shape" -- the caller decides whether that is fatal
    (parse_vendor_docx: no recognizable metric table found at all) or
    simply "not a metric table" (e.g. an incidental non-metric table
    elsewhere in the document, which this module tolerates rather than
    assuming the whole document is nothing but metric tables)."""
    if len(header_cells) < 2:
        return None
    return _TABLE_HEADER_TO_EXPORTER.get(header_cells[1].strip().lower())


def _row_to_record_or_rejection(
    cells: list[str], exporter: str
) -> VendorMetricRecord | RejectedVendorRow:
    """Validates one data row deterministically and returns either a
    valid VendorMetricRecord or a RejectedVendorRow explaining why not.
    Never raises for a single bad row -- see VendorParseError's docstring
    for why a data-quality problem in one row must not abort the whole
    document parse.
    """
    raw_cells = tuple(cells)

    if len(cells) != _EXPECTED_COLUMN_COUNT:
        return RejectedVendorRow(
            raw_cells=raw_cells,
            reason=f"expected {_EXPECTED_COLUMN_COUNT} columns, got {len(cells)}.",
        )

    category, metric_name, ptype, what_it_measures, _typical_questions = (
        c.strip() for c in cells
    )

    if not _METRIC_NAME_RE.match(metric_name):
        return RejectedVendorRow(
            raw_cells=raw_cells,
            reason=(
                f"metric name {metric_name!r} does not look like a valid "
                f"Prometheus metric name."
            ),
        )
    if ptype.lower() not in _VALID_VENDOR_TYPES:
        return RejectedVendorRow(
            raw_cells=raw_cells,
            reason=f"Prometheus Type {ptype!r} is not one of {sorted(_VALID_VENDOR_TYPES)}.",
        )
    if not category:
        return RejectedVendorRow(raw_cells=raw_cells, reason="Category is empty.")
    if not what_it_measures:
        return RejectedVendorRow(raw_cells=raw_cells, reason="What it Measures is empty.")

    return VendorMetricRecord(
        name=metric_name,
        type=ptype,
        category=category,
        help=what_it_measures,
        exporter=exporter,
        unit=_extract_unit(what_it_measures),
    )


def parse_vendor_docx(path: Path) -> VendorParseResult:
    """Parses the attached vendor metric document (a DOCX with one table
    per exporter) into a VendorParseResult.

    Deterministic: the same file always produces the same records in the
    same order (document table order, then row order within each table --
    never re-sorted by this module).

    Raises VendorParseError if the document contains no table whose
    column-1 header matches a known vendor-table shape at all -- that is
    a structural "this isn't the document format this module understands"
    problem, not a single-row data-quality issue. A malformed individual
    DATA row (e.g. the known stray ['Filesystem', '`', '', '', ''] row)
    is instead collected in the result's `rejected_rows` and does NOT
    raise or otherwise prevent the rest of the document's valid rows from
    being parsed.
    """
    document = Document(str(path))

    records: list[VendorMetricRecord] = []
    rejected: list[RejectedVendorRow] = []
    recognized_any_table = False

    for table in document.tables:
        if not table.rows:
            continue
        header_cells = [cell.text for cell in table.rows[0].cells]
        exporter = _exporter_for_table(header_cells)
        if exporter is None:
            # Not a table this module recognizes as a metric table --
            # tolerated, not fatal (see _exporter_for_table's docstring).
            continue
        recognized_any_table = True

        for row in table.rows[1:]:
            cells = [cell.text for cell in row.cells]
            result = _row_to_record_or_rejection(cells, exporter)
            if isinstance(result, VendorMetricRecord):
                records.append(result)
            else:
                rejected.append(result)

    if not recognized_any_table:
        raise VendorParseError(
            f"{path}: no table with a recognized vendor-table header "
            f"(column 1 must read one of {sorted(_TABLE_HEADER_TO_EXPORTER)}) "
            f"was found. This document does not match the vendor-source "
            f"shape this parser understands."
        )

    return VendorParseResult(records=tuple(records), rejected_rows=tuple(rejected))


def main() -> int:  # pragma: no cover -- thin CLI wrapper, exercised via
    # parse_vendor_docx()/build_catalog_from_vendor_records() unit tests,
    # not as a subprocess.
    """CLI entry point:

        python -m app.catalog.vendor_docx <vendor_docx_path> [skills_root] [output_path]

    Generates a CANDIDATE vendor-sourced catalog and writes it to
    `output_path` (default: app/catalog/catalog.vendor_candidate.json --
    deliberately NOT app/catalog/catalog.json itself). Per the frozen
    migration architecture, cutting the production catalog over to this
    candidate is a separate, deliberate step performed only after
    reviewing this candidate's diff against the currently committed
    catalog -- this CLI never overwrites the production file.
    """
    import sys

    from app.catalog.generator import build_markdown_reference_index, write_catalog
    from app.catalog.vendor import build_catalog_from_vendor_records

    if len(sys.argv) < 2:
        print(
            "usage: python -m app.catalog.vendor_docx <vendor_docx_path> "
            "[skills_root] [output_path]"
        )
        return 2

    vendor_path = Path(sys.argv[1])
    skills_root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("skills")
    output_path = (
        Path(sys.argv[3])
        if len(sys.argv) > 3
        else Path("app/catalog/catalog.vendor_candidate.json")
    )

    result = parse_vendor_docx(vendor_path)
    if result.rejected_rows:
        print(f"Rejected {len(result.rejected_rows)} malformed row(s):")
        for row in result.rejected_rows:
            print(f"  {row.raw_cells!r} -> {row.reason}")

    markdown_index = build_markdown_reference_index(skills_root)
    catalog = build_catalog_from_vendor_records(result.records, markdown_index)
    write_catalog(catalog, output_path)

    with_reference = sum(1 for m in catalog.metrics if m.reference_path is not None)
    print(
        f"Generated {len(catalog.metrics)} candidate vendor-sourced catalog "
        f"entries ({with_reference} with a resolved reference_path, "
        f"{len(catalog.metrics) - with_reference} with reference_path=None) "
        f"-> {output_path}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
