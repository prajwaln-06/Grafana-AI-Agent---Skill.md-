"""
tests/test_pipeline_catalog_integration.py

Pipeline-level tests for the final Catalog -> Router -> Generator
architecture (catalog participation is mandatory; see app/pipeline.py's
module docstring):

  - Router-level narrowing: SKILL.md Section 4 is narrowed per-question to
    catalog-suggested domain rows whenever a catalog is available, always
    preserving rows the catalog doesn't cover, and always falling back to
    the full Section 4 on a catalog miss/low-confidence hit or when no
    catalog is available at all.
  - Generator-level candidates (Level 1) and the Level-1/2/3 fallback
    chain: `GeneratorContext.metric_metadata`, candidate-scope
    enforcement, and the Level-2/Level-3 escalation orchestration.

Every test here mocks llm_client.call_llm_json (same convention as
tests/test_pipeline.py) so no real LLM or Prometheus call happens; tests
that need a real catalog load it from the real, generated
app/catalog/catalog.json via the `real_catalog` fixture and pass it
explicitly to run_pipeline. Tests that need to exercise the
"catalog genuinely unavailable" degradation path patch
`catalog_runtime.get_structured_catalog` to return None (or point `catalog_path` at a
nonexistent file) rather than relying on a feature flag -- there is no
longer a flag that disables catalog participation; participation is
mandatory, and "unavailable" is the only way to get the pre-catalog
behavior.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

import inspect

from app import label_discovery, llm_client, pipeline, validator
from app.catalog import runtime as catalog_runtime
from app.catalog import search as catalog_search
from app.catalog.generator import generate_catalog
from app.catalog.rules import apply_rules
from app.catalog.schema import Catalog, CatalogEntry
from app.catalog.search import SearchResult
from app.config import Settings


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    """catalog_runtime.get_structured_catalog caches module-globally; make sure one test's
    cached catalog (or cached load-failure) never leaks into another."""
    catalog_runtime.reset_structured_catalog_cache()
    yield
    catalog_runtime.reset_structured_catalog_cache()


@pytest.fixture
def real_catalog(skills_root):
    return apply_rules(generate_catalog(skills_root, generated_at="t"))


@pytest.fixture
def production_catalog(skills_root):
    """The actual post-cutover production Catalog: loads the committed
    app/catalog/catalog.json (vendor-source-generated -- see
    scripts/generate_production_catalog.py) and applies the same
    load-time enrichment app/catalog/runtime.py::get_structured_catalog() applies at
    request time (Phase 5 keyword/priority classification, plus the
    Batch 2 Option C Markdown-category keyword enrichment).

    Deliberately distinct from `real_catalog` above: that fixture
    regenerates a catalog from Markdown via the legacy generate_catalog()
    path, which is exactly right for tests exercising Router/Generator
    plumbing against name/reference_path (identical between the two
    generation paths -- see tests/test_generate_production_catalog.py's
    own diff coverage) but WRONG for any test making a claim about the
    real, currently-deployed production Catalog's category/keywords,
    which are vendor-source-derived and Option-C-enriched respectively.
    Use this fixture, not `real_catalog`, for the latter."""
    from app.catalog.generator import build_markdown_category_index
    from app.catalog.loader import load_catalog

    catalog_path = skills_root.parent / "app" / "catalog" / "catalog.json"
    markdown_index = build_markdown_category_index(skills_root)
    return apply_rules(load_catalog(catalog_path), markdown_category_index=markdown_index)


def _router_resp(matched_reference_path="references/node-exporter/cpu.md"):
    return MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": matched_reference_path, "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [],
    })


def _gen_resp():
    return MagicMock(parsed={
        "mode": "single", "status": "ok",
        "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
        "data_source": "prometheus", "query": "node_cpu_seconds_total",
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "test",
    })


def _settings(skills_root, **overrides):
    return Settings(
        gemini_api_key="test-key-not-real",
        skills_root=skills_root,
        prometheus_url="http://localhost:9090",
        opensearch_url="http://localhost:9600",
        **overrides,
    )


# ---- Router-level catalog narrowing (mandatory as of Batch 4; formerly
# gated behind catalog_assisted_routing_enabled, now unconditional whenever
# a catalog successfully loads) -----------------------------------------


def test_router_narrowing_falls_back_to_full_section4_when_catalog_unavailable(skill_index, skills_root):
    """If the catalog genuinely fails to load (or there is none), Router
    narrowing must degrade to exactly the pre-catalog full Section 4 --
    never an error, never a partially-built prompt."""
    settings = _settings(skills_root)
    full_section4 = skill_index.section("## 4.")

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_call, \
         patch.object(catalog_runtime, "get_structured_catalog", return_value=None), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings))

    router_call_prompt = mock_call.call_args_list[0].kwargs["prompt"]
    assert full_section4 in router_call_prompt


def test_router_narrowing_confident_hit_narrows_out_unrelated_domain_rows(skill_index, skills_root, real_catalog):
    settings = _settings(skills_root)

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_call, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu utilization question", skill_index, settings, catalog=real_catalog))

    router_call_prompt = mock_call.call_args_list[0].kwargs["prompt"]
    # Relevant row kept.
    assert "references/node-exporter/cpu.md" in router_call_prompt
    # A catalog-covered but unrelated domain row is narrowed out.
    assert "references/dcgm-exporter/reliability.md" not in router_call_prompt


def test_router_narrowing_always_keeps_rows_the_catalog_does_not_cover(skill_index, skills_root, real_catalog):
    """overview.md / *-fundamentals.md / execution-contract.md rows are
    never in any catalog entry's reference_path -- narrowing must never
    drop them, regardless of the question."""
    settings = _settings(skills_root)

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_call, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu utilization question", skill_index, settings, catalog=real_catalog))

    router_call_prompt = mock_call.call_args_list[0].kwargs["prompt"]
    for always_kept in (
        "references/node-exporter/overview.md",
        "references/dcgm-exporter/overview.md",
        "references/prometheus-fundamentals.md",
        "references/opensearch-fundamentals.md",
        "references/execution-contract.md",
    ):
        assert always_kept in router_call_prompt


def test_router_narrowing_catalog_miss_falls_back_to_full_section4(skill_index, skills_root, real_catalog):
    settings = _settings(skills_root)
    full_section4 = skill_index.section("## 4.")

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_call, \
         patch.object(catalog_search, "search", return_value=[]), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("completely unrelated query xyz", skill_index, settings, catalog=real_catalog))

    router_call_prompt = mock_call.call_args_list[0].kwargs["prompt"]
    assert full_section4 in router_call_prompt


def test_router_narrowing_relies_on_searchs_own_default_status_filter(skill_index, skills_root, real_catalog):
    """Pending-review metrics must not become eligible for query
    generation via catalog-narrowed routing. Rather than re-implementing a
    status filter in pipeline.py (a second source of truth),
    _maybe_narrow_section4 calls catalog_search.search() with no
    `statuses` override at all -- it inherits search.py's own default
    (approved + approved_unavailable, discovered_pending_review excluded)
    automatically, and would continue to if that default ever changes.

    Two search() calls happen for this request now (Router-level
    narrowing, then Generator-level Level-1 candidate construction --
    both mandatory as of Batch 4) -- this test checks the FIRST one
    (Router narrowing) specifically."""
    settings = _settings(skills_root)

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(catalog_search, "search", wraps=catalog_search.search) as spy_search, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu utilization question", skill_index, settings, catalog=real_catalog))

    assert spy_search.call_count == 2
    _, kwargs = spy_search.call_args_list[0]
    assert "statuses" not in kwargs


# ---- Uncapped-by-rank search + confidence gate for Router narrowing ----------
#
# Review finding carried forward from earlier batches: "a non-empty catalog
# search result is not necessarily a safe routing result." Two fixes,
# tested independently:
#   1. Narrowing must never truncate candidates by rank (search.py's
#      ordinary top_n=5 display cutoff is the wrong tool for a decision
#      about which Section 4 rows the Router is even allowed to see).
#   2. Narrowing must not act on a low-confidence top hit -- a non-empty
#      result whose best candidate barely scores anything is treated
#      exactly like a catalog miss (full, unnarrowed Section 4).


def test_router_narrowing_search_is_not_capped_by_default_top_n(skill_index, skills_root, real_catalog):
    """Regression guard: narrowing must search with an effectively
    unbounded top_n, never search.py's DEFAULT_TOP_N=5 -- a correct
    reference ranked 6th or worse must not be silently excluded from the
    narrowed Section 4 just because of where it landed in a ranking meant
    for user-facing candidate lists, not routing eligibility.

    Two search() calls happen for this request now (Router-level
    narrowing, then Generator-level Level-1 candidate construction --
    both mandatory as of Batch 4); this test checks the FIRST one
    (Router narrowing) specifically."""
    settings = _settings(skills_root)

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(catalog_search, "search", wraps=catalog_search.search) as spy_search, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu utilization question", skill_index, settings, catalog=real_catalog))

    assert spy_search.call_count == 2
    _, kwargs = spy_search.call_args_list[0]
    assert kwargs.get("top_n", 0) > 43  # larger than the entire real catalog


def test_router_narrowing_low_confidence_hit_falls_back_to_full_section4(skill_index, skills_root, real_catalog):
    """A non-empty search result whose top score never clears
    catalog_narrow_min_score must be treated exactly like a catalog miss.
    Simulated by stubbing search() to return one weak, low-scoring
    candidate rather than relying on a real question that happens to
    score low."""
    settings = _settings(skills_root, catalog_narrow_min_score=2.0)
    full_section4 = skill_index.section("## 4.")

    weak_entry = CatalogEntry(
        name="node_cpu_seconds_total", type="counter", category="cpu", priority="Medium",
        exporter="node-exporter", status="approved", reference_path="references/node-exporter/cpu.md",
    )
    weak_result = [SearchResult(entry=weak_entry, score=1.0)]  # below the 2.0 floor

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_call, \
         patch.object(catalog_search, "search", return_value=weak_result), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu utilization question", skill_index, settings, catalog=real_catalog))

    router_call_prompt = mock_call.call_args_list[0].kwargs["prompt"]
    assert full_section4 in router_call_prompt


def test_router_narrowing_high_confidence_hit_still_narrows(skill_index, skills_root, real_catalog):
    """Sanity check that the confidence gate doesn't neuter narrowing
    entirely -- a genuinely strong hit (well above the default floor) must
    still narrow."""
    settings = _settings(skills_root, catalog_narrow_min_score=2.0)

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_call, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu utilization question", skill_index, settings, catalog=real_catalog))

    router_call_prompt = mock_call.call_args_list[0].kwargs["prompt"]
    assert "references/node-exporter/cpu.md" in router_call_prompt
    assert "references/dcgm-exporter/reliability.md" not in router_call_prompt


def test_catalog_narrowing_candidates_true_miss_vs_low_confidence(real_catalog):
    """Unit-level check on the new pure decision function: both a true miss
    (zero candidates) and a low-confidence hit (candidates exist, none
    clear the floor) come back as confident=False, but are distinguishable
    via top_score for callers (e.g. the eval harness) that need to tell
    them apart."""
    true_miss = catalog_runtime.catalog_narrowing_candidates(real_catalog, "asdkjfhaskjdfh nonsense", min_confident_score=2.0)
    assert true_miss.confident is False
    assert true_miss.top_score is None
    assert true_miss.suggested_paths == frozenset()

    weak_entry = CatalogEntry(
        name="node_cpu_seconds_total", type="counter", category="cpu", priority="Medium",
        exporter="node-exporter", status="approved", reference_path="references/node-exporter/cpu.md",
    )
    with patch.object(catalog_search, "search", return_value=[SearchResult(entry=weak_entry, score=1.0)]):
        low_confidence = catalog_runtime.catalog_narrowing_candidates(real_catalog, "cpu", min_confident_score=2.0)
    assert low_confidence.confident is False
    assert low_confidence.top_score == 1.0
    assert low_confidence.suggested_paths == frozenset()


def test_catalog_narrowing_candidates_confident_hit_includes_all_non_capped_candidates(real_catalog):
    """A confident decision's suggested_paths must reflect every candidate
    search finds relevant (bounded only by score, never by rank) -- this is
    what test_assisted_routing_search_is_not_capped_by_default_top_n
    verifies at the call-site level; this checks the decision object
    itself carries the un-truncated set through."""
    decision = catalog_runtime.catalog_narrowing_candidates(real_catalog, "cpu utilization question", min_confident_score=2.0)
    assert decision.confident is True
    uncapped = catalog_search.search(real_catalog, "cpu utilization question", top_n=1_000_000)
    expected_paths = frozenset(r.entry.reference_path for r in uncapped if r.entry.reference_path)
    assert decision.suggested_paths == expected_paths


# ---- Batch 4: Phase 11 (Generator integration) ---------------------------------
#
# Central question: does the Router -> Generator flow already mean the
# Generator receives the correct, narrowed reference context, without the
# Generator needing to know anything about the catalog itself? Verified two
# ways: (1) structurally -- nothing in the Generator-context-building code
# path takes or references a Catalog/catalog module at all; (2) behaviorally
# -- the actual prompt text the Generator receives is built purely from
# skill_index.read_reference() for the Router's matched_references, which is
# already exactly what SkillIndex always provided, catalog or not.


def test_generator_context_builder_has_catalog_and_question_parameters():
    """Batch 1 (Phase 2) supersedes the old 'Phase 11: no code change
    required' decision: _build_generator_context now accepts `question`
    and `catalog`, both optional and defaulting to the pre-Batch-1 no-op
    values (`question=""`, `catalog=None`), so any caller that doesn't
    pass them gets byte-for-byte the old behavior (see
    test_generator_prompt_content_identical_with_and_without_catalog_generator_candidates
    below for the behavioral half of that guarantee)."""
    params = inspect.signature(pipeline._build_generator_context).parameters
    assert "catalog" in params
    assert params["catalog"].default is None
    assert "question" in params
    assert params["question"].default == ""


def test_generator_context_dataclass_has_metric_metadata_and_nothing_else_catalog_shaped():
    """`metric_metadata` (Batch 1) is the one, deliberately narrow,
    catalog-derived field GeneratorContext is allowed to carry. Every
    other catalog-internal concept -- keywords/priority/status/exporter/
    reference_path -- must still never appear here, and must never appear
    as a key inside `metric_metadata` entries themselves (see
    test_metric_metadata_dict_shape_is_exactly_name_type_help_unit below)."""
    field_names = {f for f in pipeline.GeneratorContext.__dataclass_fields__}
    assert "metric_metadata" in field_names
    assert pipeline.GeneratorContext().metric_metadata == ()
    disallowed = {"keywords", "priority", "status", "exporter", "reference_path", "catalog"}
    for name in field_names:
        if name == "metric_metadata":
            continue
        assert not disallowed & set(name.lower().split("_")), (
            f"GeneratorContext field {name!r} looks catalog-internal-shaped."
        )


def test_metric_metadata_dict_shape_is_exactly_name_type_help_unit():
    entry = CatalogEntry(
        name="node_cpu_seconds_total", type="counter", category="CPU", priority="High",
        exporter="node-exporter", status="approved", help="CPU time spent in different modes.",
        unit="seconds", keywords=("cpu", "seconds"), reference_path="references/node-exporter/cpu.md",
    )
    metadata = catalog_runtime.metric_metadata_dict(entry)
    assert metadata == {
        "name": "node_cpu_seconds_total", "type": "counter",
        "help": "CPU time spent in different modes.", "unit": "seconds",
    }
    assert set(metadata) == {"name", "type", "help", "unit"}


def test_generator_prompt_content_identical_regardless_of_catalog_availability(
    skill_index, skills_root, real_catalog,
):
    """Behavioral confirmation: once the Router has settled on
    matched_references (regardless of whether catalog-narrowed routing
    helped it get there), the Generator's own prompt content for those
    matched references -- everything EXCEPT the candidate-metadata block,
    which requires a catalog by definition -- is built identically. This
    is the actual evidence that Router -> Generator already carries the
    correct narrowed context without any Generator-side catalog
    awareness for the Markdown/labels/attributes portions of the prompt."""
    router_resp = _router_resp()

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, _gen_resp()]) as mock_no_catalog, \
         patch.object(catalog_runtime, "get_structured_catalog", return_value=None), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, _settings(skills_root)))

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_with_catalog, \
         patch("app.catalog.runtime.catalog_metric_candidates",
               return_value=catalog_runtime.MetricCandidateDecision(confident=False, candidates=(), top_score=None)), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, _settings(skills_root), catalog=real_catalog))

    # The Router call's prompt legitimately differs (that's narrowing doing
    # its job) -- but the GENERATOR call (index 1) must be byte-for-byte
    # identical either way, since matched_references is identical (the mock
    # Router response) in both runs and Level-1 candidate construction was
    # forced to a miss above (so no candidate block enters either prompt).
    generator_prompt_no_catalog = mock_no_catalog.call_args_list[1].kwargs["prompt"]
    generator_prompt_with_catalog = mock_with_catalog.call_args_list[1].kwargs["prompt"]
    assert generator_prompt_no_catalog == generator_prompt_with_catalog


# ---- Batch 1 (Phase 2): GeneratorContext.metric_metadata ------------------


def test_generator_candidates_absent_when_catalog_unavailable(skill_index, skills_root):
    """With no catalog available at all, `_build_generator_context` never
    populates `metric_metadata` -- the Generator prompt is byte-for-byte
    what it was before the catalog/Level-1/2/3 architecture existed."""
    settings = _settings(skills_root)
    router_resp = _router_resp()

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, _gen_resp()]) as mock, \
         patch.object(catalog_runtime, "get_structured_catalog", return_value=None), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings))

    generator_prompt = mock.call_args_list[1].kwargs["prompt"]
    assert "Catalog-identified candidate metrics" not in generator_prompt


def test_generator_candidates_on_confident_hit_adds_candidate_block(skill_index, skills_root, real_catalog):
    """Flag-on, confident catalog hit -> the Generator prompt gains an
    explicit candidate-metric block, and every candidate is a metric that
    also appears in the matched reference's own Metric Directory (the
    known_prometheus_metrics intersection _build_metric_metadata applies)."""
    settings = _settings(skills_root)
    router_resp = _router_resp()  # matches references/node-exporter/cpu.md

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, _gen_resp()]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings, catalog=real_catalog))

    generator_prompt = mock.call_args_list[1].kwargs["prompt"]
    assert "Catalog-identified candidate metrics" in generator_prompt
    assert "node_cpu_seconds_total" in generator_prompt

    # Never leaks catalog-internal fields into the rendered candidate
    # block specifically (the prompt legitimately contains an unrelated
    # "status:"/"reference_used" etc. elsewhere, from SKILL.md's own
    # Section 9 contract text -- so scope this check to the block itself).
    block_start = generator_prompt.index("Catalog-identified candidate metrics")
    block_end = generator_prompt.index("\n\n", block_start)
    candidate_block = generator_prompt[block_start:block_end]
    # Word-boundary match: the block legitimately contains the English word
    # "status" (Batch 2's "respond with status 'unsupported_metric'"
    # escape-hatch sentence) -- what must never appear is the catalog's
    # internal field NAME itself, e.g. "status:" or "reference_path:" as a
    # rendered key.
    for leaked in ("keywords:", "priority:", "exporter:", "reference_path"):
        assert leaked not in candidate_block
    assert "status:" not in candidate_block


def test_generator_candidates_on_catalog_miss_falls_back_to_no_block(skill_index, skills_root, real_catalog):
    """A question with genuinely zero lexical overlap against the catalog
    must degrade to no candidate block at all -- never an empty-but-
    present block, never an error."""
    settings = _settings(skills_root)
    router_resp = _router_resp()

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, _gen_resp()]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}), \
         patch("app.catalog.runtime.catalog_metric_candidates",
               return_value=catalog_runtime.MetricCandidateDecision(confident=False, candidates=(), top_score=None)):
        _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings, catalog=real_catalog))

    generator_prompt = mock.call_args_list[1].kwargs["prompt"]
    assert "Catalog-identified candidate metrics" not in generator_prompt


def test_reference_universe_mismatch_routes_directly_to_level3(skill_index, skills_root, real_catalog):
    """Batch 3 (Phase 5): a CONFIDENT catalog decision whose candidates
    share zero overlap with known_prometheus_metrics is a genuine
    Catalog/reference-universe mismatch, not an ordinary Level-1 miss --
    per the frozen architecture, this must skip Level 1/2 entirely
    (broadening metric_metadata WITHIN the same, already-mismatched,
    reference universe can never recover it) and go straight to Level 3
    (a fresh, catalog-unnarrowed routing pass), rather than silently
    running an unconstrained Level-1 Generator attempt against the
    original mismatched reference universe.

    (Before Batch 3, this same fixture only proved the Generator prompt
    had no candidate block -- true, but for the wrong reason once Level 3
    exists: without the extra mocked calls added here, the run would have
    stopped inside Level 3's OWN routing call the moment it hit a
    not-matched no-op, making the old single assertion pass trivially
    without exercising the Level-3 path at all.)"""
    settings = _settings(skills_root)
    router_resp = _router_resp()  # Level 1's routing call: matches references/node-exporter/cpu.md

    unrelated_entry = real_catalog.get("DCGM_FI_DEV_GPU_TEMP")
    fake_decision = catalog_runtime.MetricCandidateDecision(confident=True, candidates=(unrelated_entry,), top_score=9.0)

    level3_router_resp = _router_resp()  # Level 3's fresh, catalog-unnarrowed routing call
    gen_resp = _gen_resp()

    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, level3_router_resp, gen_resp]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}), \
         patch("app.catalog.runtime.catalog_metric_candidates", return_value=fake_decision):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings, catalog=real_catalog))

    # Level-1 router + Level-3 router + Level-3 Generator -- no Level-1/2
    # Generator call was ever made against the mismatched reference universe.
    assert mock.call_count == 3
    assert result["status"] == "ok"

    level3_router_prompt = mock.call_args_list[1].kwargs["prompt"]
    generator_prompt = mock.call_args_list[2].kwargs["prompt"]
    assert "Catalog-identified candidate metrics" not in level3_router_prompt
    assert "Catalog-identified candidate metrics" not in generator_prompt


def test_generator_candidates_does_not_narrow_known_prometheus_metrics(skill_index, skills_root, real_catalog):
    """known_prometheus_metrics (and therefore label discovery scope)
    stays at its existing, broader Router-selected-reference scope
    regardless of catalog availability -- metric_metadata narrows only
    what's shown to the Generator as a candidate, never the known-metrics
    set used for label discovery/validation."""
    settings = _settings(skills_root)
    matched = [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}]

    with patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        ctx_no_catalog = pipeline._build_generator_context(matched, skill_index, settings,
                                                             question="cpu utilization", catalog=None)
        ctx_with_catalog = pipeline._build_generator_context(matched, skill_index, settings,
                                                               question="cpu utilization", catalog=real_catalog)

    assert ctx_no_catalog.known_prometheus_metrics == ctx_with_catalog.known_prometheus_metrics
    assert ctx_no_catalog.metric_metadata == ()
    assert ctx_with_catalog.metric_metadata != ()


def test_generator_candidates_multi_reference_union(skill_index, skills_root, real_catalog):
    """Candidates spanning multiple matched references are all eligible to
    appear in metric_metadata -- no code path is scoped to a single
    reference_path."""
    settings = _settings(skills_root)
    matched = [
        {"reference_path": "references/dcgm-exporter/compute.md", "data_source": "prometheus"},
        {"reference_path": "references/dcgm-exporter/memory.md", "data_source": "prometheus"},
    ]

    with patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        ctx = pipeline._build_generator_context(
            matched, skill_index, settings,
            question="FP64 double precision workload and GPU memory used", catalog=real_catalog,
        )

    names = {m["name"] for m in ctx.metric_metadata}
    assert names, "expected at least one confident candidate across the two references"
    assert names <= ctx.known_prometheus_metrics


# ---- Batch 2: vendor-source cutover operational proof --------------------
#
# The currently attached vendor document's 43 metrics all already have
# matching semantic Markdown (see tests/test_generate_production_catalog.py
# ::test_every_current_vendor_metric_has_a_resolved_reference_path), so
# there is no REAL vendor-only metric to exercise this with today. Per
# Batch 2's objective 5 ("prove the actual operational goal"), these tests
# instead run a SYNTHETIC vendor document through the REAL production
# generation path end-to-end -- app.catalog.vendor_docx.parse_vendor_docx
# -> app.catalog.generator.build_markdown_reference_index (against the
# real skills_root, confirming no Markdown exists) ->
# app.catalog.vendor.build_catalog_from_vendor_records (the same function
# scripts/generate_production_catalog.py calls) -- producing a genuine
# reference_path=None CatalogEntry, not a hand-built one. That entry is
# then merged into a temporary catalog.json and driven through the REAL
# runtime code paths (SkillIndex.metric_directory()'s catalog fallback,
# catalog_runtime.get_structured_catalog/_build_generator_context,
# validator.validate_contract) -- not just an assertion that the metric
# exists in catalog.json. The real attached vendor document and the real
# skills_root/Markdown are never modified; see
# _write_synthetic_vendor_docx and vendor_only_catalog_and_skill_index
# below for exactly what's synthetic vs. real.


def _write_synthetic_vendor_docx(path, metric_name: str, category: str, help_text: str,
                                  ptype: str = "Counter") -> None:
    """Builds a minimal, synthetic vendor DOCX (real docx.Document, real
    table shape app/catalog/vendor_docx.py understands) containing exactly
    one Node Exporter metric row, for the sole purpose of exercising the
    REAL vendor parser end-to-end in a test without touching the actual
    attached vendor document (tests/fixtures/prometheus_metrics.docx) or
    inventing a real Markdown file for this metric. Table shape mirrors
    app/catalog/vendor_docx.py's own module docstring exactly (Category |
    <exporter column> | Prometheus Type | What it Measures | Typical...)."""
    from docx import Document

    doc = Document()
    table = doc.add_table(rows=2, cols=5)
    header = table.rows[0].cells
    header[0].text = "Category"
    header[1].text = "Node Exporter Metric"
    header[2].text = "Prometheus Type"
    header[3].text = "What it Measures"
    header[4].text = "Typical User Questions"
    row = table.rows[1].cells
    row[0].text = category
    row[1].text = metric_name
    row[2].text = ptype
    row[3].text = help_text
    row[4].text = "N/A -- synthetic test fixture, not a real user question."
    doc.save(str(path))


@pytest.fixture
def vendor_only_catalog_and_skill_index(skills_root, real_catalog, tmp_path):
    """A temporary catalog.json = the real 43 entries (from the actual
    committed production catalog) + one synthetic vendor-only entry,
    produced by running the REAL production generation path end-to-end
    on a synthetic vendor document:

        synthetic vendor DOCX
            -> app.catalog.vendor_docx.parse_vendor_docx (real parser)
            -> app.catalog.generator.build_markdown_reference_index
               (real Markdown-discovery join, against the REAL
               skills_root -- confirms no Markdown exists for this name)
            -> app.catalog.vendor.build_catalog_from_vendor_records
               (the same production Catalog-construction function
               scripts/generate_production_catalog.py uses)

    This is not a hand-built CatalogEntry -- it is the actual production
    generation code, run against a synthetic input, producing
    reference_path=None because build_markdown_reference_index()
    genuinely found no Markdown for this metric name (the real skills_root
    is never modified). The resulting entry is then merged into a real,
    already-generated Catalog and written to a temporary catalog.json, so
    SkillIndex.metric_directory()'s catalog-fallback path (app/skill_index.py)
    picks it up into known_prometheus_metrics exactly the way it would for
    a real vendor-only metric shipped in production catalog.json."""
    import json

    from app.catalog.generator import build_markdown_reference_index
    from app.catalog.vendor import VendorMetricRecord, build_catalog_from_vendor_records
    from app.catalog.vendor_docx import parse_vendor_docx
    from app.skill_index import SkillIndex

    synthetic_name = "node_vendor_only_synthetic_total"
    synthetic_category = "Vendor Synthetic Category"
    synthetic_help = "A synthetic vendor-only metric with no semantic Markdown, for Batch 2's no-Markdown acceptance test."

    docx_path = tmp_path / "synthetic_vendor_only.docx"
    _write_synthetic_vendor_docx(docx_path, synthetic_name, synthetic_category, synthetic_help)

    # Real vendor parser, run against the synthetic document.
    parse_result = parse_vendor_docx(docx_path)
    assert parse_result.records == (
        VendorMetricRecord(
            name=synthetic_name, type="Counter", category=synthetic_category,
            help=synthetic_help, exporter="node-exporter",
        ),
    )
    assert parse_result.rejected_rows == ()

    # Real Markdown-discovery join against the REAL skills_root -- proves
    # this metric genuinely has no Markdown, rather than assuming it.
    markdown_index = build_markdown_reference_index(skills_root)
    assert synthetic_name not in markdown_index

    # Real production Catalog-construction function (the same one
    # scripts/generate_production_catalog.py calls).
    synthetic_catalog = build_catalog_from_vendor_records(
        parse_result.records, markdown_index, generated_at="t"
    )
    vendor_only_entry = synthetic_catalog.get(synthetic_name)
    assert vendor_only_entry is not None
    assert vendor_only_entry.reference_path is None

    augmented = Catalog(
        catalog_version=real_catalog.catalog_version,
        generated_at=real_catalog.generated_at,
        metrics=real_catalog.metrics + (vendor_only_entry,),
    )
    catalog_path = tmp_path / "catalog_with_vendor_only.json"
    catalog_path.write_text(json.dumps(augmented.to_dict(), indent=2), encoding="utf-8")
    skill_index = SkillIndex.load(skills_root, catalog_path)
    return augmented, catalog_path, skill_index, vendor_only_entry


def test_no_markdown_vendor_metric_reaches_known_prometheus_metrics(
    vendor_only_catalog_and_skill_index, skills_root,
):
    """SkillIndex.metric_directory()'s catalog fallback (Batch 1's
    required runtime-safety fix) must surface the vendor-only metric into
    known_prometheus_metrics -- proving the vendor-only metric is
    discoverable by the SAME mechanism as any Markdown-documented metric,
    with no Markdown involved at all."""
    augmented, _path, skill_index, vendor_only_entry = vendor_only_catalog_and_skill_index
    matched = [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}]
    settings = _settings(skills_root)

    with patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        ctx = pipeline._build_generator_context(
            matched, skill_index, settings,
            question="vendor only test metric", catalog=augmented,
        )

    assert vendor_only_entry.name in ctx.known_prometheus_metrics


def test_no_markdown_vendor_metric_reaches_level1_candidate_metadata(
    vendor_only_catalog_and_skill_index, skills_root,
):
    """Once catalog_metric_candidates confidently surfaces the vendor-only
    metric (simulating real Catalog-search ranking it highest for a
    matching question), it must reach GeneratorContext.metric_metadata --
    i.e. Level 1 -- the same as any Markdown-documented candidate."""
    augmented, _path, skill_index, vendor_only_entry = vendor_only_catalog_and_skill_index
    matched = [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}]
    settings = _settings(skills_root)
    fake_decision = catalog_runtime.MetricCandidateDecision(
        confident=True, candidates=(vendor_only_entry,), top_score=9.0
    )

    with patch("app.label_discovery.discover_labels_for_metrics", return_value={}), \
         patch("app.catalog.runtime.catalog_metric_candidates", return_value=fake_decision):
        ctx = pipeline._build_generator_context(
            matched, skill_index, settings,
            question="vendor only test metric", catalog=augmented,
        )

    names = {m["name"] for m in ctx.metric_metadata}
    assert vendor_only_entry.name in names
    # Only the four allowed fields ever reach the Generator (see
    # _metric_metadata_dict) -- no keywords/priority/status/exporter/
    # reference_path leak through, even though this entry's
    # reference_path is None.
    entry_dict = next(m for m in ctx.metric_metadata if m["name"] == vendor_only_entry.name)
    assert set(entry_dict.keys()) == {"name", "type", "help", "unit"}


def test_no_markdown_vendor_metric_selected_by_generator_and_accepted_by_validator(
    vendor_only_catalog_and_skill_index, skills_root,
):
    """End-to-end (LLM mocked, per this whole test file's convention):
    Router matches an ordinary reference, catalog confidently surfaces
    the vendor-only metric as a Level-1 candidate, the (mocked) Generator
    selects it by name, and validator.validate_contract accepts it as a
    known metric -- proving the operational dependency on Markdown has
    actually been removed for a metric to be validly selected and
    validated, not just present in catalog.json."""
    augmented, _path, skill_index, vendor_only_entry = vendor_only_catalog_and_skill_index
    settings = _settings(skills_root)
    router_resp = _router_resp()  # matches references/node-exporter/cpu.md
    gen_resp = MagicMock(parsed={
        "mode": "single", "status": "ok",
        "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {
            "type": "raw_metric", "name": vendor_only_entry.name, "source_metrics": [],
        },
        "data_source": "prometheus", "query": vendor_only_entry.name,
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"},
        "explanation": "test",
    })
    fake_decision = catalog_runtime.MetricCandidateDecision(
        confident=True, candidates=(vendor_only_entry,), top_score=9.0
    )

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, gen_resp]), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}), \
         patch("app.catalog.runtime.catalog_metric_candidates", return_value=fake_decision):
        result = _run(pipeline.run_pipeline(
            "vendor only test metric", skill_index, settings, catalog=augmented,
        ))

    # status "ok" means validation passed (a real Prometheus connection is
    # not required for this -- see execution.execution_status, which is
    # allowed to be endpoint_error/not_executed in this sandboxed test
    # environment; "ok" specifically means the query was NOT rejected by
    # validate_contract's known_metrics/label checks).
    assert result.get("status") == "ok"
    assert result["measurement_used"]["name"] == vendor_only_entry.name


# ---- Batch 2: existing Markdown-backed metric regression ------------------


def test_existing_markdown_backed_metric_still_resolves_reference_and_candidates(
    skill_index, skills_root, real_catalog,
):
    """The opposite case (Batch 2 objective 6): a vendor metric WITH
    existing semantic Markdown continues to resolve its reference_path,
    remain available to Router/Generator, and reach metric_metadata
    exactly as before the vendor-source cutover."""
    settings = _settings(skills_root)
    entry = real_catalog.get("node_cpu_seconds_total")
    assert entry is not None
    # Post-cutover: category is the vendor category ("CPU"), not the
    # Markdown category ("CPU Utilization") -- Option C boundary.
    assert entry.reference_path == "references/node-exporter/cpu.md"

    router_resp = _router_resp()  # matches references/node-exporter/cpu.md

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, _gen_resp()]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline(
            "cpu utilization busy idle", skill_index, settings, catalog=real_catalog,
        ))

    assert result.get("status") == "ok"
    generator_prompt = mock.call_args_list[1].kwargs["prompt"]
    assert "node_cpu_seconds_total" in generator_prompt
    # Semantic Markdown (Use when / distinctions / etc.) is still present
    # in the prompt above the candidate block -- the candidate block is
    # additive, never a replacement (see _format_metric_metadata_for_prompt).
    assert "Catalog-identified candidate metrics" in generator_prompt


def test_existing_markdown_backed_metric_gets_category_enrichment_keywords(
    skills_root, production_catalog,
):
    """Option C: an existing Markdown-backed metric's Catalog category
    stays the vendor category, but its keywords are enriched with tokens
    from the Markdown Category -- proving retrieval keyword enrichment
    fires for exactly the case it's meant to (a metric with both a vendor
    record and existing semantic Markdown). Uses `production_catalog`
    (the real, committed, vendor-source-generated app/catalog/catalog.json
    + Option C enrichment) rather than a fresh Markdown-regenerated
    catalog, since this test's whole point is a claim about the actual
    production Catalog's behavior."""
    entry = production_catalog.get("node_cpu_seconds_total")

    assert entry.category != "CPU Utilization"  # still the vendor category
    assert "utilization" in entry.keywords       # enrichment fired


def test_existing_markdown_backed_metric_no_semantic_information_lost(skills_root):
    """Confirms Batch 2 did not remove semantic Markdown content: Category,
    Use when, and other semantic guidance are all still present in the
    domain file on disk for a metric that is also vendor-sourced."""
    cpu_md = (skills_root / "references" / "node-exporter" / "cpu.md").read_text(encoding="utf-8")
    assert "### `node_cpu_seconds_total`" in cpu_md
    assert "**Category:**" in cpu_md
    assert "Use when" in cpu_md or "**Use when" in cpu_md


def test_generator_candidates_alert_path_also_gets_metric_metadata(skill_index, skills_root, real_catalog):
    """_build_generator_context is called once, before the read/alert
    branch is even known -- metric_metadata should be populated
    identically regardless of action_intent."""
    settings = _alert_catalog_settings(skills_root)
    router_resp = _alert_router_resp()

    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, _alert_gen_resp(condition_query="node_cpu_seconds_total")]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("alert me if cpu exceeds 90% for 5m", skill_index, settings,
                                             catalog=real_catalog))

    assert result.get("status") == "alert_rule_proposed"
    generator_prompt = mock.call_args_list[1].kwargs["prompt"]
    assert "Catalog-identified candidate metrics" in generator_prompt


# ---- Batch 4: Phase 12 (Validator / known_metrics integration) -----------------


def test_catalog_is_always_loaded_for_normal_pipeline_by_default(skill_index, skills_root):
    """Batch 4 (Phase 9): catalog participation is mandatory for the
    normal query pipeline -- `_get_catalog` must be called even when no
    catalog-status-validation opt-in or explicit override is present,
    unlike the old (now-removed) migration flags that used to gate this."""
    settings = _settings(skills_root)
    assert settings.catalog_metric_status_validation_enabled is False

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(catalog_runtime, "get_structured_catalog", wraps=catalog_runtime.get_structured_catalog) as spy_get_catalog, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings))

    spy_get_catalog.assert_called_once()
    assert result["status"] == "ok"


def test_catalog_status_validation_off_passes_catalog_status_by_metric_none_to_validator(
    skill_index, skills_root, real_catalog,
):
    """The catalog is always loaded now (mandatory participation), but
    catalog-STATUS validation specifically stays independently opt-in --
    the Validator must not receive catalog-status information unless
    catalog_metric_status_validation_enabled ITSELF is True."""
    settings = _settings(skills_root, catalog_metric_status_validation_enabled=False)
    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(catalog_runtime, "get_structured_catalog", return_value=real_catalog), \
         patch.object(validator, "validate_contract", wraps=validator.validate_contract) as spy_validate, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings))

    assert result["status"] == "ok"
    _, kwargs = spy_validate.call_args
    assert kwargs.get("catalog_status_by_metric") is None


def test_catalog_status_validation_on_valid_metric_still_passes(skill_index, skills_root, real_catalog):
    """The real generated catalog's 43 metrics are all 'approved' -- turning
    Phase 12 on must not regress an ordinary successful query."""
    settings = _settings(skills_root, catalog_metric_status_validation_enabled=True)
    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(catalog_runtime, "get_structured_catalog", return_value=real_catalog), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings))

    assert result["status"] == "ok"


def test_catalog_status_validation_on_rejects_a_pending_review_metric_end_to_end(skill_index, skills_root):
    """Full pipeline wiring test: a metric that the Generator resolved to
    (and that IS present in the Metric Directory, so known_metrics alone
    would have accepted it) must still be rejected end-to-end when its
    catalog status is discovered_pending_review/rejected and Phase 12 is
    enabled -- this is the actual safety guarantee Phase 12 exists for."""
    pending_catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(
            CatalogEntry(
                name="node_cpu_seconds_total", type="counter", category="CPU Utilization",
                priority="Medium", exporter="node-exporter", status="discovered_pending_review",
                reference_path="references/node-exporter/cpu.md",
            ),
        ),
    )
    settings = _settings(skills_root, catalog_metric_status_validation_enabled=True)
    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(catalog_runtime, "get_structured_catalog", return_value=pending_catalog), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings))

    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert "discovered_pending_review" in result["explanation"]


def test_catalog_status_validation_catalog_load_failure_does_not_weaken_validation(
    skill_index, skills_root, tmp_path,
):
    """Requirement #9 at the pipeline level: a missing/corrupt catalog.json
    with Phase 12 enabled must degrade to known_metrics-only behavior (the
    request still succeeds), never crash and never silently reject."""
    settings = _settings(
        skills_root, catalog_metric_status_validation_enabled=True,
        catalog_path=tmp_path / "does-not-exist.json",
    )
    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings))

    assert result["status"] == "ok"


# ---- Batch 4: Phase 13 (alert known-metric validation) -------------------------


def _alert_catalog_settings(skills_root, **overrides):
    return _settings(
        skills_root, alert_rule_creation_enabled=True, grafana_url="http://localhost:3000",
        grafana_service_account_token="glsa_test", grafana_default_folder_uid="alerts-folder",
        **overrides,
    )


def _alert_router_resp():
    return MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [], "action_intent": "propose_alert_rule",
    })


def _alert_gen_resp(metric_name="node_cpu_seconds_total", condition_query=None):
    condition_query = condition_query or f'100 - (avg(rate({metric_name}{{mode="idle"}}[1m])) * 100)'
    return MagicMock(parsed={
        "mode": "single", "status": "alert_rule_proposed",
        "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": metric_name, "source_metrics": []},
        "data_source": "prometheus",
        "alert_rule": {
            "title": "High CPU utilization",
            "condition_query": condition_query,
            "comparison": {"operator": ">", "threshold": 90},
            "for_duration": "5m",
            "folder": None,
            "datasource_uid": None,
        },
        "explanation": "Derived from the verified idle-based CPU utilization expression. Proposal only.",
    })


def test_alert_proposal_for_unknown_metric_is_rejected_end_to_end(skill_index, skills_root):
    """Requirement #12 at the pipeline level -- this is the Phase 13 gap
    fix: previously an alert proposal for a fabricated metric name (not in
    any Metric Directory this request opened) would have passed validation
    entirely, since _validate_alert_rule_proposed never checked
    known_metrics at all."""
    settings = _alert_catalog_settings(skills_root)
    with patch.object(llm_client, "call_llm_json",
                       side_effect=[_alert_router_resp(), _alert_gen_resp(metric_name="totally_made_up_metric",
                                                                           condition_query="totally_made_up_metric > 90")]), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("alert me if made up metric exceeds 90", skill_index, settings))

    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert "fabricated" in result["explanation"]


def test_alert_proposal_for_valid_known_metric_still_succeeds_end_to_end(skill_index, skills_root):
    """Regression guard alongside the fix above: a legitimate alert
    proposal for a real, known metric must still reach
    alert_rule_proposed, not be caught by the new known_metrics check."""
    settings = _alert_catalog_settings(skills_root)
    with patch.object(llm_client, "call_llm_json", side_effect=[_alert_router_resp(), _alert_gen_resp()]), \
         patch("app.label_discovery.discover_labels_for_metrics",
               return_value={"node_cpu_seconds_total": ["mode"]}):
        result = _run(pipeline.run_pipeline("alert me if CPU exceeds 90%", skill_index, settings))

    assert result["status"] == "alert_rule_proposed"


def test_alert_proposal_for_rejected_catalog_metric_is_rejected_end_to_end(skill_index, skills_root):
    """Requirement #14 at the pipeline level: catalog status also gates the
    alert path when Phase 12's flag is enabled alongside alert creation."""
    rejected_catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(
            CatalogEntry(
                name="node_cpu_seconds_total", type="counter", category="CPU Utilization",
                priority="Medium", exporter="node-exporter", status="rejected",
                reference_path="references/node-exporter/cpu.md",
            ),
        ),
    )
    settings = _alert_catalog_settings(skills_root, catalog_metric_status_validation_enabled=True)
    with patch.object(llm_client, "call_llm_json", side_effect=[_alert_router_resp(), _alert_gen_resp()]), \
         patch.object(catalog_runtime, "get_structured_catalog", return_value=rejected_catalog), \
         patch("app.label_discovery.discover_labels_for_metrics",
               return_value={"node_cpu_seconds_total": []}):
        result = _run(pipeline.run_pipeline("alert me if CPU exceeds 90%", skill_index, settings))

    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert "rejected" in result["explanation"]


def test_alert_creation_disabled_still_forces_out_of_scope_with_catalog_status_validation_on(
    skill_index, skills_root,
):
    """Catalog integration (mandatory participation, plus the optional
    catalog-status-validation flag) must never bypass the existing
    alert_rule_creation_enabled defense-in-depth -- disabled means
    deterministic out_of_scope_action regardless."""
    settings = _settings(
        skills_root, alert_rule_creation_enabled=False,
        catalog_metric_status_validation_enabled=True,
    )
    assert settings.alert_rule_creation_enabled is False

    with patch.object(llm_client, "call_llm_json", return_value=_alert_router_resp()), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("alert me if CPU exceeds 90%", skill_index, settings))

    assert result["status"] == "out_of_scope_action"


# ---- Batch 2 (Phase 3/4): candidate-scope enforcement + Level-1 -> Level-2 --------
#
# All of these use the real catalog's actual behavior for the question "cpu
# utilization busy idle" against the single matched reference
# references/node-exporter/cpu.md: Level-1's confident candidate set (after
# the known_prometheus_metrics intersection) is exactly {node_cpu_seconds_total,
# node_context_switches_total, node_intr_total} -- deliberately EXCLUDING
# node_load1/node_load5/node_load15, even though all six metrics belong to
# the same reference_path and are all in known_prometheus_metrics. This gives
# a real (not synthetic) known-but-out-of-Level-1-scope metric (node_load1)
# to exercise the fallback with.


def _ok_entry_for(metric_name):
    return {
        "mode": "single", "status": "ok",
        "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": metric_name, "source_metrics": []},
        "data_source": "prometheus", "query": metric_name,
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "test",
    }


def _unsupported_metric_entry():
    return {
        "mode": "single", "status": "unsupported_metric",
        "reference_used": "references/node-exporter/cpu.md",
        "requested_measurement": "something not covered by the Level-1 candidates",
        "explanation": "None of the candidate metrics match this question.",
    }


def test_level1_success_never_triggers_level2(skill_index, skills_root, real_catalog):
    """A Generator response using a metric that IS inside the Level-1
    candidate scope must not cause any retry -- exactly 2 LLM calls total
    (Router + one Generator attempt)."""
    settings = _settings(skills_root)
    router_resp = _router_resp()
    gen_resp = MagicMock(parsed=_ok_entry_for("node_cpu_seconds_total"))

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, gen_resp]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings, catalog=real_catalog))

    assert mock.call_count == 2
    assert result["status"] == "ok"
    assert result["measurement_used"]["name"] == "node_cpu_seconds_total"


def test_candidate_scope_violation_triggers_level2_retry_and_succeeds(skill_index, skills_root, real_catalog):
    """Trigger #2: deterministic post-generation validation catches the
    Generator picking a known-but-out-of-Level-1-scope metric
    (node_load1). This must trigger exactly one Level-2 retry (3 LLM
    calls total: Router + 2 Generator attempts), and the Level-2 attempt
    -- which has no narrower scope than known_prometheus_metrics -- must
    accept the same metric this time."""
    settings = _settings(skills_root)
    router_resp = _router_resp()
    attempt1 = MagicMock(parsed=_ok_entry_for("node_load1"))  # known, but outside Level-1 scope
    attempt2 = MagicMock(parsed=_ok_entry_for("node_load1"))  # Level-2 retry, now accepted

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, attempt1, attempt2]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings, catalog=real_catalog))

    assert mock.call_count == 3
    assert result["status"] == "ok"
    assert result["measurement_used"]["name"] == "node_load1"

    # The Level-2 Generator prompt must contain all six cpu.md metrics, not
    # just the Level-1 three.
    level2_prompt = mock.call_args_list[2].kwargs["prompt"]
    for m in ("node_cpu_seconds_total", "node_context_switches_total", "node_intr_total",
              "node_load1", "node_load5", "node_load15"):
        assert m in level2_prompt


def test_unsupported_metric_status_triggers_level2_retry_and_succeeds(skill_index, skills_root, real_catalog):
    """Trigger #1: the Generator itself reports unsupported_metric for the
    Level-1 candidate set (a structurally VALID contract) -- must also
    trigger exactly one Level-2 retry."""
    settings = _settings(skills_root)
    router_resp = _router_resp()
    attempt1 = MagicMock(parsed=_unsupported_metric_entry())
    attempt2 = MagicMock(parsed=_ok_entry_for("node_load1"))

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, attempt1, attempt2]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings, catalog=real_catalog))

    assert mock.call_count == 3
    assert result["status"] == "ok"
    assert result["measurement_used"]["name"] == "node_load1"


def test_unsupported_metric_at_level2_escalates_to_level3_and_recovers(skill_index, skills_root, real_catalog):
    """Batch 3 (Phase 5)'s primary Level-2 -> Level-3 trigger: the
    Level-2 attempt (already the broadest metric_metadata this reference
    universe can offer) STILL reports unsupported_metric -- this must
    escalate to Level 3 (a fresh, catalog-unnarrowed routing pass), not
    return unsupported_metric as final the way Batch 2 alone would have.
    5 LLM calls total: Level-1 router, Level-1 Generator, Level-2
    Generator, Level-3 router, Level-3 Generator."""
    settings = _settings(skills_root)
    router_resp = _router_resp()
    attempt1 = MagicMock(parsed=_unsupported_metric_entry())
    attempt2 = MagicMock(parsed=_unsupported_metric_entry())
    level3_router_resp = _router_resp()
    level3_gen_resp = MagicMock(parsed=_ok_entry_for("node_load1"))

    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, attempt1, attempt2, level3_router_resp, level3_gen_resp]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings, catalog=real_catalog))

    assert mock.call_count == 5
    assert result["status"] == "ok"
    assert result["measurement_used"]["name"] == "node_load1"

    # Level 3's Generator attempt must be unconstrained -- no candidate block.
    level3_gen_prompt = mock.call_args_list[4].kwargs["prompt"]
    assert "Catalog-identified candidate metrics" not in level3_gen_prompt


def test_level3_reached_exactly_once_no_recursive_fallback(skill_index, skills_root, real_catalog):
    """If Level 3's OWN Generator attempt ALSO reports unsupported_metric,
    that is the final answer -- exactly 5 LLM calls, never a 6th. Level 3
    must never re-trigger itself or a further round of Level 2."""
    settings = _settings(skills_root)
    router_resp = _router_resp()
    attempt1 = MagicMock(parsed=_unsupported_metric_entry())
    attempt2 = MagicMock(parsed=_unsupported_metric_entry())
    level3_router_resp = _router_resp()
    level3_gen_resp = MagicMock(parsed=_unsupported_metric_entry())

    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, attempt1, attempt2, level3_router_resp, level3_gen_resp]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings, catalog=real_catalog))

    assert mock.call_count == 5  # never a 6th call
    assert result["status"] == "unsupported_metric"


def test_level3_gate_stop_returned_cleanly(skill_index, skills_root, real_catalog):
    """Level 3's fresh routing pass can independently produce a gate_stop
    (e.g. the question turns out to be genuinely out-of-scope) -- this
    must be returned cleanly via the normal gate_stop path, not treated as
    an error or retried further."""
    settings = _settings(skills_root)
    router_resp = _router_resp()
    attempt1 = MagicMock(parsed=_unsupported_metric_entry())
    attempt2 = MagicMock(parsed=_unsupported_metric_entry())
    level3_router_resp = MagicMock(parsed={
        "gate_stop": {"status": "declined", "reason": "nonsensical_input",
                      "explanation": "This is not an observability question."},
    })

    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, attempt1, attempt2, level3_router_resp]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings, catalog=real_catalog))

    assert mock.call_count == 4  # no Generator call after a Level-3 gate_stop
    assert result["status"] == "declined"


def test_level3_no_match_returned_cleanly(skill_index, skills_root, real_catalog):
    """Level 3's fresh routing pass finding literally nothing (no
    gate_stop, no matched references, no unresolved topics) must fall
    back to the same generic-unmapped safety net the primary routing call
    uses -- not an unhandled exception, not a further retry."""
    settings = _settings(skills_root)
    router_resp = _router_resp()
    attempt1 = MagicMock(parsed=_unsupported_metric_entry())
    attempt2 = MagicMock(parsed=_unsupported_metric_entry())
    level3_router_resp = MagicMock(parsed={"matched_references": [], "unresolved_topics": []})

    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, attempt1, attempt2, level3_router_resp]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings, catalog=real_catalog))

    assert mock.call_count == 4
    assert result["status"] == "unmapped"


def test_ordinary_level2_validation_failure_does_not_trigger_level3(skill_index, skills_root, real_catalog):
    """Per the frozen architecture: 'ordinary validation failures are not
    automatically Level-3 routing failures.' A Level-2 attempt that fails
    for a reason OTHER than unsupported_metric (here: a fabricated metric
    name) must return validation_failed as final -- no Level-3 escalation,
    no extra LLM calls."""
    settings = _settings(skills_root)
    router_resp = _router_resp()
    attempt1 = MagicMock(parsed=_unsupported_metric_entry())
    fabricated = _ok_entry_for("totally_made_up_metric")
    fabricated["query"] = "totally_made_up_metric"
    attempt2 = MagicMock(parsed=fabricated)

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, attempt1, attempt2]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings, catalog=real_catalog))

    assert mock.call_count == 3  # no Level-3 router/Generator calls
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS


def test_alert_path_level2_unsupported_metric_escalates_to_level3(skill_index, skills_root, real_catalog):
    """Special-path coverage: the Level-2 -> Level-3 escalation applies to
    alert-rule proposals too, not just ordinary read queries."""
    settings = _alert_catalog_settings(skills_root)
    router_resp = _alert_router_resp()
    attempt1 = MagicMock(parsed=_unsupported_metric_entry())
    attempt2 = MagicMock(parsed=_unsupported_metric_entry())
    level3_router_resp = _alert_router_resp()
    level3_gen_resp = _alert_gen_resp(metric_name="node_load1", condition_query="node_load1 > 5")

    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, attempt1, attempt2, level3_router_resp, level3_gen_resp]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("alert me if cpu utilization busy idle exceeds 90% for 5m",
                                             skill_index, settings, catalog=real_catalog))

    assert mock.call_count == 5
    assert result.get("status") == "alert_rule_proposed"
    assert result["measurement_used"]["name"] == "node_load1"


def test_clarification_does_not_spuriously_trigger_level2_or_level3(skill_index, skills_root, real_catalog):
    """Genuine ambiguity (status: ambiguous_metric) must remain
    clarification -- it must never be treated as unsupported_metric or a
    candidate_scope_violation, and must never trigger Level 2/3 merely
    because the Generator is uncertain."""
    settings = _settings(skills_root)
    router_resp = _router_resp()
    ambiguous_resp = MagicMock(parsed={
        "mode": "single", "status": "ambiguous_metric",
        "reference_used": "references/node-exporter/cpu.md",
        "candidates": [
            {"name": "node_cpu_seconds_total", "purpose": "CPU busy time"},
            {"name": "node_load1", "purpose": "1-minute load average"},
        ],
        "clarification": "Do you want CPU busy time or load average?",
        "explanation": "The question could mean either CPU utilization or system load.",
    })

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, ambiguous_resp]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings, catalog=real_catalog))

    assert mock.call_count == 2  # no Level-2/3 escalation
    assert result["status"] == "ambiguous_metric"


def test_level3_preserves_catalog_status_validation(skill_index, skills_root, real_catalog):
    """Special-path coverage: Phase 12/13's catalog-status validation
    (independent of Level 1/2/3) must remain active inside Level 3's own
    attempt -- a pending/rejected catalog metric must still be rejected
    even after escalating all the way to Level 3."""
    settings = _alert_catalog_settings(
        skills_root, catalog_metric_status_validation_enabled=True,
    )
    router_resp = _router_resp()
    attempt1 = MagicMock(parsed=_unsupported_metric_entry())
    attempt2 = MagicMock(parsed=_unsupported_metric_entry())
    level3_router_resp = _router_resp()

    pending_catalog = replace(real_catalog, metrics=tuple(
        replace(m, status="discovered_pending_review") if m.name == "node_load1" else m
        for m in real_catalog.metrics
    ))
    level3_gen_resp = MagicMock(parsed=_ok_entry_for("node_load1"))

    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, attempt1, attempt2, level3_router_resp, level3_gen_resp]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings,
                                             catalog=pending_catalog))

    assert mock.call_count == 5
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert "discovered_pending_review" in result["explanation"]


def _level3_calls(l3_gen_parsed):
    """Router, L1 gen (unsupported), L2 gen (unsupported), L3 router, L3 gen."""
    return [_router_resp(), MagicMock(parsed=_unsupported_metric_entry()),
            MagicMock(parsed=_unsupported_metric_entry()), _router_resp(), MagicMock(parsed=l3_gen_parsed)]


def test_level3_still_rejects_fabricated_metric(skill_index, skills_root, real_catalog):
    """Level 3 is unconstrained ONLY with respect to catalog candidates:
    known_metrics non-fabrication must still reject a made-up metric."""
    settings = _settings(skills_root)
    with patch.object(llm_client, "call_llm_json",
                       side_effect=_level3_calls(_ok_entry_for("node_totally_made_up_total"))) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings, catalog=real_catalog))
    assert mock.call_count == 5
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert "fabricated" in result["explanation"]


def test_level3_still_rejects_undiscovered_label(skill_index, skills_root, real_catalog):
    """Live label discovery remains the authority for label keys at Level 3."""
    settings = _settings(skills_root)
    entry = _ok_entry_for("node_load1")
    entry["query"] = 'node_load1{bogus_label="x"}'
    with patch.object(llm_client, "call_llm_json", side_effect=_level3_calls(entry)) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics",
               return_value={"node_load1": ["instance", "job"]}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings, catalog=real_catalog))
    assert mock.call_count == 5
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS


def test_level3_alert_proposal_blocked_when_alert_creation_disabled(skill_index, skills_root, real_catalog):
    """A Level-3 Generator that emits alert_rule_proposed while the feature
    flag is off must not get through (same gates as the first pass)."""
    settings = _settings(skills_root)
    proposal = {"mode": "single", "status": "alert_rule_proposed"}
    with patch.object(llm_client, "call_llm_json", side_effect=_level3_calls(proposal)), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings, catalog=real_catalog))
    assert result["status"] != "alert_rule_proposed"


def test_no_level1_scope_or_fallback_when_catalog_unavailable(skill_index, skills_root):
    """With no catalog available, metric_metadata is always empty, so
    there is no Level-1 scope to violate and therefore never a retry -- a
    metric that would have been outside a Level-1 set (node_load1) is
    simply accepted on the first attempt, exactly like the pre-catalog
    architecture."""
    settings = _settings(skills_root)
    router_resp = _router_resp()
    gen_resp = MagicMock(parsed=_ok_entry_for("node_load1"))

    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, gen_resp]) as mock, \
         patch.object(catalog_runtime, "get_structured_catalog", return_value=None), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu utilization busy idle", skill_index, settings))

    assert mock.call_count == 2
    assert result["status"] == "ok"


def test_build_level2_generator_context_unions_multiple_matched_references(skill_index, skills_root, real_catalog):
    """Direct unit coverage of _build_level2_generator_context: candidates
    span every matched reference, deduplicated, restricted to
    known_prometheus_metrics, and every other GeneratorContext field is
    carried over unchanged (Markdown/known_metrics/labels_by_metric)."""
    settings = _settings(skills_root)
    matched = [
        {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"},
        {"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus"},
    ]
    with patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        base_ctx = pipeline._build_generator_context(matched, skill_index, settings,
                                                       question="cpu and memory", catalog=real_catalog)
        level2_ctx = pipeline._build_level2_generator_context(base_ctx, matched, real_catalog)

    level2_names = {m["name"] for m in level2_ctx.metric_metadata}
    expected = {"node_cpu_seconds_total", "node_context_switches_total", "node_intr_total",
                "node_load1", "node_load5", "node_load15",
                "node_memory_MemTotal_bytes", "node_memory_MemAvailable_bytes", "node_memory_MemFree_bytes",
                "node_memory_Cached_bytes", "node_memory_Buffers_bytes",
                "node_memory_SwapTotal_bytes", "node_memory_SwapFree_bytes"}
    assert level2_names == expected
    assert level2_names <= base_ctx.known_prometheus_metrics

    # Everything else is untouched.
    assert level2_ctx.known_prometheus_metrics == base_ctx.known_prometheus_metrics
    assert level2_ctx.labels_by_metric == base_ctx.labels_by_metric
    assert level2_ctx.reference_texts == base_ctx.reference_texts
    assert level2_ctx.overview_texts == base_ctx.overview_texts


def test_build_level2_generator_context_defensive_none_catalog(skill_index, skills_root):
    """Defensive path: catalog=None degrades to an empty metric_metadata,
    never an error."""
    settings = _settings(skills_root)
    matched = [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}]
    with patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        base_ctx = pipeline._build_generator_context(matched, skill_index, settings)
    level2_ctx = pipeline._build_level2_generator_context(base_ctx, matched, None)
    assert level2_ctx.metric_metadata == ()


def test_alert_path_candidate_scope_violation_also_falls_back_to_level2(skill_index, skills_root, real_catalog):
    """Special-path coverage: the same Level-1 -> Level-2 fallback applies
    to alert-rule proposals, not just ordinary read queries."""
    settings = _alert_catalog_settings(skills_root)
    router_resp = _alert_router_resp()  # matches references/node-exporter/cpu.md
    attempt1 = _alert_gen_resp(metric_name="node_load1", condition_query="node_load1 > 5")
    attempt2 = _alert_gen_resp(metric_name="node_load1", condition_query="node_load1 > 5")

    with patch.object(llm_client, "call_llm_json",
                       side_effect=[router_resp, attempt1, attempt2]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("alert me if cpu utilization busy idle exceeds 90% for 5m",
                                             skill_index, settings, catalog=real_catalog))

    assert mock.call_count == 3
    assert result.get("status") == "alert_rule_proposed"
    assert result["measurement_used"]["name"] == "node_load1"


# ---- Level-1 reference scoping: Level 1 within the Router-selected file(s), Level 1 <= Level 2 ----
#
# Regression for the "node" scope-word problem: "What is the current CPU
# utilization on node X?" scores EVERY node_* metric (name + keyword +
# exporter match on "node"), and Level 1 used to intersect only with the whole
# exporter's Metric Directory, so 16 metrics -- including node_memory_* and
# node_filesystem_* -- were offered for a CPU question while Level 2 (the
# matched file's metrics) had only 6. Level 1 is now drawn from the matched
# reference file(s), so it is always a subset of Level 2.

_CPU_REF = "references/node-exporter/cpu.md"
_CPU_FILE_METRICS = {
    "node_cpu_seconds_total", "node_context_switches_total", "node_intr_total",
    "node_load1", "node_load5", "node_load15",
}
_CPU_NODE_QUERY = "What is the current CPU utilization on node X?"
_CANDIDATE_BLOCK_HEADER = "Catalog-identified candidate metrics"


def _matched_refs(*reference_paths):
    return [{"reference_path": p, "data_source": "prometheus"} for p in reference_paths]


def _scoped_context(catalog, skill_index, skills_root, question, *reference_paths):
    matched = _matched_refs(*reference_paths)
    with patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        ctx = pipeline._build_generator_context(
            matched, skill_index, _settings(skills_root), question=question, catalog=catalog,
        )
    return matched, ctx


def _names(ctx):
    return [m["name"] for m in ctx.metric_metadata]


def _file_metric_names(catalog, *reference_paths):
    return {e.name for p in reference_paths for e in catalog.by_reference_path(p)}


def _block_metric_names(prompt):
    """Metric names listed in the catalog candidate block of a Generator prompt."""
    import re

    if _CANDIDATE_BLOCK_HEADER not in prompt:
        return []
    block = prompt.split(_CANDIDATE_BLOCK_HEADER, 1)[1].split("User question:", 1)[0]
    return re.findall(r"^- (\S+) \(type: ", block, flags=re.MULTILINE)


def test_cpu_node_query_level1_is_limited_to_the_cpu_reference(skill_index, skills_root, real_catalog,
                                                              production_catalog):
    for catalog in (production_catalog, real_catalog):
        _, ctx = _scoped_context(catalog, skill_index, skills_root, _CPU_NODE_QUERY, _CPU_REF)
        names = _names(ctx)
        assert names[0] == "node_cpu_seconds_total"
        assert set(names) == _CPU_FILE_METRICS
        # Unrelated metrics from OTHER reference files of the same exporter are excluded ...
        assert not any(n.startswith(("node_memory_", "node_filesystem_", "DCGM_")) for n in names)
        # ... while the validator's / label discovery's universe is still the whole exporter.
        assert "node_memory_MemFree_bytes" in ctx.known_prometheus_metrics
        assert "node_filesystem_size_bytes" in ctx.known_prometheus_metrics
        assert len(ctx.known_prometheus_metrics) == 16
        assert ctx.reference_universe_mismatch is False


def test_scope_word_node_does_not_change_which_file_the_candidates_come_from(skill_index, skills_root,
                                                                            production_catalog):
    _, with_node = _scoped_context(production_catalog, skill_index, skills_root, _CPU_NODE_QUERY, _CPU_REF)
    _, without_node = _scoped_context(production_catalog, skill_index, skills_root,
                                      "What is the current CPU utilization?", _CPU_REF)
    assert _names(without_node)[0] == _names(with_node)[0] == "node_cpu_seconds_total"
    assert set(_names(without_node)) <= set(_names(with_node)) <= _CPU_FILE_METRICS


_SCOPING_CASES = [
    (_CPU_NODE_QUERY, ("references/node-exporter/cpu.md",)),
    (_CPU_NODE_QUERY, ("references/node-exporter/memory.md",)),
    (_CPU_NODE_QUERY, ("references/node-exporter/filesystem.md",)),
    ("What is the 5 minute load average on node X?", ("references/node-exporter/cpu.md",)),
    ("How much memory is available on node X?", ("references/node-exporter/memory.md",)),
    ("How much disk space is available on node X?", ("references/node-exporter/filesystem.md",)),
    ("How much GPU memory is used on each GPU?", ("references/dcgm-exporter/memory.md",)),
    ("What is the GPU temperature?", ("references/dcgm-exporter/thermal.md",)),
    ("Is there any ECC error on the GPUs?", ("references/dcgm-exporter/reliability.md",)),
    ("Show NVLink bandwidth over the last hour", ("references/dcgm-exporter/interconnect.md",)),
    # Multi-reference match: scope and Level 2 are both the union.
    ("cpu and memory utilization on node X",
     ("references/node-exporter/cpu.md", "references/node-exporter/memory.md")),
]


@pytest.mark.parametrize("question,refs", _SCOPING_CASES, ids=[f"{q[:38]}|{len(r)}ref" for q, r in _SCOPING_CASES])
def test_level1_is_scoped_to_matched_files_and_is_a_subset_of_level2(skill_index, skills_root,
                                                                    production_catalog, question, refs):
    """The architectural invariant: Level 1 candidates come from the
    Router-selected reference file(s), and Level 2 (all metrics of those
    files) is always a superset, never wider than known_prometheus_metrics."""
    matched, ctx = _scoped_context(production_catalog, skill_index, skills_root, question, *refs)
    level1 = set(_names(ctx))
    level2 = {m["name"] for m in pipeline._build_level2_generator_context(ctx, matched, production_catalog)
              .metric_metadata}

    assert level1, "expected a confident, non-empty Level-1 candidate set for this question"
    assert level1 <= _file_metric_names(production_catalog, *refs)
    assert level1 <= level2
    assert level2 <= ctx.known_prometheus_metrics
    assert level2 == _file_metric_names(production_catalog, *refs) & ctx.known_prometheus_metrics


def test_level2_keeps_level1_candidates_that_have_no_reference_path(vendor_only_catalog_and_skill_index,
                                                                    skills_root):
    """A vendor-only metric (no Markdown file, reference_path=None) stays
    eligible at Level 1 (scoped to its exporter) and Level 2 must carry it
    over, or Level 1 would not be a subset of Level 2."""
    augmented, _path, vendor_skill_index, vendor_entry = vendor_only_catalog_and_skill_index
    matched = _matched_refs(_CPU_REF)
    fake_decision = catalog_runtime.MetricCandidateDecision(confident=True, candidates=(vendor_entry,),
                                                            top_score=9.0)

    with patch("app.label_discovery.discover_labels_for_metrics", return_value={}), \
         patch("app.catalog.runtime.catalog_metric_candidates", return_value=fake_decision):
        ctx = pipeline._build_generator_context(matched, vendor_skill_index, _settings(skills_root),
                                                question="vendor only test metric", catalog=augmented)
    level2 = pipeline._build_level2_generator_context(ctx, matched, augmented)

    assert vendor_entry.name in _names(ctx)
    assert vendor_entry.name in {m["name"] for m in level2.metric_metadata}
    assert set(_names(ctx)) <= {m["name"] for m in level2.metric_metadata}
    # And the rest of Level 2 is still the matched file's metrics.
    assert _CPU_FILE_METRICS <= {m["name"] for m in level2.metric_metadata}


# ---- fallback behavior with scoped Level 1 ----------------------------------------


def test_candidates_only_in_another_file_fail_open_without_retry_or_level3(skill_index, skills_root,
                                                                           production_catalog):
    """The catalog ranks metrics of a file the Router did NOT pick (a
    memory question routed to cpu.md). Level 1 has no usable scope: no
    candidate block, not a reference-universe mismatch, and exactly the
    ordinary 2 LLM calls -- no Level 2, no Level 3."""
    settings = _settings(skills_root)
    _, ctx = _scoped_context(production_catalog, skill_index, skills_root, "free swap memory", _CPU_REF)
    assert ctx.metric_metadata == ()
    assert ctx.reference_universe_mismatch is False

    gen_resp = MagicMock(parsed=_ok_entry_for("node_memory_SwapFree_bytes"))
    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), gen_resp]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("free swap memory", skill_index, settings, catalog=production_catalog))

    assert mock.call_count == 2
    assert result["status"] == "ok"
    assert _CANDIDATE_BLOCK_HEADER not in mock.call_args_list[1].kwargs["prompt"]


def test_level2_retry_prompt_is_a_superset_of_the_level1_prompt(skill_index, skills_root, production_catalog):
    settings = _settings(skills_root)
    attempt1 = MagicMock(parsed=_unsupported_metric_entry())
    attempt2 = MagicMock(parsed=_ok_entry_for("node_load1"))

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), attempt1, attempt2]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline(_CPU_NODE_QUERY, skill_index, settings, catalog=production_catalog))

    assert mock.call_count == 3          # Router + Level 1 + Level 2, no Level 3
    assert result["status"] == "ok"
    level1_block = set(_block_metric_names(mock.call_args_list[1].kwargs["prompt"]))
    level2_block = set(_block_metric_names(mock.call_args_list[2].kwargs["prompt"]))
    assert level1_block == _CPU_FILE_METRICS
    assert level1_block <= level2_block
    assert not any(n.startswith(("node_memory_", "node_filesystem_")) for n in level1_block | level2_block)


def test_picking_a_metric_from_another_file_is_now_a_scope_violation_that_retries_at_level2(
        skill_index, skills_root, production_catalog):
    """With the exporter-wide Level 1, a memory metric for a CPU question
    sat inside the candidate scope and was accepted without any retry. With
    Level 1 scoped to cpu.md it is a candidate-scope violation, so the
    existing Level-2 fallback runs once and the Generator recovers."""
    settings = _settings(skills_root)
    attempt1 = MagicMock(parsed=_ok_entry_for("node_memory_MemFree_bytes"))   # known, other file
    attempt2 = MagicMock(parsed=_ok_entry_for("node_cpu_seconds_total"))

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), attempt1, attempt2]) as mock, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline(_CPU_NODE_QUERY, skill_index, settings, catalog=production_catalog))

    assert mock.call_count == 3
    assert result["status"] == "ok"
    assert result["measurement_used"]["name"] == "node_cpu_seconds_total"
