import pytest

from app.skill_index import SkillIndex, SkillIndexError, parse_routing_table


def test_load_real_skill_package(skill_index):
    assert skill_index.metadata.name == "observability-query-builder"
    assert skill_index.metadata.version == "1.4"
    assert len(skill_index.routing_rows) >= 10


def test_routing_table_has_expected_rows(skill_index):
    paths = {row.reference_path for row in skill_index.routing_rows}
    assert "references/node-exporter/cpu.md" in paths
    assert "references/dcgm-exporter/thermal.md" in paths
    assert "references/opensearch-fundamentals.md" in paths
    assert "references/execution-contract.md" in paths


def test_execution_contract_row_is_not_a_real_datasource(skill_index):
    row = skill_index.row_for_path("references/execution-contract.md")
    assert row is not None
    assert row.data_sources == ("n/a",)
    assert row.is_real_datasource is False


def test_opensearch_fundamentals_row_carries_infrastructure_note(skill_index):
    row = skill_index.row_for_path("references/opensearch-fundamentals.md")
    assert row is not None
    assert row.data_sources == ("opensearch",)
    assert "infrastructure only" in row.note.lower()


def test_overview_path_for_domain_file(skill_index):
    assert skill_index.overview_path_for("references/node-exporter/cpu.md") == "references/node-exporter/overview.md"
    assert skill_index.overview_path_for("references/dcgm-exporter/thermal.md") == "references/dcgm-exporter/overview.md"


def test_overview_path_and_metric_directory_paths_are_always_forward_slash(skill_index):
    """Regression test: on Windows, pathlib.Path's string conversion uses
    the native separator ('\\\\'), which silently produced paths like
    'references\\\\node-exporter\\\\overview.md' -- valid for disk access, but
    WRONG as a logical reference-path string, since every comparison
    against SKILL.md's own routing table (and every dict key derived from
    it) is forward-slash. Asserts the actual character content, so this
    fails on any OS if the bug is reintroduced -- it doesn't rely on
    running on Windows to catch it."""
    overview_path = skill_index.overview_path_for("references/node-exporter/cpu.md")
    assert "\\" not in overview_path
    assert overview_path == "references/node-exporter/overview.md"

    metrics = skill_index.metric_directory("references/node-exporter/overview.md")
    for domain_file_path in metrics.values():
        assert "\\" not in domain_file_path


def test_overview_path_for_overview_itself_is_none(skill_index):
    assert skill_index.overview_path_for("references/node-exporter/overview.md") is None


def test_overview_path_for_fundamentals_is_none(skill_index):
    assert skill_index.overview_path_for("references/prometheus-fundamentals.md") is None


def test_metric_directory_parses_all_node_exporter_metrics(skill_index):
    metrics = skill_index.metric_directory("references/node-exporter/overview.md")
    assert "node_cpu_seconds_total" in metrics
    assert "node_load1" in metrics
    assert "node_filesystem_avail_bytes" in metrics
    assert metrics["node_cpu_seconds_total"] == "references/node-exporter/cpu.md"
    assert metrics["node_filesystem_avail_bytes"] == "references/node-exporter/filesystem.md"


def test_editing_an_existing_overview_content_is_picked_up_without_reload(tmp_path):
    """Proves the 'no restart needed for a content edit' half of the
    dynamism story: metric_directory() (and read_reference() underneath
    it) re-reads the file from disk on every call -- never caches -- so a
    metric added to an ALREADY-ROUTED exporter's overview.md is visible on
    the very next call, with the same SkillIndex object, no reload."""
    refs = tmp_path / "references" / "node-exporter"
    refs.mkdir(parents=True)
    (refs / "cpu.md").write_text("# CPU\n", encoding="utf-8")
    overview_path = refs / "overview.md"
    overview_path.write_text(
        "# Node Exporter\n\n## Metric Directory\n"
        "| Domain | Purpose | Metric | File |\n|---|---|---|---|\n"
        "| cpu | CPU time | `node_cpu_seconds_total` | `cpu.md` |\n", encoding="utf-8")
    (tmp_path / "SKILL.md").write_text(
        "---\nname: x\ndescription: y\nmetadata:\n  version: \"1.0\"\n---\n"
        "## 4. Routing table\n"
        "| CPU | prometheus | [references/node-exporter/cpu.md](references/node-exporter/cpu.md) |\n", encoding="utf-8")
    index = SkillIndex.load(tmp_path)

    before = index.metric_directory("references/node-exporter/overview.md")
    assert "node_load1" not in before

    # Simulate someone editing overview.md on disk WITHOUT restarting the
    # process or calling /api/v1/admin/reload-skill.
    overview_path.write_text(
        "# Node Exporter\n\n## Metric Directory\n"
        "| Domain | Purpose | Metric | File |\n|---|---|---|---|\n"
        "| cpu | CPU time | `node_cpu_seconds_total` | `cpu.md` |\n"
        "| load | Load average | `node_load1` | `cpu.md` |\n", encoding="utf-8")

    after = index.metric_directory("references/node-exporter/overview.md")
    assert "node_load1" in after  # same SkillIndex object, new content, no reload call


def test_adding_a_new_routing_row_requires_reload_not_picked_up_live(tmp_path):
    """The other half: a structural change (a new routing-table row) is
    snapshotted in SkillIndex.routing_rows at .load() time and does NOT
    appear until SkillIndex.load() runs again -- proving routing_rows is
    genuinely a load-time snapshot, distinct from reference-file content."""
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\nname: x\ndescription: y\nmetadata:\n  version: \"1.0\"\n---\n"
        "## 4. Routing table\n"
        "| CPU | prometheus | [references/cpu.md](references/cpu.md) |\n", encoding="utf-8")
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "cpu.md").write_text("# CPU\n", encoding="utf-8")

    index = SkillIndex.load(tmp_path)
    assert len(index.routing_rows) == 1

    (tmp_path / "references" / "memory.md").write_text("# Memory\n", encoding="utf-8")
    skill_md.write_text(
        "---\nname: x\ndescription: y\nmetadata:\n  version: \"1.0\"\n---\n"
        "## 4. Routing table\n"
        "| CPU | prometheus | [references/cpu.md](references/cpu.md) |\n"
        "| Memory | prometheus | [references/memory.md](references/memory.md) |\n", encoding="utf-8")

    assert len(index.routing_rows) == 1  # unchanged -- same object, no reload
    reloaded = SkillIndex.load(tmp_path)
    assert len(reloaded.routing_rows) == 2  # a fresh .load() picks it up


def test_datasources_in_play(skill_index):
    sources = skill_index.datasources_in_play([
        "references/node-exporter/cpu.md",
        "references/dcgm-exporter/thermal.md",
    ])
    assert sources == {"prometheus"}


def test_section_extraction(skill_index):
    section = skill_index.section("## 5.")
    assert section.startswith("## 5. Operating Principles")
    assert "Never fabricate" in section
    # must stop before the next header
    assert "## 6." not in section


def test_section_not_found_raises(skill_index):
    with pytest.raises(SkillIndexError):
        skill_index.section("## 99. Nonexistent Section")


def test_load_missing_skill_md_raises(tmp_path):
    with pytest.raises(SkillIndexError):
        SkillIndex.load(tmp_path)


def test_validate_catches_missing_non_pending_reference(tmp_path):
    (tmp_path / "SKILL.md").write_text(
        "---\nname: x\ndescription: y\nmetadata:\n  version: \"1.0\"\n---\n"
        "## 4. Routing table\n"
        "| topic | prometheus | [references/missing.md](references/missing.md) |\n", encoding="utf-8")
    with pytest.raises(SkillIndexError):
        SkillIndex.load(tmp_path)


def test_pending_addition_row_does_not_fail_validation(tmp_path):
    (tmp_path / "SKILL.md").write_text(
        "---\nname: x\ndescription: y\nmetadata:\n  version: \"1.0\"\n---\n"
        "## 4. Routing table\n"
        "| topic | opensearch | [references/pending.md](references/pending.md) — pending addition |\n", encoding="utf-8")
    index = SkillIndex.load(tmp_path)
    assert index.routing_rows[0].pending is True


def test_fundamentals_reference_for_known_datasources(skill_index):
    assert skill_index.fundamentals_reference_for("prometheus") == "references/prometheus-fundamentals.md"
    assert skill_index.fundamentals_reference_for("opensearch") == "references/opensearch-fundamentals.md"
    assert skill_index.fundamentals_reference_for("OPENSEARCH") == "references/opensearch-fundamentals.md"  # case-insensitive


def test_fundamentals_reference_for_unknown_datasource_is_none(skill_index):
    assert skill_index.fundamentals_reference_for("loki") is None


def test_fundamentals_reference_for_is_derived_not_hardcoded(tmp_path):
    """Proves fundamentals_reference_for() is genuinely derived from the
    routing table, not a hardcoded {prometheus, opensearch} map: a
    synthetic third data source with its own *-fundamentals.md row is
    picked up with zero code change."""
    refs = tmp_path / "references"
    refs.mkdir()
    (refs / "loki-fundamentals.md").write_text("# Loki fundamentals\n", encoding="utf-8")
    (tmp_path / "SKILL.md").write_text(
        "---\nname: x\ndescription: y\nmetadata:\n  version: \"1.0\"\n---\n"
        "## 4. Routing table\n"
        "| Loki syntax | loki | [references/loki-fundamentals.md](references/loki-fundamentals.md) |\n", encoding="utf-8")
    index = SkillIndex.load(tmp_path)
    assert index.fundamentals_reference_for("loki") == "references/loki-fundamentals.md"


def test_parse_routing_table_multi_valued_data_source():
    text = (
        "## 4. Routing table\n"
        "| GPU thermal (Prometheus + log evidence) | prometheus, opensearch | "
        "[references/dcgm-exporter/thermal.md](references/dcgm-exporter/thermal.md) |\n"
    )
    rows = parse_routing_table(text)
    assert len(rows) == 1
    assert rows[0].data_sources == ("prometheus", "opensearch")
