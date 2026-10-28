"""
app/catalog/loader.py

Reads a catalog.json file from disk into a validated, in-memory Catalog
(schema.py) -- the same "load once, fail loudly, reuse across requests"
pattern app/skill_index.py::SkillIndex.load already establishes for
SKILL.md.

This module has exactly one responsibility: turning a catalog.json file on
disk into a validated Catalog object. It does NOT:
  - generate catalog content (that's scripts/generate_production_catalog.py,
    via vendor_docx.py / vendor.py)
  - perform candidate retrieval/ranking (that's search.py)
  - apply keyword/priority rules (that's rules.py, applied by the caller)
  - reconcile against Prometheus/vendor data (that's reconciler.py)

Callers of load_catalog():
  - app/pipeline.py::_get_catalog -- the request path. Catches
    CatalogLoadError and degrades to "no catalog"; on success it applies
    rules.apply_rules() in memory and caches the result process-wide.
  - app/skill_index.py::SkillIndex.metric_directory -- reads the exporter's
    metric universe from the catalog when an overview.md has no legacy
    Metric Directory table.
  - scripts/evaluate_catalog_retrieval.py -- offline retrieval measurement.
app/validator.py does not import this module; it receives catalog statuses
as a plain {metric_name: status} dict built by app/pipeline.py.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.catalog.schema import Catalog, CatalogSchemaError

logger = logging.getLogger(__name__)


class CatalogLoadError(ValueError):
    """Raised when catalog.json is missing, unreadable, not valid JSON, or
    fails schema validation. Deliberately distinct from CatalogSchemaError
    (schema.py) so callers/tests can tell "the file itself is the problem"
    apart from "the content inside a well-formed file is invalid" --
    though in practice both are treated the same way by callers: fail
    closed, never fall back to an empty or partially-loaded catalog.

    This mirrors SkillIndexError's role in app/skill_index.py: a broken
    catalog is a configuration problem the caller should surface loudly,
    not something to silently paper over with an empty catalog (which
    would make every catalog-assisted lookup silently behave as a
    catalog miss -- see the mandatory catalog-miss fallback rule -- and
    could mask a real deployment problem as normal fallback behavior).
    """


def load_catalog(path: Path) -> Catalog:
    """Loads and validates the catalog.json file at `path`.

    Raises CatalogLoadError -- never returns a partially-valid Catalog --
    if:
      - the file does not exist,
      - the file cannot be read (permissions, I/O error),
      - the file's contents are not valid JSON,
      - the JSON does not satisfy schema.py's validation (missing required
        fields, an enum value outside its allowed set, or a duplicate
        metric name -- Catalog.__post_init__ itself rejects duplicates).
    """
    if not path.exists():
        raise CatalogLoadError(
            f"Catalog file not found: {path}. Set the catalog path to the "
            f"file produced by the catalog generator (see app/catalog/"
            f"generator.py, introduced in a later phase)."
        )
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise CatalogLoadError(f"Could not read catalog file {path}: {e}") from e

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise CatalogLoadError(
            f"Catalog file {path} is not valid JSON: {e}"
        ) from e

    if not isinstance(data, dict):
        raise CatalogLoadError(
            f"Catalog file {path} must contain a JSON object at the top "
            f"level, got {type(data).__name__}."
        )

    try:
        catalog = Catalog.from_dict(data)
    except CatalogSchemaError as e:
        raise CatalogLoadError(
            f"Catalog file {path} failed schema validation: {e}"
        ) from e

    logger.info(
        "Loaded catalog %s (version=%s, generated_at=%s, metrics=%d) from %s",
        path.name,
        catalog.catalog_version,
        catalog.generated_at,
        len(catalog.metrics),
        path,
    )
    return catalog


class CatalogIndex:
    """Load-once wrapper around a loaded Catalog, with a reload() that keeps
    the previous catalog if the new file is invalid. Mirrors the role
    SkillIndex plays for SKILL.md.

    NOTE: currently NOT used by the request path. It was introduced in
    Phase 1 as a stable object for later integration phases to depend on.
    The request path instead reads the immutable snapshot published by
    app/catalog/runtime.py's CatalogRuntime (via pipeline._get_catalog and
    SkillIndex.metric_directory), which also merges review decisions and
    live reconciliation -- a CatalogIndex over catalog.json alone could not.
    Nothing in app/ constructs a CatalogIndex; it is retained, and covered
    by tests/test_catalog_loader.py.
    """

    def __init__(self, catalog: Catalog, path: Path):
        self._catalog = catalog
        self._path = path

    @classmethod
    def load(cls, path: Path) -> "CatalogIndex":
        return cls(load_catalog(path), path)

    def reload(self) -> None:
        """Re-reads the catalog from the same path this index was
        constructed with. Raises CatalogLoadError and leaves the
        previously-loaded catalog in place if the new file is invalid --
        the same "don't tear down a working state for a broken reload"
        behavior a caller would want from any hot-reload path, matching
        the spirit (not the exact mechanism) of ObservabilityQueryBuilder
        Agent.reload_skill()'s existing reload flow.
        """
        self._catalog = load_catalog(self._path)

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    @property
    def path(self) -> Path:
        return self._path
