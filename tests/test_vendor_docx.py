"""
tests/test_vendor_docx.py

Batch 1 tests for app/catalog/vendor_docx.py: parsing the attached vendor
metric document (tests/fixtures/prometheus_metrics.docx) into
VendorMetricRecords, including its one known malformed row.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.catalog.vendor import RejectedVendorRow, VendorMetricRecord, VendorParseError
from app.catalog.vendor_docx import _extract_unit, parse_vendor_docx

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "prometheus_metrics.docx"

# Every metric in the real fixture document that has an explicit,
# recognized unit, and the exact value _extract_unit must produce for it.
# This is the full, closed set (14 of 43 metrics) -- everything else in
# the fixture must resolve to None (see test_only_these_14_metrics_get_a_unit).
_EXPECTED_FIXTURE_UNITS: dict[str, str] = {
    "DCGM_FI_DEV_GPU_UTIL": "%",
    "DCGM_FI_DEV_FB_USED": "bytes",
    "DCGM_FI_DEV_FB_FREE": "bytes",
    "DCGM_FI_DEV_MEM_COPY_UTIL": "%",
    "DCGM_FI_DEV_GPU_TEMP": "°C",
    "DCGM_FI_DEV_MEMORY_TEMP": "°C",
    "DCGM_FI_DEV_POWER_USAGE": "Watts",
    "DCGM_FI_DEV_SM_CLOCK": "MHz",
    "DCGM_FI_DEV_MEM_CLOCK": "MHz",
    "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE": "%",
    "DCGM_FI_PROF_PIPE_FP64_ACTIVE": "%",
    "DCGM_FI_PROF_PIPE_FP32_ACTIVE": "%",
    "DCGM_FI_PROF_PIPE_FP16_ACTIVE": "%",
    "DCGM_FI_PROF_DRAM_ACTIVE": "%",
}


# ---- against the real attached vendor document -----------------------


def test_parses_exactly_43_valid_metrics():
    result = parse_vendor_docx(FIXTURE_PATH)
    assert len(result.records) == 43


def test_16_node_exporter_and_27_dcgm_exporter_metrics():
    result = parse_vendor_docx(FIXTURE_PATH)
    by_exporter: dict[str, int] = {}
    for record in result.records:
        by_exporter[record.exporter] = by_exporter.get(record.exporter, 0) + 1
    assert by_exporter == {"node-exporter": 16, "dcgm-exporter": 27}


def test_representative_node_exporter_metric_fields():
    result = parse_vendor_docx(FIXTURE_PATH)
    by_name = {r.name: r for r in result.records}
    entry = by_name["node_cpu_seconds_total"]
    assert entry.type == "Counter"
    assert entry.category == "CPU"
    assert "CPU time spent in different modes" in entry.help
    assert entry.exporter == "node-exporter"


def test_representative_dcgm_exporter_metric_fields():
    result = parse_vendor_docx(FIXTURE_PATH)
    by_name = {r.name: r for r in result.records}
    entry = by_name["DCGM_FI_DEV_GPU_UTIL"]
    assert entry.type == "Gauge"
    assert entry.category == "GPU Utilization"
    assert "GPU utilization" in entry.help
    assert entry.exporter == "dcgm-exporter"


def test_exporter_assigned_by_table_membership_not_name_heuristic():
    """Every node-exporter record's name and every dcgm-exporter record's
    name follow a naming convention in this particular document, but the
    exporter assignment itself must come from which table the row was in
    -- prove that by checking it holds for every parsed record, not just
    spot-checking the convention."""
    result = parse_vendor_docx(FIXTURE_PATH)
    for record in result.records:
        if record.exporter == "node-exporter":
            assert record.name.startswith("node_")
        elif record.exporter == "dcgm-exporter":
            assert record.name.startswith("DCGM_")
        else:
            pytest.fail(f"unexpected exporter {record.exporter!r} for {record.name!r}")


def test_known_malformed_filesystem_row_is_rejected_not_a_record():
    result = parse_vendor_docx(FIXTURE_PATH)
    names = {r.name for r in result.records}
    assert "`" not in names
    assert len(result.rejected_rows) == 1
    rejected = result.rejected_rows[0]
    assert isinstance(rejected, RejectedVendorRow)
    assert rejected.raw_cells == ("Filesystem", "`", "", "", "")
    assert rejected.reason


def test_malformed_row_does_not_prevent_other_rows_from_parsing():
    """The malformed row sits between valid Filesystem rows in the
    document; a document-level failure would lose all of them, not just
    the bad one."""
    result = parse_vendor_docx(FIXTURE_PATH)
    names = {r.name for r in result.records}
    assert "node_filesystem_size_bytes" in names
    assert "node_filesystem_avail_bytes" in names
    assert "node_filesystem_free_bytes" in names


def test_no_duplicate_metric_names():
    result = parse_vendor_docx(FIXTURE_PATH)
    names = [r.name for r in result.records]
    assert len(names) == len(set(names))


def test_every_record_has_all_required_fields_populated():
    result = parse_vendor_docx(FIXTURE_PATH)
    for record in result.records:
        assert isinstance(record, VendorMetricRecord)
        assert record.name.strip()
        assert record.type.strip()
        assert record.category.strip()
        assert record.help.strip()
        assert record.exporter.strip()


# ---- structural / invalid-input behavior -------------------------------


def test_missing_file_raises(tmp_path):
    missing = tmp_path / "does_not_exist.docx"
    with pytest.raises(Exception):
        parse_vendor_docx(missing)


def test_document_with_no_recognized_table_raises_vendor_parse_error(tmp_path):
    import docx

    doc = docx.Document()
    doc.add_paragraph("Not a metric document at all.")
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "Foo"
    table.rows[0].cells[1].text = "Bar"
    table.rows[1].cells[0].text = "a"
    table.rows[1].cells[1].text = "b"
    unrecognized_path = tmp_path / "unrecognized.docx"
    doc.save(str(unrecognized_path))

    with pytest.raises(VendorParseError):
        parse_vendor_docx(unrecognized_path)


def test_row_with_wrong_column_count_is_rejected_not_raised(tmp_path):
    import docx

    doc = docx.Document()
    table = doc.add_table(rows=2, cols=4)
    table.rows[0].cells[0].text = "Category"
    table.rows[0].cells[1].text = "Node Exporter Metric"
    table.rows[0].cells[2].text = "Prometheus Type"
    table.rows[0].cells[3].text = "What it Measures"
    table.rows[1].cells[0].text = "CPU"
    table.rows[1].cells[1].text = "node_cpu_seconds_total"
    table.rows[1].cells[2].text = "Counter"
    table.rows[1].cells[3].text = "CPU time"
    short_row_path = tmp_path / "short_row.docx"
    doc.save(str(short_row_path))

    result = parse_vendor_docx(short_row_path)
    assert result.records == ()
    assert len(result.rejected_rows) == 1
    assert "columns" in result.rejected_rows[0].reason


# ---- _extract_unit: focused, isolated behavior ------------------------


def test_extract_unit_percent():
    assert _extract_unit("Overall GPU utilization (%)") == "%"


def test_extract_unit_bytes():
    assert _extract_unit("Free framebuffer memory (bytes)") == "bytes"


def test_extract_unit_degrees_celsius():
    assert _extract_unit("GPU core temperature (°C)") == "°C"


def test_extract_unit_mhz():
    assert _extract_unit("SM/Core clock (MHz)") == "MHz"


def test_extract_unit_watts():
    assert _extract_unit("Instantaneous GPU power draw (Watts)") == "Watts"


def test_extract_unit_multiple_parentheticals_uses_last_group_only():
    """'(VRAM)' is descriptive, not a unit; only the trailing '(bytes)'
    is the unit -- this is the real DCGM_FI_DEV_FB_USED wording."""
    assert _extract_unit("Used framebuffer (VRAM) memory (bytes)") == "bytes"


def test_extract_unit_non_unit_parenthetical_returns_none():
    """A parenthetical that lists label values, not a unit, must not be
    mistaken for one -- this is the real node_cpu_seconds_total wording."""
    text = "CPU time spent in different modes (user, system, idle, iowait, etc.)"
    assert _extract_unit(text) is None


def test_extract_unit_no_parenthetical_returns_none():
    assert _extract_unit("1-minute load average") is None


def test_extract_unit_unrecognized_token_returns_none_never_guessed():
    """A parenthetical exists but doesn't match the recognized-unit
    allowlist -- must yield None, not a guess at what it might mean."""
    assert _extract_unit("Fraction of time the SM is active (ratio)") is None
    assert _extract_unit("Some measurement (furlongs)") is None


def test_extract_unit_case_and_whitespace_insensitive_match():
    assert _extract_unit("Power draw ( watts )") == "Watts"
    assert _extract_unit("Utilization (PERCENT)") is None  # not in allowlist; only "%" is


# ---- _extract_unit against the real fixture document -------------------


def test_only_these_14_metrics_get_a_unit_in_the_real_fixture():
    result = parse_vendor_docx(FIXTURE_PATH)
    by_name = {r.name: r for r in result.records}
    actual_units = {name: r.unit for name, r in by_name.items() if r.unit is not None}
    assert actual_units == _EXPECTED_FIXTURE_UNITS


def test_node_cpu_seconds_total_unit_is_none_despite_having_a_parenthetical():
    result = parse_vendor_docx(FIXTURE_PATH)
    by_name = {r.name: r for r in result.records}
    assert by_name["node_cpu_seconds_total"].unit is None


def test_dcgm_fb_used_resolves_to_bytes_not_vram():
    result = parse_vendor_docx(FIXTURE_PATH)
    by_name = {r.name: r for r in result.records}
    assert by_name["DCGM_FI_DEV_FB_USED"].unit == "bytes"


def test_no_node_exporter_metric_gets_a_unit_in_the_real_fixture():
    """None of the 16 node-exporter rows in this document state an
    explicit, recognized unit."""
    result = parse_vendor_docx(FIXTURE_PATH)
    node_units = {r.name: r.unit for r in result.records if r.exporter == "node-exporter"}
    assert all(u is None for u in node_units.values())


def test_row_with_invalid_type_is_rejected(tmp_path):
    import docx

    doc = docx.Document()
    table = doc.add_table(rows=2, cols=5)
    table.rows[0].cells[1].text = "Node Exporter Metric"
    table.rows[1].cells[0].text = "CPU"
    table.rows[1].cells[1].text = "node_made_up_metric"
    table.rows[1].cells[2].text = "NotAType"
    table.rows[1].cells[3].text = "Something"
    table.rows[1].cells[4].text = "A question"
    bad_type_path = tmp_path / "bad_type.docx"
    doc.save(str(bad_type_path))

    result = parse_vendor_docx(bad_type_path)
    assert result.records == ()
    assert len(result.rejected_rows) == 1
    assert "Prometheus Type" in result.rejected_rows[0].reason
