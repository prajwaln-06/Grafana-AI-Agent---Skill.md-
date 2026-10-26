"""
app/catalog/

Structured metric registry + retrieval layer (metric-catalogization work,
per the frozen catalog architecture). This package is ordinary backend
infrastructure -- not an ADK Agent, not an LLM knowledge dump, not a
replacement for SKILL.md/Router/Generator/Validator.

It is fully wired into the request path (see app/pipeline.py's module
docstring). Participation is mandatory to ATTEMPT but never mandatory to
SUCCEED: a missing or invalid catalog.json makes every catalog-assisted
step degrade to its pre-catalog behavior rather than fail a request.

Modules, grouped by when they run:

  Build time (producing catalog.json; entry point is
  scripts/generate_production_catalog.py)
    vendor_docx.py  DOCX-specific parser: vendor tables -> VendorMetricRecord
    vendor.py       format-agnostic: VendorMetricRecord -> Catalog
    generator.py    Markdown-discovery helpers (metric -> reference_path,
                    metric -> Markdown Category). Also holds a legacy,
                    NON-production Markdown-sourced catalog generator.

  Load time (once per process)
    schema.py       CatalogEntry / Catalog data model and validation
    loader.py       catalog.json -> validated Catalog (fails closed)
    rules.py        keywords + priority, applied IN MEMORY by
                    app/pipeline.py::_get_catalog -- not stored in the JSON

  Request time
    search.py       deterministic scoring/ranking, used by the pipeline for
                    Router narrowing and Generator Level-1 candidates

  Implemented but not yet scheduled in the runtime
    reconciler.py   vendor-approved universe vs. live Prometheus status
                    reconciliation. Covered by tests, but nothing in app/
                    calls it, so every committed entry is still `approved`.
"""

from app.catalog.schema import (
    Catalog,
    CatalogEntry,
    CatalogSchemaError,
    CatalogStatus,
    MetricType,
    Priority,
)
from app.catalog.loader import CatalogIndex, CatalogLoadError, load_catalog

__all__ = [
    "Catalog",
    "CatalogEntry",
    "CatalogSchemaError",
    "CatalogStatus",
    "MetricType",
    "Priority",
    "CatalogIndex",
    "CatalogLoadError",
    "load_catalog",
]
