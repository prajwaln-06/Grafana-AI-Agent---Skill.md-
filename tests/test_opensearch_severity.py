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


# ---- distinct states; absence of evidence is never health (checkpoint-2 review, item 1) -----------------------


@pytest.mark.parametrize("sev,vendor,state", [
    ("OK", "Informational", "normal"),          # positive evidence from BOTH fields
    ("Warning", "Informational", "abnormal"),
    ("OK", "Critical", "abnormal"),
    ("OK", None, "unknown"),                    # one field missing
    (None, "Informational", "unknown"),
    (None, None, "unknown"),                    # nothing at all
    ("Bogus", "Informational", "unknown"),      # unrecognised
    ("OK", "Bogus", "unknown"),
    ("", "Informational", "unknown"),           # empty string counts as missing
    ("ok", "informational", "unknown"),         # wrong case is unrecognised, not normal
    (5, "Informational", "unknown"),            # wrong type is unrecognised, not normal
])
def test_state_table(ilo, sev, vendor, state):
    assert assess_hit(ilo.severity_semantics, ilo_doc(sev, vendor))["state"] == state


def test_one_normal_and_one_missing_field_is_not_healthy(ilo):
    a = assess_hit(ilo.severity_semantics, ilo_doc("OK"))
    assert a["state"] == "unknown" and not a["abnormal"]
    assert a["missing_fields"] == ["Oem.Hpe.Severity"]


def test_one_normal_and_one_unrecognised_field_is_not_healthy(ilo):
    a = assess_hit(ilo.severity_semantics, ilo_doc("OK", "Bogus"))
    assert a["state"] == "unknown" and a["unrecognised_fields"] == ["Oem.Hpe.Severity"]


def test_abnormal_beats_missing_and_unrecognised(ilo):
    for doc in (ilo_doc("Critical"), ilo_doc("Critical", "Bogus"), ilo_doc(None, "Caution")):
        assert assess_hit(ilo.severity_semantics, doc)["state"] == "abnormal"


def test_conflict_only_when_an_abnormal_field_meets_a_normal_one(ilo):
    assert assess_hit(ilo.severity_semantics, ilo_doc("Warning", "Informational"))["conflict"] is True
    assert assess_hit(ilo.severity_semantics, ilo_doc("OK", "Caution"))["conflict"] is True
    assert assess_hit(ilo.severity_semantics, ilo_doc("Critical", "Critical"))["conflict"] is False
    assert assess_hit(ilo.severity_semantics, ilo_doc("Critical"))["conflict"] is False  # missing is not "normal"
    assert assess_hit(ilo.severity_semantics, ilo_doc("OK", "Informational"))["conflict"] is False


def test_the_four_outcomes_are_distinguishable_per_field(ilo):
    classes = {assess_hit(ilo.severity_semantics, ilo_doc(*pair))["fields"]["Severity"]["class"]
               for pair in (("OK", "Informational"), ("Critical", "Informational"), (None, "Informational"),
                            ("Bogus", "Informational"))}
    assert classes == {"normal", "abnormal", "missing", "unrecognised"}


def test_negative_healthy_family_missing_is_unknown_not_healthy(os_registry):
    sem = os_registry.get("fabric_health").severity_semantics
    assert assess_hit(sem, {})["state"] == "unknown"
    assert assess_hit(sem, {"fields": {"PhysicalSubContext": ""}})["state"] == "unknown"


def test_controller_log_missing_or_unrecognised_severity_is_unknown(os_registry):
    sem = os_registry.get("controller_log").severity_semantics
    assert assess_hit(sem, {})["state"] == "unknown"
    assert assess_hit(sem, {"Severity": "bogus"})["state"] == "unknown"
    assert assess_hit(sem, {"Severity": "warning"})["state"] == "unknown"  # known-other: neither normal nor an error
    assert assess_hit(sem, {"Severity": "info"})["state"] == "normal"


# ---- provisional vocabulary ---------------------------------------------------------------------------------------


def test_vendor_severity_vocabulary_is_marked_provisional(ilo):
    by_path = {f.path: f for f in ilo.severity_semantics.fields}
    assert by_path["Oem.Hpe.Severity"].verification == "provisional"
    assert by_path["Severity"].verification == "documented"   # DMTF enum; only OK observed here
    assert "Informational" in by_path["Oem.Hpe.Severity"].normal


def test_no_registered_vocabulary_claims_more_than_it_has(os_registry):
    for pid in ("fabric_health", "controller_log", "ilo_events"):
        for f in os_registry.get(pid).severity_semantics.fields:
            assert f.verification in {"observed", "documented", "provisional"} and f.source


def test_abnormal_verdict_on_a_provisional_field_says_so(ilo):
    a = assess_hit(ilo.severity_semantics, ilo_doc("OK", "Caution"))
    assert a["provisional_basis"] == ["Oem.Hpe.Severity"]
    assert a["fields"]["Oem.Hpe.Severity"]["vocabulary"] == "provisional"
    assert assess_hit(ilo.severity_semantics, ilo_doc("Critical", "Informational"))["provisional_basis"] == []


def test_registry_rejects_a_vocabulary_without_a_verification_level(tmp_path, skills_root):
    import json
    from app.opensearch_catalog.registry import DEFAULT_REGISTRY_PATH, RegistryError, load_registry
    data = json.loads(DEFAULT_REGISTRY_PATH.read_text())
    del data["patterns"][2]["severity_semantics"]["fields"]["Severity"]["verification"]
    bad = tmp_path / "r.json"
    bad.write_text(json.dumps(data))
    with pytest.raises(RegistryError, match="verification"):
        load_registry(bad, skills_root=skills_root)


def test_prompt_flags_provisional_vocabulary(os_ctx):
    from app.opensearch_catalog.context import format_prompt_block
    assert "PROVISIONAL" in format_prompt_block(os_ctx("ilo_events"))
    assert "PROVISIONAL" not in format_prompt_block(os_ctx("controller_log"))


def test_result_metadata_flags_provisional_vocabulary(os_registry):
    from app.opensearch_catalog.redaction import RedactionPolicy
    from app.opensearch_catalog.results import normalize_search_response
    raw = {"took": 1, "timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0},
           "hits": {"total": {"value": 1, "relation": "eq"},
                    "hits": [{"_source": {"@timestamp": "2026-10-28T10:00:00Z", "Severity": "OK",
                                          "Oem": {"Hpe": {"Severity": "Caution"}}, "Message": "m"}}]}}
    r = normalize_search_response(raw, entry=os_registry.get("ilo_events"), query={}, policy=RedactionPolicy(),
                                  max_series=10, max_hits=10)
    assert r.metadata["severity_vocabulary_provisional"] == ["Oem.Hpe.Severity"]
    assert r.metadata["severity_vocabulary"]["Severity"] == "documented"
    assert r.hits[0].severity_assessment["provisional_basis"] == ["Oem.Hpe.Severity"]
    other = dict(raw)
    r2 = normalize_search_response({**raw, "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}},
                                   entry=os_registry.get("controller_log"), query={}, policy=RedactionPolicy(),
                                   max_series=10, max_hits=10)
    assert "severity_vocabulary_provisional" not in r2.metadata
