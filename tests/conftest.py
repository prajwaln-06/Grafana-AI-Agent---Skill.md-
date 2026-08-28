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
    )
