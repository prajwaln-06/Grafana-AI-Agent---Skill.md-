"""
app/catalog/

Structured metric registry + retrieval/indexing layer (metric-catalogization
work, per the frozen catalog architecture). This package is ordinary backend
infrastructure -- not an ADK Agent, not an LLM knowledge dump, not a
replacement for SKILL.md/Router/Generator/Validator.

Phase 1 (current): schema.py + loader.py only -- defines what a catalog
entry looks like and how to load a catalog.json file into memory. Nothing
in this package is wired into app/pipeline.py, app/validator.py, or
app/skill_index.py yet; that begins in later phases (9-13) once catalog
generation (Phase 2+), reconciliation (Phase 4), and retrieval (Phase 6)
exist and have been proven in shadow mode (Phase 8).
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
