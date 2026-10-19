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
    return SkillIndex.load(skills_root)


@pytest.fixture()
def settings(skills_root):
    from app.config import Settings
    return Settings(
        gemini_api_key="test-key-not-real",
        skills_root=skills_root,
        prometheus_url="http://localhost:9090",
        opensearch_url="http://localhost:9600",
        # The structured metric catalog participates in the normal pipeline by
        # default (Router narrowing + Level 1/2/3), so ordinary tests that use
        # mocked Router/Generator responses and do not care about it would
        # otherwise pick up the real, committed app/catalog/catalog.json and
        # get catalog-driven retries layered onto their canned LLM replies.
        # Point at a path that can never exist so the catalog degrades to
        # "unavailable" (logged once) and those tests stay deterministic and
        # catalog-free. Catalog tests opt in explicitly (see
        # tests/test_pipeline_catalog_integration.py's `_settings`/`real_catalog`).
        catalog_path=skills_root.parent / "tests" / "__no_catalog_for_this_fixture__.json",
    )
