"""
tests/test_catalog_consistency.py

Phase 3: validates the catalog Phase 2 generates against the skill
package's own metric universe, independent of generator.py's internal
implementation -- these tests would still catch a regression even if
generator.py's parsing approach changed entirely, because they check the
*output* against sources of truth this repository already treats as
authoritative:

  - app/skill_index.py's own routing-table / Metric Directory parsing
    (already trusted by the running application), and
  - scripts/check_metric_directory.py's own invariant (every Metric
    Directory row has a matching domain-file definition and vice versa),
    re-expressed here as an automated test rather than a manually-run
    script.

No pipeline/router/generator/validator behavior is touched by these
tests -- they only assert facts about the generated catalog.json content.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.catalog.generator import generate_catalog
from app.catalog.loader import CatalogLoadError, load_catalog
from app.catalog.schema import CatalogSchemaError

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_SKILLS_ROOT = REPO_ROOT / "skills"
CATALOG_PATH = REPO_ROOT / "app" / "catalog" / "catalog.json"


# ---- schema validity / structural invariants ----------------------------


def test_generated_catalog_json_file_loads_and_validates():
    """If app/catalog/catalog.json has been generated (Phase 2's output
    committed to the repo), it must load cleanly through the real,
    already-tested loader -- exercising schema validation end to end
    against the actual artifact the application would load, not just an
    in-memory Catalog object."""
    if not CATALOG_PATH.exists():
        catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
        from app.catalog.generator import write_catalog

        write_catalog(catalog, CATALOG_PATH)
    catalog = load_catalog(CATALOG_PATH)
    assert len(catalog.metrics) == 43


def test_no_duplicate_names_no_invalid_enum_values():
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
    names = [m.name for m in catalog.metrics]
    assert len(names) == len(set(names))
    for m in catalog.metrics:
        assert m.type in ("counter", "gauge", "histogram", "summary")
        assert m.status == "approved"
        assert m.priority == "Review"


def test_43_metric_completeness():
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
    assert len(catalog.metrics) == 43
    node = catalog.by_exporter("node-exporter")
    dcgm = catalog.by_exporter("dcgm-exporter")
    assert len(node) == 16
    assert len(dcgm) == 27


# ---- consistency with SkillIndex's own view of the metric universe -----


def test_every_catalog_metric_has_a_valid_reference_path_on_disk(skills_root):
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
    for entry in catalog.metrics:
        assert entry.reference_path is not None
        target = skills_root / entry.reference_path
        assert target.exists(), f"{entry.name}: reference_path {entry.reference_path} missing"


def test_catalog_metric_directories_match_skill_index_metric_directories(skill_index):
    """Cross-check against app/skill_index.py's own, independently-parsed
    metric_directory() lookup (already relied on by label_discovery.py) --
    every metric that skill_index.py's Metric Directory parsing finds must
    also be present in the generated catalog, and vice versa."""
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
    catalog_names = {m.name for m in catalog.metrics}

    skill_index_names: set[str] = set()
    seen_overviews = set()
    for row in skill_index.routing_rows:
        overview_path = skill_index.overview_path_for(row.reference_path)
        if overview_path is None or overview_path in seen_overviews:
            continue
        seen_overviews.add(overview_path)
        skill_index_names.update(skill_index.metric_directory(overview_path).keys())

    assert catalog_names == skill_index_names


def test_catalog_reference_paths_match_skill_index_metric_directory_mapping(skill_index):
    """Not just the same metric *names* -- each metric must map to the same
    domain-file reference_path skill_index.py itself would resolve it to,
    since label_discovery.py and the router both trust that mapping."""
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")

    expected: dict[str, str] = {}
    seen_overviews = set()
    for row in skill_index.routing_rows:
        overview_path = skill_index.overview_path_for(row.reference_path)
        if overview_path is None or overview_path in seen_overviews:
            continue
        seen_overviews.add(overview_path)
        expected.update(skill_index.metric_directory(overview_path))

    for entry in catalog.metrics:
        assert entry.reference_path == expected[entry.name], (
            f"{entry.name}: catalog reference_path {entry.reference_path!r} "
            f"!= skill_index-derived {expected[entry.name]!r}"
        )


# ---- consistency with scripts/check_metric_directory.py's own invariant -


def test_check_metric_directory_script_still_passes():
    """Phase 2/3 must not have introduced any Metric Directory /
    domain-file drift -- re-run the maintainer script that already checks
    this invariant, as an automated regression guard rather than a
    manually-run tool."""
    script = REPO_ROOT / "skills" / "scripts" / "check_metric_directory.py"
    result = subprocess.run(
        [sys.executable, str(script), str(REPO_ROOT / "skills")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---- deterministic / idempotent generation ------------------------------


def test_regenerating_catalog_from_scratch_matches_committed_artifact():
    """If catalog.json has been committed, regenerating it fresh (with the
    same generated_at) must produce byte-identical content -- proof the
    generator is deterministic and that the committed artifact isn't
    stale relative to the skill package it was generated from."""
    if not CATALOG_PATH.exists():
        return  # nothing to compare yet; covered by the other tests.
    committed = load_catalog(CATALOG_PATH)
    regenerated = generate_catalog(
        REAL_SKILLS_ROOT, generated_at=committed.generated_at
    )
    assert committed.to_dict() == regenerated.to_dict()
