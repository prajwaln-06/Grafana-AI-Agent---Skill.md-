"""Routing / reference-structure tests for the OpenSearch log domain
(skills/references/opensearch-logs/*).

The Router is an LLM, so these tests do not (and cannot) assert which
reference it would pick for a free-text question. What is deterministic, and
what is checked here: the routing table exposes the three index families with
the right data source, every linked file exists, the pipeline loads the right
reference/overview/fundamentals text for a chosen reference, and the existing
Prometheus routing is untouched.
"""
from unittest.mock import patch

import pytest

from app import pipeline
from app.opensearch_catalog.context import error_context

FAMILY_REFERENCES = {
    "fabric_health": "references/opensearch-logs/fabric-health.md",
    "controller_log": "references/opensearch-logs/controller-log.md",
    "ilo_events": "references/opensearch-logs/ilo-events.md",
}
OVERVIEW = "references/opensearch-logs/overview.md"

PROMETHEUS_REFERENCES = (
    "references/node-exporter/overview.md",
    "references/node-exporter/cpu.md",
    "references/node-exporter/memory.md",
    "references/node-exporter/filesystem.md",
    "references/dcgm-exporter/overview.md",
    "references/dcgm-exporter/compute.md",
    "references/dcgm-exporter/thermal.md",
    "references/dcgm-exporter/memory.md",
    "references/dcgm-exporter/interconnect.md",
    "references/dcgm-exporter/reliability.md",
    "references/prometheus-fundamentals.md",
)


def test_routing_table_has_a_row_per_opensearch_family_and_overview(skill_index):
    for path in (OVERVIEW, *FAMILY_REFERENCES.values()):
        row = skill_index.row_for_path(path)
        assert row is not None, f"no routing row for {path}"
        assert row.data_sources == ("opensearch",)
        assert row.is_real_datasource
        assert row.pending is False


def test_every_opensearch_row_points_at_a_file_that_exists(skill_index, skills_root):
    rows = [r for r in skill_index.routing_rows if "opensearch" in r.data_sources]
    # overview + three families + fundamentals
    assert len(rows) == 5
    for row in rows:
        assert (skills_root / row.reference_path).is_file(), row.reference_path


def test_family_rows_use_distinctive_vocabulary(skill_index):
    """Guards against the three rows collapsing into near-duplicates, which
    would give the Router no basis to separate the families."""
    topics = {
        pid: skill_index.row_for_path(path).topic.lower()
        for pid, path in FAMILY_REFERENCES.items()
    }
    assert "fabric" in topics["fabric_health"] and "heartbeat" in topics["fabric_health"]
    assert "controller" in topics["controller_log"] and "hmscd" in topics["controller_log"]
    assert "ilo" in topics["ilo_events"] and "bmc" in topics["ilo_events"]
    # A family's signature terms must not appear in a sibling family's row.
    assert "hmscd" not in topics["fabric_health"] and "hmscd" not in topics["ilo_events"]
    assert "heartbeat" not in topics["controller_log"] and "heartbeat" not in topics["ilo_events"]
    assert "ipmi" not in topics["fabric_health"] and "ipmi" not in topics["controller_log"]


ANALYSIS_TRIGGERS = {
    "fabric_health": ("stopped sending heartbeats", "recurring port faults"),
    "controller_log": ("before and after an event", "fewer switches available than expected"),
    "ilo_events": ("ingested",),
}


def test_analysis_phrasing_routes_to_exactly_one_family(skill_index):
    """The new phrasings for the five analyses appear in their own family's row and in no
    sibling row, so they add no overlap between the three families."""
    topics = {pid: skill_index.row_for_path(path).topic.lower() for pid, path in FAMILY_REFERENCES.items()}
    for pid, phrases in ANALYSIS_TRIGGERS.items():
        for phrase in phrases:
            assert phrase in topics[pid], (pid, phrase)
            for other, topic in topics.items():
                if other != pid:
                    assert phrase not in topic, (phrase, other)
    assert "before and after" not in topics["fabric_health"] + topics["ilo_events"]
    assert "available" not in topics["fabric_health"] + topics["ilo_events"]


def test_each_family_file_documents_exactly_its_registry_analyses(skills_root, os_registry):
    """A family file describes an analysis only if the registry configures it for that family,
    and every configured analysis is described (as `"type": "<name>"` in an example)."""
    names = ("absence", "context_window", "ingest_lag", "text_ratio_shortfall", "recurrence")
    for pid, path in FAMILY_REFERENCES.items():
        text = (skills_root / path).read_text(encoding="utf-8")
        configured = set(os_registry.get(pid).analyses)
        for name in names:
            documented = f'"type": "{name}"' in text
            assert documented == (name in configured), (pid, name)


def test_the_old_unmapped_statements_for_the_new_analyses_are_gone(skills_root):
    """The four places that said 'treat as unmapped' for absence, before/after, the n/m shortfall
    and ingest lag now give instructions instead; the remaining unmapped boundaries stay."""
    def flat(path):
        return " ".join((skills_root / path).read_text(encoding="utf-8").split())
    overview = flat(OVERVIEW)
    assert 'absence or "no recent heartbeat" detection' not in overview
    assert "Still not supported: cross-family queries" in overview                 # still unmapped
    ctl = flat(FAMILY_REFERENCES["controller_log"])
    assert "Treat these as `unmapped`; do not approximate" not in ctl
    assert "Use `text_ratio_shortfall`" in ctl and "Use `context_window`" in ctl
    fab = flat(FAMILY_REFERENCES["fabric_health"])
    assert 'no "heartbeat missing" logic is supported yet' not in fab and "**placeholders**" in fab
    ilo = flat(FAMILY_REFERENCES["ilo_events"])
    assert "computing per-document ingestion lag" not in ilo and "Use the `ingest_lag` analysis" in ilo
    assert 'detecting "unusually frequent" access' in ilo and "failed login detection" in ilo   # these stay unmapped


def test_skill_section_9_documents_the_optional_keys_and_keeps_the_contract(skill_index):
    text = skill_index.section("## 9.")
    for needle in ("`intent`", "`analysis`", "absence", "context_window", "ingest_lag", "text_ratio_shortfall",
                   "recurrence", "`pattern_id`", "never write an `index`", '"type": "raw_metric"'):
        assert needle in text, needle


def test_opensearch_datasource_is_in_play_for_each_family(skill_index):
    for path in FAMILY_REFERENCES.values():
        assert skill_index.datasources_in_play([path]) == {"opensearch"}


def test_family_files_resolve_the_logs_overview_but_overview_has_none(skill_index):
    for path in FAMILY_REFERENCES.values():
        assert skill_index.overview_path_for(path) == OVERVIEW
    assert skill_index.overview_path_for(OVERVIEW) is None


def test_opensearch_fundamentals_is_still_found_for_opensearch(skill_index):
    assert skill_index.fundamentals_reference_for("opensearch") == "references/opensearch-fundamentals.md"


def test_prometheus_routing_rows_are_unchanged(skill_index):
    for path in PROMETHEUS_REFERENCES:
        row = skill_index.row_for_path(path)
        assert row is not None, path
        assert row.data_sources == ("prometheus",)
    # No Prometheus reference was moved to opensearch or vice versa.
    assert skill_index.datasources_in_play(PROMETHEUS_REFERENCES) == {"prometheus"}


def test_skill_package_validates_with_the_new_references(skill_index):
    skill_index.validate()  # raises SkillIndexError if any non-pending link is broken


def test_skill_section_3_admits_log_questions(skill_index):
    text = skill_index.section("## 3.")
    assert "references/opensearch-logs/overview.md" in text


def test_each_family_file_names_its_pattern_id(skills_root):
    """The registry resolves pattern_id -> index pattern; each domain file must
    name the pattern_id it is paired with, so the two cannot silently drift."""
    for pattern_id, path in FAMILY_REFERENCES.items():
        assert f"`pattern_id: {pattern_id}`" in (skills_root / path).read_text(encoding="utf-8")
    overview = (skills_root / OVERVIEW).read_text(encoding="utf-8")
    for pattern_id in FAMILY_REFERENCES:
        assert f"`{pattern_id}`" in overview


def test_domain_files_contain_no_hard_index_names_or_raw_log_data(skills_root):
    """Domain files refer to families by pattern_id; the registry owns the
    index patterns. Also a tripwire against pasting real log content."""
    import re
    for path in (OVERVIEW, *FAMILY_REFERENCES.values()):
        text = (skills_root / path).read_text(encoding="utf-8")
        assert "slingshot_crayfabrichealth-" not in text, path
        assert "controllerlog-" not in text, path
        assert "log_iml-" not in text, path
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text), f"IP-like string in {path}"


# ---- pipeline reference selection --------------------------------------------


@pytest.mark.parametrize("pattern_id", sorted(FAMILY_REFERENCES))
def test_generator_context_loads_family_overview_and_fundamentals(pattern_id, skill_index, settings):
    """Given a Router choice of one OpenSearch family, the Generator context
    carries that family's reference, the shared logs overview and the
    OpenSearch fundamentals -- and nothing Prometheus-specific."""
    ref = FAMILY_REFERENCES[pattern_id]
    matched = [{"reference_path": ref, "data_source": "opensearch"}]

    with patch("app.opensearch_catalog.runtime.build_request_context", return_value=error_context("test: no cluster")):
        ctx = pipeline._build_generator_context(matched, skill_index, settings, question="q")

    assert ref in ctx.reference_texts
    assert OVERVIEW in ctx.overview_texts
    assert "references/opensearch-fundamentals.md" in ctx.fundamentals_texts
    assert ctx.known_prometheus_metrics == set()
    assert not ctx.labels_by_metric
    assert "references/prometheus-fundamentals.md" not in ctx.fundamentals_texts


def test_other_families_are_not_loaded_into_a_single_family_context(skill_index, settings):
    """Per-family files exist so one family's schema is not in another's
    prompt (plan decision OS-D4)."""
    ref = FAMILY_REFERENCES["controller_log"]
    matched = [{"reference_path": ref, "data_source": "opensearch"}]

    with patch("app.opensearch_catalog.runtime.build_request_context", return_value=error_context("test: no cluster")):
        ctx = pipeline._build_generator_context(matched, skill_index, settings, question="q")

    assert set(ctx.reference_texts) == {ref}
    for other in (FAMILY_REFERENCES["fabric_health"], FAMILY_REFERENCES["ilo_events"]):
        assert other not in ctx.reference_texts


def test_router_prompt_shows_the_opensearch_rows(skill_index, settings):
    """Without catalog narrowing, the Router is shown the full Section 4,
    including the new OpenSearch rows."""
    section4 = skill_index.section("## 4.")
    for path in (OVERVIEW, *FAMILY_REFERENCES.values()):
        assert path in section4
