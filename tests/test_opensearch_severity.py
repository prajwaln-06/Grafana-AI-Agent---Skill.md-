"""Severity semantics: per-row assessment (assess_hit) and deterministic expansion of the
`abnormal_severity` named condition. Abnormal value sets for iLO `Oem.Hpe.Severity` are an
UNVERIFIED assumption recorded in the registry (see its `source` fields); these tests verify
the logic, not the live vocabulary."""
import pytest

from app.opensearch_catalog.severity import assess_hit, classify_value, expand_abnormal_condition


@pytest.fixture()
def ilo(os_registry):
    return os_registry.get("ilo_events")


def ilo_doc(sev=None, vendor=None):
    d = {}
    if sev is not None:
        d["Severity"] = sev
    if vendor is not None:
        d["Oem"] = {"Hpe": {"Severity": vendor}}
    return d


def test_both_normal_is_normal(ilo):
    a = assess_hit(ilo.severity_semantics, ilo_doc("OK", "Informational"))
    assert a["state"] == "normal" and not a["abnormal"] and not a["conflict"]


@pytest.mark.parametrize("sev,vendor,basis", [
    ("Warning", "Informational", ["Severity"]),
    ("Critical", "Informational", ["Severity"]),
    ("OK", "Critical", ["Oem.Hpe.Severity"]),
    ("OK", "Caution", ["Oem.Hpe.Severity"]),
    ("Critical", "Critical", ["Severity", "Oem.Hpe.Severity"]),
])
def test_either_field_abnormal_makes_the_event_abnormal(ilo, sev, vendor, basis):
    a = assess_hit(ilo.severity_semantics, ilo_doc(sev, vendor))
    assert a["state"] == "abnormal" and a["abnormal"]
    assert sorted(a["basis"]) == sorted(basis)


def test_disagreeing_fields_are_flagged_as_a_conflict_not_hidden(ilo):
    a = assess_hit(ilo.severity_semantics, ilo_doc("Warning", "Informational"))
    assert a["conflict"] is True
    assert a["fields"]["Severity"]["class"] == "abnormal"
    assert a["fields"]["Oem.Hpe.Severity"]["class"] == "normal"


def test_agreeing_fields_are_not_a_conflict(ilo):
    assert not assess_hit(ilo.severity_semantics, ilo_doc("Critical", "Critical"))["conflict"]


def test_missing_field_is_reported_explicitly(ilo):
    a = assess_hit(ilo.severity_semantics, ilo_doc("OK"))
    assert a["missing_fields"] == ["Oem.Hpe.Severity"]
    assert a["fields"]["Oem.Hpe.Severity"]["class"] == "missing"


def test_missing_field_does_not_hide_an_abnormal_other_field(ilo):
    a = assess_hit(ilo.severity_semantics, ilo_doc("Critical"))
    assert a["state"] == "abnormal" and a["missing_fields"] == ["Oem.Hpe.Severity"]


def test_all_fields_missing_is_unknown_not_normal(ilo):
    a = assess_hit(ilo.severity_semantics, {})
    assert a["state"] == "unknown" and not a["abnormal"]


def test_unrecognised_value_is_neither_normal_nor_abnormal(ilo):
    a = assess_hit(ilo.severity_semantics, ilo_doc("Bogus", "Informational"))
    assert a["state"] == "unknown" and a["unrecognised_fields"] == ["Severity"]
    assert not a["abnormal"]


def test_non_empty_value_is_not_automatically_abnormal(ilo):
    for v in ("Bogus", "x", "Maybe"):
        assert not assess_hit(ilo.severity_semantics, ilo_doc(v, "Informational"))["abnormal"]


def test_case_must_match_stored_value(ilo):
    f = next(x for x in ilo.severity_semantics.fields if x.path == "Severity")
    assert classify_value(f, "Critical", "positive_abnormal") == "abnormal"
    assert classify_value(f, "critical", "positive_abnormal") == "unrecognised"


def test_controller_log_error_is_abnormal_warning_is_not(os_registry):
    sem = os_registry.get("controller_log").severity_semantics
    assert assess_hit(sem, {"Severity": "error"})["abnormal"]
    assert assess_hit(sem, {"Severity": "critical"})["abnormal"]
    assert not assess_hit(sem, {"Severity": "warning"})["abnormal"]
    assert not assess_hit(sem, {"Severity": "info"})["abnormal"]


def test_fabric_health_unhealthy_is_anything_not_healthy(os_registry):
    sem = os_registry.get("fabric_health").severity_semantics
    assert assess_hit(sem, {"fields": {"PhysicalSubContext": "CRITICAL"}})["abnormal"]
    assert not assess_hit(sem, {"fields": {"PhysicalSubContext": "HEALTHY"}})["abnormal"]
    assert assess_hit(sem, {})["state"] == "unknown"


def test_expansion_for_ilo_is_an_or_of_recognised_abnormal_values(os_ctx):
    pc = os_ctx("ilo_events").patterns["ilo_events"]
    clause, _ = expand_abnormal_condition(pc.entry, pc.view)
    should = clause["bool"]["should"]
    assert len(should) == 2 and clause["bool"]["minimum_should_match"] == 1
    values = {v for c in should for t in c["terms"].values() for v in t}
    assert values == {"Warning", "Critical", "Caution"}
