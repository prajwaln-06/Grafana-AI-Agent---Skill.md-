"""
scripts/generate_production_catalog.py

Batch 2: the production Catalog-generation cutover.

    python -m scripts.generate_production_catalog <vendor_docx_path> [skills_root] [output_path]

This is now the authoritative production entry point for
app/catalog/catalog.json. It implements the frozen migration
architecture's target flow:

    Vendor metric document
          |
          v
    vendor parser (app/catalog/vendor_docx.py)
          |
          v
    VendorMetricRecord (app/catalog/vendor.py)
          |
          v
    CatalogEntry / Catalog
          |
          v
    app/catalog/catalog.json

`CatalogEntry.category` on every generated entry is the vendor's own
category text, verbatim (Option C: never Markdown-derived -- see
app/catalog/vendor.py's module docstring). `reference_path` is resolved
independently, by a metric-NAME join against
app.catalog.generator.build_markdown_reference_index() -- a vendor metric
with no matching semantic Markdown gets `reference_path=None`, a valid,
expected outcome, never an error and never a reason to exclude the metric
from the catalog. Markdown is therefore no longer required for a metric
to enter the persistent Catalog.

Per the frozen architecture, Markdown-Category -> keyword enrichment
(Option C's additive retrieval vocabulary) and Phase 5's keyword/priority
classification (app/catalog/rules.py) are deliberately NOT applied here
and NOT persisted into catalog.json -- exactly as they were not applied
by the pre-Batch-2 Markdown-sourced generator.py's own main(). Both are
applied in memory, at load time, by app/pipeline.py::_get_catalog(), so
the two concerns (what this script derives from the vendor source, vs.
what rules.py classifies/enriches) stay visibly separate and can never
drift from what's actually committed to the repo. This script only
writes the vendor-source-derived, reference-path-resolved fields.

app/catalog/generator.py's Markdown-parsing generate_catalog()/main()
remain available for migration/testing/comparison (e.g. diffing a
Markdown-derived candidate against this vendor-derived production
catalog) but are no longer the authoritative source for
app/catalog/catalog.json -- this script is. Likewise,
app/catalog/vendor_docx.py's own `main()` CLI remains available for
generating a non-production CANDIDATE catalog
(app/catalog/catalog.vendor_candidate.json) for review before a cutover;
this script is the deliberate, reviewed "yes, write the production file
now" step that follows that review.
"""

from __future__ import annotations

from pathlib import Path

from app.catalog.generator import build_markdown_reference_index, write_catalog
from app.catalog.schema import Catalog
from app.catalog.vendor import RejectedVendorRow, build_catalog_from_vendor_records
from app.catalog.vendor_docx import parse_vendor_docx

DEFAULT_OUTPUT_PATH = Path("app/catalog/catalog.json")
DEFAULT_SKILLS_ROOT = Path("skills")


def generate_production_catalog(
    vendor_path: Path,
    skills_root: Path = DEFAULT_SKILLS_ROOT,
    generated_at: str | None = None,
) -> tuple[Catalog, tuple[RejectedVendorRow, ...]]:
    """Parses `vendor_path` and builds the production Catalog, without
    touching disk beyond reading the vendor document and the skill
    package's Markdown (for reference_path resolution only). Returns the
    Catalog plus every malformed vendor row that was deterministically
    rejected (e.g. the known stray Filesystem/backtick row), so a caller
    (main() below, or a test) can inspect/report rejections without
    re-parsing the document."""
    result = parse_vendor_docx(vendor_path)
    markdown_index = build_markdown_reference_index(skills_root)
    catalog = build_catalog_from_vendor_records(
        result.records, markdown_index, generated_at=generated_at
    )
    return catalog, result.rejected_rows


def main() -> int:  # pragma: no cover -- thin CLI wrapper, exercised via
    # generate_production_catalog()'s own unit tests, not as a subprocess.
    import sys

    if len(sys.argv) < 2:
        print(
            "usage: python -m scripts.generate_production_catalog "
            "<vendor_docx_path> [skills_root] [output_path]"
        )
        return 2

    vendor_path = Path(sys.argv[1])
    skills_root = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_SKILLS_ROOT
    output_path = Path(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_OUTPUT_PATH

    catalog, rejected_rows = generate_production_catalog(vendor_path, skills_root)

    if rejected_rows:
        print(f"Rejected {len(rejected_rows)} malformed vendor row(s):")
        for row in rejected_rows:
            print(f"  {row.raw_cells!r} -> {row.reason}")

    write_catalog(catalog, output_path)

    with_reference = sum(1 for m in catalog.metrics if m.reference_path is not None)
    by_exporter: dict[str, int] = {}
    for m in catalog.metrics:
        by_exporter[m.exporter] = by_exporter.get(m.exporter, 0) + 1

    print(
        f"Generated {len(catalog.metrics)} production catalog entries "
        f"(catalog_version={catalog.catalog_version}) -> {output_path}"
    )
    print(
        f"  {with_reference} with a resolved reference_path, "
        f"{len(catalog.metrics) - with_reference} with reference_path=None"
    )
    for exporter, count in sorted(by_exporter.items()):
        print(f"  {exporter}: {count}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
