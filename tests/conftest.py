import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture(scope="session")
def skills_root() -> Path:
    return BACKEND_ROOT / "skills"


@pytest.fixture(scope="session")
def skill_index(skills_root):
    from app.skill_index import SkillIndex
    return SkillIndex.load(skills_root, BACKEND_ROOT / "app" / "catalog" / "catalog.json")


@pytest.fixture()
def settings(skills_root):
    from app.config import Settings
    return Settings(
        gemini_api_key="test-key-not-real",
        skills_root=skills_root,
        prometheus_url="http://localhost:9090",
        opensearch_url="http://localhost:9600",
        # Catalog participation is mandatory for the normal pipeline (see
        # app/pipeline.py's module docstring), so ordinary tests that don't
        # explicitly care about catalog-narrowed routing/candidates would
        # otherwise silently pick up the real, committed app/catalog/
        # catalog.json (a relative default path that resolves against the
        # repo root most test runs execute from) and get real Level-1/2/3
        # behavior layered onto whatever mocked Router/Generator responses
        # they use -- exactly the kind of incidental coupling this default
        # is meant to prevent. Point at a path that can never exist instead,
        # so `_get_catalog` degrades to None (logged once) and every test
        # using this fixture gets deterministic, catalog-free behavior
        # unless it explicitly opts in (see
        # tests/test_pipeline_catalog_integration.py's own `_settings`
        # helper and `real_catalog` fixture, which build a real catalog on
        # purpose for exactly that kind of test).
        catalog_path=skills_root.parent / "tests" / "__no_catalog_for_this_fixture__.json",
        alert_rule_creation_enabled=False,
        dependent_query_resolution_enabled=False,
    )


# ---- OpenSearch helpers (synthetic mapping fixtures; see tests/fixtures/opensearch/README.md) ----

_OS_FIXTURE_BY_INDEX = {
    "slingshot_crayfabrichealth-*": "mapping_fabric_health.json",
    "controllerlog-*": "mapping_controller_log.json",
    "log_iml-*": "mapping_ilo_events.json",
}


@pytest.fixture(scope="session")
def os_registry(skills_root):
    from app.opensearch_catalog.registry import DEFAULT_REGISTRY_PATH, load_registry
    return load_registry(DEFAULT_REGISTRY_PATH, skills_root=skills_root)


@pytest.fixture()
def os_ctx(os_registry):
    """Factory: os_ctx("controller_log", "ilo_events") -> OpenSearchContext built from the SYNTHETIC
    fixture mappings (a real cluster's mappings are not claimed)."""
    import json
    from app.opensearch_catalog.context import Limits, OpenSearchContext, PatternContext
    from app.opensearch_catalog.field_catalog import FieldCatalogCache, build_field_view

    def fetch(index_pattern):
        return json.loads((BACKEND_ROOT / "tests" / "fixtures" / "opensearch" / _OS_FIXTURE_BY_INDEX[index_pattern]).read_text())

    def make(*pattern_ids, limits=None):
        cache = FieldCatalogCache(fetch, ttl_seconds=60, stale_ttl_seconds=60, max_indices=50,
                                  failure_backoff_seconds=1)
        pats = {}
        for pid in pattern_ids or ("fabric_health", "controller_log", "ilo_events"):
            entry = os_registry.get(pid)
            pats[pid] = PatternContext(entry=entry, view=build_field_view(entry, cache.get(entry)))
        return OpenSearchContext(patterns=pats, limits=limits or Limits())

    return make
