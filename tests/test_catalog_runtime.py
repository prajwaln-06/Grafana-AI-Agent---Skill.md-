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


def test_prompt_block_states_candidates_are_eligible_and_leaks_no_internal_catalog_fields(catalog):
    md = rt.build_metric_metadata(catalog, "GPU temperature",
                                  {"DCGM_FI_DEV_GPU_TEMP", "DCGM_FI_DEV_MEMORY_TEMP"}, MIN_SCORE)
    block = rt.format_metric_metadata_for_prompt(md)
    for m in md:
        assert f"- {m['name']} (type: {m['type']}" in block
    # Restriction wording (as on `main`): eligible set, Markdown still authoritative, and the
    # escape hatch that the pipeline turns into a Level-2 retry.
    assert "currently eligible for selection" in block
    assert "respond with status 'unsupported_metric'" in block
    assert "reference Markdown above, which remains authoritative" in block
    for leaked in ("reference_path", "keywords", "priority", "approved", "dimensions"):
        assert leaked not in block


def test_level1_outcome_distinguishes_a_miss_from_a_reference_universe_mismatch(catalog):
    gpu = {"DCGM_FI_DEV_GPU_TEMP", "DCGM_FI_DEV_MEMORY_TEMP"}
    hit = rt.build_level1_outcome(catalog, "GPU temperature", gpu, MIN_SCORE)
    assert hit.metric_metadata and hit.reference_universe_mismatch is False

    miss = rt.build_level1_outcome(catalog, "zzz qqq xyzzy", gpu, MIN_SCORE)
    assert miss.metric_metadata == () and miss.reference_universe_mismatch is False

    # A CONFIDENT catalog hit whose candidates share nothing with the routed universe.
    mismatch = rt.build_level1_outcome(catalog, "GPU temperature", {"node_load1"}, MIN_SCORE)
    assert mismatch.metric_metadata == () and mismatch.reference_universe_mismatch is True

    # No Prometheus universe at all (e.g. an OpenSearch-only request): no opinion, never a mismatch.
    none = rt.build_level1_outcome(catalog, "GPU temperature", set(), MIN_SCORE)
    assert none.metric_metadata == () and none.reference_universe_mismatch is False


def test_level2_candidates_are_the_matched_references_metrics_within_the_known_universe(catalog):
    cpu_ref = {"reference_path": "references/node-exporter/cpu.md"}
    mem_ref = {"reference_path": "references/dcgm-exporter/memory.md"}
    known = {e.name for e in catalog.metrics}
    names = [m["name"] for m in rt.build_level2_metric_metadata(catalog, [cpu_ref, mem_ref], known)]
    assert names == [e.name for e in catalog.by_reference_path(cpu_ref["reference_path"])] + \
        [e.name for e in catalog.by_reference_path(mem_ref["reference_path"])]
    # Restricted to the opened Metric Directory, deduplicated, tolerant of junk input.
    assert [m["name"] for m in rt.build_level2_metric_metadata(catalog, [cpu_ref, cpu_ref], {"node_load1"})] \
        == ["node_load1"]
    assert rt.build_level2_metric_metadata(None, [cpu_ref], known) == ()
    assert rt.build_level2_metric_metadata(catalog, [{"no": "path"}], known) == ()


def test_prompt_block_renders_missing_unit_as_n_a():
    block = rt.format_metric_metadata_for_prompt(
        ({"name": "m", "type": "gauge", "help": "h", "unit": None},)
    )
    assert "unit: n/a" in block


def test_status_lookup_covers_every_catalog_metric(catalog):
    status = rt.catalog_status_by_metric(catalog)
    assert len(status) == 43 and set(status.values()) == {"approved"}


# ---- narrowing false positives: measured behaviour on the real catalog -----------------

OFF_TOPIC = [
    "What is the weather in Paris",
    "What is the weather in Paris today?",
    "Tell me a joke",
    "What is the capital of France?",
    "Who won the world cup?",
    "what is the stock price of Apple",
    "Is it going to rain tomorrow?",
    "book me a flight to Delhi",
]


@pytest.mark.parametrize("question", OFF_TOPIC)
def test_off_topic_questions_are_clean_misses_not_confident_narrowing(catalog, question):
    """Regression for a measured false positive: 'What is the weather in Paris'
    scored 2.1 (>= the 2.0 floor) purely from function words ('what', 'is',
    'the', 'in') matching metric HELP prose. With function words ignored on
    the query side these are true catalog misses (no candidates at all), so
    the pipeline keeps the full Section 4 and gives the Generator no block."""
    assert rt.catalog_narrowing_candidates(catalog, question, MIN_SCORE).confident is False
    assert rt.catalog_metric_candidates(catalog, question, MIN_SCORE).candidates == ()
    known = {e.name for e in catalog.metrics}
    out = rt.build_level1_outcome(catalog, question, known, MIN_SCORE)
    assert out.metric_metadata == () and out.reference_universe_mismatch is False


@pytest.mark.parametrize("question, expected_first", [
    ("What is the CPU utilization?", "node_cpu_seconds_total"),
    ("How much memory is available?", "node_memory_MemAvailable_bytes"),
    ("What is the GPU temperature?", "DCGM_FI_DEV_GPU_TEMP"),
])
def test_clear_cpu_memory_and_gpu_questions_still_narrow_with_the_right_metric_on_top(
        catalog, question, expected_first):
    decision = rt.catalog_metric_candidates(catalog, question, MIN_SCORE)
    assert decision.confident and decision.top_score >= MIN_SCORE
    assert decision.candidates[0].name == expected_first


def test_multi_metric_question_gets_candidates_for_every_part(catalog):
    names = {e.name for e in rt.catalog_metric_candidates(
        catalog, "CPU utilization and GPU temperature", MIN_SCORE).candidates}
    assert "node_cpu_seconds_total" in names and "DCGM_FI_DEV_GPU_TEMP" in names


def test_ambiguous_question_is_confident_but_spans_both_exporters(catalog):
    decision = rt.catalog_metric_candidates(catalog, "memory usage", MIN_SCORE)
    exporters = {e.exporter for e in decision.candidates}
    assert decision.confident and exporters == {"node-exporter", "dcgm-exporter"}


def test_every_project_question_still_clears_the_confidence_floor(catalog):
    """The stopword fix must not cost genuine questions: all 46 questions the
    retrieval evaluation takes from the vendor document stay confident."""
    from scripts.evaluate_catalog_retrieval import QUESTION_SET

    weak = []
    for item in QUESTION_SET:
        question = item[0] if isinstance(item, tuple) else item.question
        if not rt.catalog_narrowing_candidates(catalog, question, MIN_SCORE).confident:
            weak.append(question)
    assert weak == []
