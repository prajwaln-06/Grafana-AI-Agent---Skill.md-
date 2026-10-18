"""app/catalog/runtime.py: cached/failure-tolerant loading, retrieval
decisions, scoped candidate metadata and prompt formatting."""

import json
from collections import Counter
from pathlib import Path

import pytest

from app.catalog import runtime as rt
from app.config import Settings

REPO = Path(__file__).resolve().parent.parent
REAL_CATALOG = REPO / "app" / "catalog" / "catalog.json"
SKILLS = REPO / "skills"
MIN_SCORE = Settings.model_fields["catalog_narrow_min_score"].default


@pytest.fixture(autouse=True)
def _fresh_cache():
    rt.reset_structured_catalog_cache()
    yield
    rt.reset_structured_catalog_cache()


def _settings(catalog_path=REAL_CATALOG):
    return Settings(gemini_api_key="test-key-not-real", skills_root=SKILLS, catalog_path=catalog_path)


@pytest.fixture()
def catalog():
    cat = rt.get_structured_catalog(_settings())
    assert cat is not None
    return cat


def _write_catalog(path: Path, metric_count: int | None = None) -> None:
    data = json.loads(REAL_CATALOG.read_text(encoding="utf-8"))
    if metric_count is not None:
        data["metrics"] = data["metrics"][:metric_count]
    path.write_text(json.dumps(data), encoding="utf-8")


# ---- configuration ----------------------------------------------------------------


def test_catalog_settings_defaults_and_separation_from_dashboard_catalog():
    fields = Settings.model_fields
    assert fields["catalog_path"].default == Path("app/catalog/catalog.json")
    assert fields["catalog_path"].default != Path("app/catalog.json")
    assert fields["catalog_narrow_min_score"].default == 2.0
    assert fields["catalog_metric_status_validation_enabled"].default is False


# ---- loading ----------------------------------------------------------------------


def test_real_catalog_loads_with_expected_contents(catalog):
    # 43 metrics, all approved: 16 Node Exporter + 27 DCGM Exporter. If the
    # source data legitimately changes, update these numbers deliberately.
    assert catalog.catalog_version == "1.0"
    assert len(catalog.metrics) == 43
    assert {m.status for m in catalog.metrics} == {"approved"}
    assert Counter(m.exporter for m in catalog.metrics) == {"node-exporter": 16, "dcgm-exporter": 27}


def test_missing_catalog_returns_none_without_raising(tmp_path):
    assert rt.get_structured_catalog(_settings(tmp_path / "nope.json")) is None


@pytest.mark.parametrize("content", ["{not json", "[]", '{"unexpected": 1}', ""])
def test_malformed_catalog_returns_none_without_raising(tmp_path, content):
    bad = tmp_path / "catalog.json"
    bad.write_text(content, encoding="utf-8")
    assert rt.get_structured_catalog(_settings(bad)) is None


def test_catalog_is_cached_and_not_reread_on_every_call(monkeypatch):
    calls = []
    real_load = rt.load_catalog
    monkeypatch.setattr(rt, "load_catalog", lambda p: (calls.append(p), real_load(p))[1])
    settings = _settings()
    first = rt.get_structured_catalog(settings)
    second = rt.get_structured_catalog(settings)
    assert first is second and len(calls) == 1
    rt.reset_structured_catalog_cache()
    rt.get_structured_catalog(settings)
    assert len(calls) == 2


def test_a_failed_load_is_cached_so_it_is_not_retried_every_request(tmp_path, monkeypatch):
    calls = []
    real_load = rt.load_catalog
    monkeypatch.setattr(rt, "load_catalog", lambda p: (calls.append(p), real_load(p))[1])
    settings = _settings(tmp_path / "missing.json")
    assert rt.get_structured_catalog(settings) is None
    assert rt.get_structured_catalog(settings) is None
    assert len(calls) == 1


def test_reload_picks_up_changes_and_keeps_previous_catalog_on_a_bad_edit(tmp_path):
    path = tmp_path / "catalog.json"
    _write_catalog(path, metric_count=5)
    settings = _settings(path)
    assert len(rt.get_structured_catalog(settings).metrics) == 5

    _write_catalog(path, metric_count=8)
    assert len(rt.get_structured_catalog(settings).metrics) == 5   # not live until reload
    assert len(rt.reload_structured_catalog(settings).metrics) == 8

    path.write_text("{broken", encoding="utf-8")
    kept = rt.reload_structured_catalog(settings)
    assert kept is not None and len(kept.metrics) == 8               # previous good one survives
    assert len(rt.get_structured_catalog(settings).metrics) == 8


def test_reload_with_no_previous_good_catalog_and_a_bad_file_is_unavailable(tmp_path):
    bad = tmp_path / "catalog.json"
    bad.write_text("{broken", encoding="utf-8")
    assert rt.reload_structured_catalog(_settings(bad)) is None


# ---- retrieval --------------------------------------------------------------------


def _names(decision):
    return [e.name for e in decision.candidates]


def test_cpu_question_ranks_node_cpu_metric_first(catalog):
    d = rt.catalog_metric_candidates(catalog, "What is the CPU utilization?", MIN_SCORE)
    assert d.confident and _names(d)[0] == "node_cpu_seconds_total"


def test_memory_question_returns_host_and_gpu_memory_candidates(catalog):
    d = rt.catalog_metric_candidates(catalog, "How much memory is being used?", MIN_SCORE)
    names = _names(d)
    assert d.confident
    assert "DCGM_FI_DEV_FB_USED" in names
    assert any(n.startswith("node_memory_") for n in names)


def test_gpu_temperature_question_ranks_gpu_temp_first(catalog):
    d = rt.catalog_metric_candidates(catalog, "GPU temperature", MIN_SCORE)
    assert d.confident and _names(d)[0] == "DCGM_FI_DEV_GPU_TEMP"


def test_unrelated_question_is_a_clean_miss(catalog):
    d = rt.catalog_metric_candidates(catalog, "tell me a joke about cheese", MIN_SCORE)
    assert not d.confident and d.candidates == ()


def test_hit_below_the_confidence_floor_is_treated_as_a_miss(catalog):
    d = rt.catalog_metric_candidates(catalog, "CPU utilization", min_confident_score=10_000)
    assert not d.confident and d.candidates == () and d.top_score is not None


def test_narrowing_decision_suggests_reference_paths(catalog):
    n = rt.catalog_narrowing_candidates(catalog, "GPU temperature", MIN_SCORE)
    assert n.confident and "references/dcgm-exporter/thermal.md" in n.suggested_paths
    miss = rt.catalog_narrowing_candidates(catalog, "tell me a joke about cheese", MIN_SCORE)
    assert not miss.confident and miss.suggested_paths == frozenset()


# ---- scoped candidate metadata ----------------------------------------------------


def test_candidate_metadata_is_limited_to_the_requests_known_metrics(catalog):
    known = {"node_cpu_seconds_total", "node_load1", "node_load5"}
    md = rt.build_metric_metadata(catalog, "CPU utilization", known, MIN_SCORE)
    names = [m["name"] for m in md]
    assert names and names[0] == "node_cpu_seconds_total"
    assert set(names) <= known


def test_candidates_outside_the_matched_references_are_dropped(catalog):
    # Confident catalog hit (GPU temperature) but this request only opened
    # node-exporter metrics: nothing may leak through.
    known = {"node_cpu_seconds_total", "node_load1"}
    assert rt.build_metric_metadata(catalog, "GPU temperature", known, MIN_SCORE) == ()


def test_no_known_metrics_means_no_candidates(catalog):
    assert rt.build_metric_metadata(catalog, "CPU utilization", set(), MIN_SCORE) == ()


def test_candidate_metadata_exposes_only_identity_fields(catalog):
    md = rt.build_metric_metadata(catalog, "CPU utilization", {"node_cpu_seconds_total"}, MIN_SCORE)
    assert md and set(md[0]) == {"name", "type", "help", "unit"}


def test_prompt_block_is_advisory_and_leaks_no_internal_catalog_fields(catalog):
    md = rt.build_metric_metadata(catalog, "GPU temperature",
                                  {"DCGM_FI_DEV_GPU_TEMP", "DCGM_FI_DEV_MEMORY_TEMP"}, MIN_SCORE)
    block = rt.format_metric_metadata_for_prompt(md)
    for m in md:
        assert f"- {m['name']} (type: {m['type']}" in block
    assert "not a restriction" in block
    assert "reference Markdown remains authoritative" in block
    for leaked in ("reference_path", "keywords", "priority", "approved", "dimensions"):
        assert leaked not in block


def test_prompt_block_renders_missing_unit_as_n_a():
    block = rt.format_metric_metadata_for_prompt(
        ({"name": "m", "type": "gauge", "help": "h", "unit": None},)
    )
    assert "unit: n/a" in block


def test_status_lookup_covers_every_catalog_metric(catalog):
    status = rt.catalog_status_by_metric(catalog)
    assert len(status) == 43 and set(status.values()) == {"approved"}
