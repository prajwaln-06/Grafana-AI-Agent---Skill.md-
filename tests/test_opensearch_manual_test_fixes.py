"""Regression tests for the failures found by the 2026-11-10 manual test run (31 questions).

Issue 1  CL-03   "Are the same errors recurring on a controller?"   nested host -> service, honest labelling
Issue 2  CL-07   "Did link-down events occur near fabric availability problems?"   time-proximity correlation
Issue 3  ILO-05  "Which BMCs have unusually frequent login/logout?"   frequency yes, anomaly no
Issue 4  ILO-06  "Any failed login attempts?"                      stays unmapped (no verified failure wording)
Issue 5  ILO-09  unrepaired warning/critical events                effective field path, never a fabricated `.keyword`
Issue 6  ILO-10  most recurring event codes per host               ONE top-level aggregation, nested

Everything here runs against SYNTHETIC fixture mappings and a FAKE cluster: it proves the code paths, not
behaviour on a live cluster or what the real model writes.
"""
import copy
import json
from pathlib import Path

import pytest

from app import executor, validator
from app.opensearch_catalog import correlation, guard
from app.opensearch_catalog.context import Limits, OpenSearchContext, PatternContext
from app.opensearch_catalog.field_catalog import build_field_catalog, build_field_view
from app.opensearch_catalog.registry import RegistryError, load_registry
from tests.os_fixtures import FIX, INDEX, REF, agg_body, error, hits_body, os_entry, window
from tests.test_opensearch_mentor_questions import ABN, CTL, F, ILO, FAB, ask

ROOT = Path(__file__).resolve().parent.parent
REFS = ROOT / "skills" / "references" / "opensearch-logs"


# ---------------------------------------------------------------------------------------- helpers


def custom_ctx(os_registry, pid, mutate=None):
    """A context for one family whose live mapping is the fixture mapping after `mutate(properties)`."""
    mapping = json.loads((FIX / f"mapping_{pid}.json").read_text())
    for body in mapping.values():
        if mutate:
            mutate(body["mappings"]["properties"])
    entry = os_registry.get(pid)
    view = build_field_view(entry, build_field_catalog(pid, mapping))
    return OpenSearchContext(patterns={pid: PatternContext(entry=entry, view=view)}, limits=Limits())


def check(ctx, pid, query, **kw):
    return guard.check_entry({"status": "ok", "data_source": "opensearch", "pattern_id": pid, "query": query, **kw}, ctx)


def hpe(props):
    return props["Oem"]["properties"]["Hpe"]["properties"]


def rows_query(*clauses, size=20, source=None):
    q = {"size": size, "query": {"bool": {"filter": [window(), *clauses]}}}
    if source:
        q["_source"] = source
    return q


# =============================================================================== Issue 5: ILO-09


REPAIRED = {"term": {"Oem.Hpe.Repaired": False}}
TEXT_KW = {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}}


def test_repaired_boolean_mapping_takes_the_bare_path_and_a_json_boolean(os_ctx):
    r = check(os_ctx("ilo_events"), "ilo_events", rows_query(ABN, REPAIRED))
    assert r.ok, r.reason
    assert {"term": {"Oem.Hpe.Repaired": False}} in r.query["query"]["bool"]["filter"]


def test_a_fabricated_keyword_path_on_a_boolean_field_is_rejected_with_a_useful_hint(os_ctx):
    r = check(os_ctx("ilo_events"), "ilo_events", rows_query(ABN, {"term": {"Oem.Hpe.Repaired.keyword": False}}))
    assert not r.ok and r.code == guard.CODE_UNKNOWN_FIELD
    assert "Oem.Hpe.Repaired" in r.reason and "no `.keyword` sub-field" in r.reason and "boolean" in r.reason


def test_the_fabricated_path_is_still_rejected_when_the_base_field_is_missing(os_registry):
    ctx = custom_ctx(os_registry, "ilo_events", lambda p: hpe(p).pop("Repaired"))
    r = check(ctx, "ilo_events", rows_query(ABN, REPAIRED))
    assert not r.ok and r.code == guard.CODE_UNKNOWN_FIELD and "not present in the live mapping" in r.reason
    r2 = check(ctx, "ilo_events", rows_query(ABN, {"term": {"Oem.Hpe.Repaired.keyword": "false"}}))
    assert not r2.ok and r2.code == guard.CODE_UNKNOWN_FIELD


def test_repaired_as_a_keyword_mapping_uses_the_bare_path_and_a_string(os_registry):
    ctx = custom_ctx(os_registry, "ilo_events", lambda p: hpe(p).update(Repaired={"type": "keyword"}))
    ok = check(ctx, "ilo_events", rows_query(ABN, {"term": {"Oem.Hpe.Repaired": "false"}}))
    assert ok.ok, ok.reason
    bad = check(ctx, "ilo_events", rows_query(ABN, {"term": {"Oem.Hpe.Repaired.keyword": "false"}}))
    assert not bad.ok and bad.code == guard.CODE_UNKNOWN_FIELD


def test_repaired_as_text_with_a_confirmed_keyword_subfield_is_substituted_not_assumed(os_registry):
    ctx = custom_ctx(os_registry, "ilo_events", lambda p: hpe(p).update(Repaired=copy.deepcopy(TEXT_KW)))
    r = check(ctx, "ilo_events", rows_query(ABN, {"term": {"Oem.Hpe.Repaired": "false"}}))
    assert r.ok, r.reason
    assert {"term": {"Oem.Hpe.Repaired.keyword": "false"}} in r.query["query"]["bool"]["filter"]
    assert any("Repaired" in a for a in r.applied)          # the substitution is recorded, never silent


def test_an_operation_the_boolean_field_does_not_support_is_rejected(os_ctx):
    r = check(os_ctx("ilo_events"), "ilo_events", rows_query(ABN, {"match": {"Oem.Hpe.Repaired": "false"}}))
    assert not r.ok and r.code in (guard.CODE_OPERATION_NOT_SUPPORTED, guard.CODE_BAD_CLAUSE)
    r2 = check(os_ctx("ilo_events"), "ilo_events", rows_query(ABN, {"prefix": {"Oem.Hpe.Repaired": "fal"}}))
    assert not r2.ok


def test_unrepaired_query_combines_the_abnormal_condition_and_never_admits_ok_or_informational(os_ctx):
    r = check(os_ctx("ilo_events"), "ilo_events", rows_query(ABN, REPAIRED))
    flat = json.dumps(r.query)
    assert '"minimum_should_match": 1' in flat
    for abnormal in ("Warning", "Critical", "Caution"):
        assert abnormal in flat
    assert "Informational" not in flat and '"OK"' not in flat
    assert "named_condition" not in flat                      # expanded by the system


def test_unrepaired_query_projects_the_recommended_action_and_rows_return_it(os_ctx, os_registry, settings):
    r = check(os_ctx("ilo_events"), "ilo_events", rows_query(ABN, REPAIRED))
    assert "Oem.Hpe.RecommendedAction" in r.query["_source"] and "Oem.Hpe.Repaired" not in r.query["_source"]
    from app.opensearch_catalog.redaction import RedactionPolicy
    from app.opensearch_catalog.results import normalize_search_response
    doc = {"@timestamp": "2026-11-10T10:00:00Z", "host": "bmc1", "Severity": "Warning",
           "Oem": {"Hpe": {"Severity": "Caution", "Code": 1, "Repaired": False,
                           "RecommendedAction": "Replace the DIMM."}}, "Message": "Correctable memory error"}
    res = normalize_search_response(hits_body([doc]), entry=os_registry.get("ilo_events"), query=r.query,
                                    policy=RedactionPolicy(mask_usernames=False, ip_mode="off"), max_series=50, max_hits=50)
    assert res.hits[0].fields["Oem.Hpe.RecommendedAction"] == "Replace the DIMM."


def test_hand_written_severity_exclusion_is_still_refused(os_ctx):
    bad = rows_query(REPAIRED)
    bad["query"]["bool"]["must_not"] = [{"terms": {"Oem.Hpe.Severity.keyword": ["OK", "Informational"]}}]
    r = check(os_ctx("ilo_events"), "ilo_events", bad)
    assert not r.ok


# =============================================================================== Issue 6: ILO-10


def nested_host_code(host="host.keyword", code="Oem.Hpe.Code", filters=()):
    return {"size": 0, "query": {"bool": {"filter": [window(), *filters]}},
            "aggs": {"by_host": {"terms": {"field": host, "size": 20},
                                 "aggs": {"by_event_code": {"terms": {"field": code, "size": 10}}}}}}


def test_one_top_level_aggregation_with_a_nested_terms_is_accepted(os_ctx):
    r = check(os_ctx("ilo_events"), "ilo_events", nested_host_code())
    assert r.ok, r.reason
    assert list(r.query["aggs"]) == ["by_host"] and list(r.query["aggs"]["by_host"]["aggs"]) == ["by_event_code"]


def test_two_sibling_top_level_aggregations_are_rejected_and_the_hint_says_to_nest(os_ctx):
    q = {"size": 0, "query": {"bool": {"filter": [window()]}},
         "aggs": {"by_host": {"terms": {"field": "host.keyword", "size": 20}},
                  "by_code": {"terms": {"field": "Oem.Hpe.Code", "size": 10}}}}
    r = check(os_ctx("ilo_events"), "ilo_events", q)
    assert not r.ok and r.code == guard.CODE_AGGREGATION
    assert "exactly ONE top-level aggregation" in r.reason and "NEST" in r.reason


def test_both_levels_must_be_groupable(os_ctx, os_registry):
    ctx = os_ctx("ilo_events")
    assert not check(ctx, "ilo_events", nested_host_code(host="Message")).ok            # free text is never grouped
    assert not check(ctx, "ilo_events", nested_host_code(code="Message.keyword")).ok
    # a code field missing from the live mapping cannot be grouped on either
    gone = custom_ctx(os_registry, "ilo_events", lambda p: hpe(p).pop("Code"))
    r = check(gone, "ilo_events", nested_host_code())
    assert not r.ok and r.code == guard.CODE_UNKNOWN_FIELD


def test_terms_size_limits_still_apply_to_the_nested_level(os_ctx):
    q = nested_host_code()
    q["aggs"]["by_host"]["aggs"]["by_event_code"]["terms"]["size"] = 100000
    assert not check(os_ctx("ilo_events"), "ilo_events", q).ok


def test_nested_host_event_code_result_keeps_the_hierarchy_and_reports_truncation(os_ctx, os_registry):
    from app.opensearch_catalog.redaction import RedactionPolicy
    from app.opensearch_catalog.results import normalize_search_response
    q = check(os_ctx("ilo_events"), "ilo_events", nested_host_code()).query
    raw = agg_body({"by_host": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 7, "buckets": [
        {"key": "bmc1", "doc_count": 9, "by_event_code": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 2,
         "buckets": [{"key": 1017, "doc_count": 5}, {"key": 1018, "doc_count": 2}]}},
        {"key": "bmc2", "doc_count": 3, "by_event_code": {"sum_other_doc_count": 0,
         "buckets": [{"key": 1017, "doc_count": 3}]}}]}}, total=19)
    res = normalize_search_response(raw, entry=os_registry.get("ilo_events"), query=q,
                                    policy=RedactionPolicy(mask_usernames=False, ip_mode="off"), max_series=50, max_hits=50)
    assert res.result_type == "buckets"
    assert [(b.key, b.doc_count, [(s.key, s.doc_count) for s in b.sub_buckets]) for b in res.buckets] == [
        ("bmc1", 9, [("1017", 5), ("1018", 2)]), ("bmc2", 3, [("1017", 3)])]
    agg = res.metadata["aggregation"]
    assert agg["approximate"] and agg["sum_other_doc_count"] == 9 and res.truncated
    assert any("outside the returned buckets" in r for r in agg["approximate_reasons"])


# =============================================================================== Issue 1: CL-03


def host_service(host="Resource.host.name.keyword", service="Resource.service.name.keyword"):
    return {"size": 0, "query": {"bool": {"filter": [window(), ABN]}},
            "aggs": {"by_host": {"terms": {"field": host, "size": 20},
                                 "aggs": {"by_service": {"terms": {"field": service, "size": 5}}}}}}


def test_recurring_errors_is_host_then_service_with_the_abnormal_severity_set(os_ctx, os_registry):
    r = check(os_ctx("controller_log"), "controller_log", host_service())
    assert r.ok, r.reason
    assert list(r.query["aggs"]["by_host"]["aggs"]) == ["by_service"]
    flat = json.dumps(r.query)
    sem = os_registry.get("controller_log").severity_semantics.fields[0]
    for value in sem.abnormal:
        assert value in flat                       # the registered abnormal set, not a hand-written one
    for value in sem.normal | sem.other_known:
        assert f'"{value}"' not in flat            # info/notice/warning rows are not "errors"


def test_grouping_by_the_message_body_is_rejected(os_ctx):
    ctx = os_ctx("controller_log")
    for field in ("Body", "Body.keyword"):
        q = host_service(service=field)
        r = check(ctx, "controller_log", q)
        assert not r.ok, field


def test_missing_or_non_groupable_host_and_service_fields_are_handled(os_ctx, os_registry):
    gone = custom_ctx(os_registry, "controller_log", lambda p: p["Resource"]["properties"].pop("service"))
    r = check(gone, "controller_log", host_service())
    assert not r.ok and r.code == guard.CODE_UNKNOWN_FIELD
    no_kw = custom_ctx(os_registry, "controller_log",
                       lambda p: p["Resource"]["properties"]["host"]["properties"].update(name={"type": "text"}))
    r2 = check(no_kw, "controller_log", host_service())
    assert not r2.ok            # a text field without a confirmed keyword sub-field cannot be grouped


def test_the_generic_recurrence_analysis_is_not_extended_to_controller_logs(os_ctx):
    q = host_service()
    r = check(os_ctx("controller_log"), "controller_log", q, analysis={"type": "recurrence"})
    assert not r.ok and r.code == guard.CODE_ANALYSIS
    assert "recurrence" not in os_ctx("controller_log").patterns["controller_log"].entry.analyses


def test_the_result_says_what_it_establishes_and_what_it_does_not(os_ctx, os_registry):
    from app.opensearch_catalog.redaction import RedactionPolicy
    from app.opensearch_catalog.results import normalize_search_response
    q = check(os_ctx("controller_log"), "controller_log", host_service()).query
    raw = agg_body({"by_host": {"buckets": [{"key": "x1000c0s0b0", "doc_count": 12,
                    "by_service": {"buckets": [{"key": "hmscd", "doc_count": 12}]}}]}}, total=12)
    res = normalize_search_response(raw, entry=os_registry.get("controller_log"), query=q,
                                    policy=RedactionPolicy(mask_usernames=False, ip_mode="off"), max_series=50, max_hits=50)
    interp = res.metadata["aggregation"]["interpretation"]
    assert [g["role"] for g in interp["grouped_by"]] == ["entity_host", "service"]
    assert interp["count_unit"] == "documents"
    text = " ".join(interp["does_not_establish"])
    assert "Body" in text and "same message text" in text and "identical message" in text
    assert interp["anomaly_detection"]["performed"] is False


def test_cl03_end_to_end_runs_one_nested_query_and_returns_labelled_buckets(skill_index, settings, monkeypatch):
    contract = {"mode": "single", **os_entry("controller_log", host_service(), intent="top_n")}
    handler = lambda i, d: agg_body({"by_host": {"buckets": [{"key": "h1", "doc_count": 4, "by_service": {  # noqa: E731
        "buckets": [{"key": "hmscd", "doc_count": 4}]}}]}}, total=4)
    _, executed, cluster = ask(skill_index, settings, monkeypatch, "Are the same errors recurring on a controller?",
                               CTL, contract, handler)
    block = executed["execution"]
    assert block["execution_status"] == "success" and block["buckets"][0]["sub_buckets"][0]["key"] == "hmscd"
    assert len(cluster.searches) == 1
    assert "interpretation" in block["metadata"]["aggregation"]


# =============================================================================== Issue 3: ILO-05


def frequency_query():
    return {"size": 0, "query": {"bool": {"filter": [window(), {"terms": {"Oem.Hpe.Code": [1017, 1018]}}]}},
            "aggs": {"by_host": {"terms": {"field": "host.keyword", "size": 20},
                                 "aggs": {"by_event_code": {"terms": {"field": "Oem.Hpe.Code", "size": 2}}}}}}


def test_login_logout_frequency_is_a_valid_nested_count_per_bmc(os_ctx):
    r = check(os_ctx("ilo_events"), "ilo_events", frequency_query())
    assert r.ok, r.reason


def _ilo_result(os_registry, buckets):
    from app.opensearch_catalog.redaction import RedactionPolicy
    from app.opensearch_catalog.results import normalize_search_response
    return normalize_search_response(agg_body({"by_host": {"buckets": buckets}}, total=20), query=frequency_query(),
                                     entry=os_registry.get("ilo_events"),
                                     policy=RedactionPolicy(mask_usernames=False, ip_mode="off"), max_series=50, max_hits=50)


def test_login_and_logout_are_counted_separately_and_labelled_from_verified_codes(os_registry):
    res = _ilo_result(os_registry, [{"key": "bmc1", "doc_count": 9, "by_event_code": {"buckets": [
        {"key": 1017, "doc_count": 6}, {"key": 1018, "doc_count": 3}]}}])
    assert [(s.key, s.doc_count) for s in res.buckets[0].sub_buckets] == [("1017", 6), ("1018", 3)]
    labels = res.metadata["aggregation"]["interpretation"]["event_code_labels"]
    assert labels["1017"]["kind"] == "login" and labels["1018"]["kind"] == "logout"
    assert labels["1017"] != labels["1018"]


def test_a_code_without_verified_meaning_is_never_labelled_as_login_or_logout(os_registry):
    res = _ilo_result(os_registry, [{"key": "bmc1", "doc_count": 4, "by_event_code": {"buckets": [
        {"key": 1017, "doc_count": 3}, {"key": 9999, "doc_count": 1}]}}])
    interp = res.metadata["aggregation"]["interpretation"]
    assert "9999" not in interp["event_code_labels"] and interp["event_codes_without_verified_meaning"] == ["9999"]


def test_a_frequency_ranking_is_never_marked_as_an_anomaly(os_registry):
    res = _ilo_result(os_registry, [{"key": "bmc1", "doc_count": 99, "by_event_code": {"buckets": [
        {"key": 1017, "doc_count": 99}]}}])
    interp = res.metadata["aggregation"]["interpretation"]
    assert interp["anomaly_detection"] == {"performed": False,
                                           "reason": "no baseline or threshold is defined for this index family"}
    assert "unusual" in " ".join(interp["does_not_establish"]) and "no anomaly detection" in " ".join(interp["does_not_establish"])


def test_an_anomaly_analysis_is_rejected_because_no_baseline_exists(os_ctx):
    r = check(os_ctx("ilo_events"), "ilo_events", frequency_query(), analysis={"type": "anomaly"})
    assert not r.ok and r.code == guard.CODE_ANALYSIS
    r2 = check(os_ctx("ilo_events"), "ilo_events", frequency_query(), analysis={"type": "baseline"})
    assert not r2.ok


def test_the_registry_has_no_anomaly_baseline_and_lists_only_evidenced_codes(os_registry):
    e = os_registry.get("ilo_events")
    assert set(e.analyses) == {"ingest_lag"}
    assert set(e.event_codes) == {"1017", "1018"} and all(v["evidence"] for v in e.event_codes.values())


@pytest.mark.parametrize("body,fragment", [
    ({"1017": {"meaning": "x", "kind": "login", "evidence": ""}}, "evidence"),
    ({"1017": {"meaning": "x", "kind": "failed_login", "evidence": "e"}}, "kind"),
    ({"abc": {"meaning": "x", "kind": "login", "evidence": "e"}}, "numeric"),
])
def test_the_registry_refuses_an_event_code_without_evidence_or_with_an_unknown_kind(tmp_path, body, fragment):
    from app.opensearch_catalog.registry import DEFAULT_REGISTRY_PATH
    data = json.loads(DEFAULT_REGISTRY_PATH.read_text())
    next(p for p in data["patterns"] if p["pattern_id"] == "ilo_events")["event_codes"] = body
    path = tmp_path / "r.json"
    path.write_text(json.dumps(data))
    with pytest.raises(RegistryError, match=fragment):
        load_registry(path)


# =============================================================================== Issue 4: ILO-06


def test_failed_login_stays_unmapped_in_the_reference_and_registry():
    text = (REFS / "ilo-events.md").read_text(encoding="utf-8")
    assert "Failed logins are not supported" in text and '"status": "unmapped"' in text
    assert "never `unsupported_metric`" in text
    registry = json.loads((ROOT / "app" / "opensearch_catalog" / "index_registry.json").read_text())
    ilo = next(p for p in registry["patterns"] if p["pattern_id"] == "ilo_events")
    kinds = {v["kind"] for v in ilo["event_codes"].values()}
    assert kinds == {"login", "logout"}                  # no failure code is listed: none has been observed
    for banned in ("login failed", "authentication failure", "failed login\"", "invalid password"):
        assert banned not in json.dumps(ilo).lower()


def test_failed_login_unmapped_sends_nothing_to_opensearch(skill_index, settings, monkeypatch):
    contract = {"mode": "single", "status": "unmapped",
                "explanation": "The failed-login event has not been verified in the data, so it is not supported."}
    result, executed, cluster = ask(skill_index, settings, monkeypatch, "Any failed login attempts?", ILO, contract)
    assert result["status"] == "unmapped" and "execution" not in executed and cluster.searches == []


def test_the_prompt_tells_the_generator_unsupported_log_questions_are_unmapped_not_unsupported_metric(os_ctx):
    from app.opensearch_catalog.context import format_prompt_block as render_prompt_block
    block = render_prompt_block(os_ctx("ilo_events"))
    assert 'Never use "unsupported_metric" for OpenSearch' in block
    assert "NEST the second `terms`" in block and "has NO `.keyword` sub-field" in block


# =============================================================================== Issue 2: CL-07


def link_down_entry(**kw):
    return os_entry("controller_log", {"size": 50, "query": F({"match_phrase": {"Body": "LinkStatus Down"}})},
                    intent="rows", **kw)


def fabric_entry(**kw):
    return os_entry("fabric_health", {"size": 50, "query": F(ABN)}, intent="rows", **kw)


def multi(tolerance="5m", **corr):
    c = {"type": "temporal_proximity", "left": 0, "right": 1}
    if tolerance is not None:
        c["tolerance"] = tolerance
    c.update(corr)
    return {"mode": "multi", "results": [link_down_entry(), fabric_entry()], "synthesis": None, "correlation": c}


def validate(contract, os_ctx):
    return validator.validate_contract(contract, known_references={REF["controller_log"], REF["fabric_health"]},
                                       known_datasources={"opensearch"}, opensearch_context=os_ctx("controller_log",
                                                                                                    "fabric_health"))


def canon(contract, os_ctx):
    from app.opensearch_catalog.canonical import canonicalise_contract
    return canonicalise_contract(contract, os_ctx("controller_log", "fabric_health"))


def test_two_valid_entries_from_different_families_pass_validation(os_ctx):
    v = validate(canon(multi(), os_ctx), os_ctx)
    assert v.passed, v.reason


def test_a_missing_status_is_reported_with_the_offending_entry(os_ctx):
    c = multi()
    del c["results"][1]["status"]
    v = validate(c, os_ctx)
    assert not v.passed and "not an object with a 'status' field" in v.reason and "results[1]" in v.reason


def test_a_non_object_entry_is_rejected(os_ctx):
    c = multi()
    c["results"][1] = "fabric_health: abnormal events"
    v = validate(c, os_ctx)
    assert not v.passed and "not an object with a 'status' field" in v.reason and "results[1] is a str" in v.reason


def test_invalid_result_counts_are_rejected(os_ctx):
    one = multi()
    one["results"] = one["results"][:1]
    assert not validate(one, os_ctx).passed
    none = multi()
    none["results"] = "nope"
    assert not validate(none, os_ctx).passed


def test_a_missing_synthesis_field_is_rejected(os_ctx):
    c = canon(multi(), os_ctx)
    del c["synthesis"]
    v = validate(c, os_ctx)
    assert not v.passed and "synthesis" in v.reason


@pytest.mark.parametrize("mutate,fragment", [
    (lambda c: c["correlation"].update(tolerance="soon"), "tolerance"),
    (lambda c: c["correlation"].update(tolerance="2h"), "exceeds the maximum"),
    (lambda c: c["correlation"].update(window="5m"), "unknown key"),
    (lambda c: c["correlation"].update(type="pearson"), "type"),
    (lambda c: c["correlation"].update(left=0, right=0), "two different entries"),
    (lambda c: c["correlation"].update(right=7), "index"),
    (lambda c: c["correlation"].update(left=True), "index"),
    (lambda c: c.update(correlation="near"), "must be an object"),
])
def test_malformed_correlation_parameters_are_rejected_deterministically(os_ctx, mutate, fragment):
    c = canon(multi(), os_ctx)
    mutate(c)
    v = validate(c, os_ctx)
    assert not v.passed and fragment in v.reason, v.reason


def test_both_entries_must_be_different_families_and_plain_rows_queries():
    """Spec-level checks (before the guard sees the entries)."""
    c = multi()
    c["results"][1]["pattern_id"] = "controller_log"
    assert "DIFFERENT" in correlation.validate_spec(c, 3600)
    c = multi()
    c["results"][1]["query"] = {"size": 0, "aggs": {"a": {"terms": {"field": "x"}}}}
    assert "without aggregations" in correlation.validate_spec(c, 3600)
    c = multi()
    c["results"][1]["query"]["size"] = 0
    assert "size" in correlation.validate_spec(c, 3600)
    c = multi()
    c["results"][0]["analysis"] = {"type": "absence"}
    assert "not an analysis" in correlation.validate_spec(c, 3600)


def test_a_correlation_in_a_single_mode_contract_is_rejected(os_ctx):
    c = {"mode": "single", **link_down_entry(), "correlation": {"type": "temporal_proximity", "left": 0, "right": 1,
                                                               "tolerance": "5m"}}
    v = validate(canon(c, os_ctx), os_ctx)
    assert not v.passed and "multi" in v.reason


def test_a_missing_tolerance_becomes_a_clarification_never_a_default(os_ctx):
    out = canon(multi(tolerance=None), os_ctx)
    assert out["status"] == "declined" and out["reason"] == "parameter_requires_clarification"
    assert "How close in time" in out["clarification"] and "No default" in out["clarification"]
    assert validate(out, os_ctx).passed               # a well-formed declined entry


def test_the_five_minute_context_window_default_is_not_used_as_a_tolerance(os_ctx):
    out = canon(multi(tolerance=None), os_ctx)
    assert "tolerance_seconds" not in out and "correlation" not in out and out["status"] == "declined"
    assert "results" not in out                      # nothing runnable is left behind


# ---- execution: pure-Python correlation over the executed rows


def hit(ts, **fields):
    return {"timestamp": ts, "fields": fields, "body": None}


def executed(rows, status="success", total=None, partial=False, error_text=None, pid="x"):
    ex = {"execution_status": status, "metadata": {"partial": partial, "count": {"value": total if total is not None else len(rows)},
                                                    "rows": {"more_available": (total or len(rows)) > len(rows)}}}
    if rows or status == "success":
        ex["hits"] = rows
    if error_text:
        ex["error"] = error_text
    return {"status": "ok", "pattern_id": pid, "execution": ex}


SPEC = {"type": "temporal_proximity", "left": 0, "right": 1, "tolerance": "5m"}


def run(left, right, spec=SPEC):
    return correlation.run(spec, [left, right], max_pairs=100)


def test_matching_timestamps_produce_labelled_pairs_with_signed_deltas():
    left = executed([hit("2026-11-10T10:00:00Z", Body="LinkStatus Down")], pid="controller_log")
    right = executed([hit("2026-11-10T10:03:00Z", Switch="s1"), hit("2026-11-10T09:30:00Z", Switch="s2")], pid="fabric_health")
    out = run(left, right)
    assert out["outcome"] == "correlated" and out["complete"] is True
    (pair,) = out["pairs"]
    assert pair["delta_seconds"] == 180 and pair["left"]["source"] == "controller_log" and pair["right"]["source"] == "fabric_health"
    assert pair["right"]["fields"] == {"Switch": "s1"} and pair["right_events_within_tolerance"] == 1
    assert "ingest" in out["timestamp_basis"] and out["tolerance"] == "5m"


def test_no_matching_timestamps_is_none_found_and_not_confused_with_failure():
    left = executed([hit("2026-11-10T10:00:00Z")], pid="controller_log")
    right = executed([hit("2026-11-10T11:00:00Z")], pid="fabric_health")
    out = run(left, right)
    assert out["outcome"] == "none_found" and out["complete"] is True and out["pairs"] == []


def test_the_tolerance_is_respected_at_the_boundary():
    left = executed([hit("2026-11-10T10:00:00Z")])
    assert run(left, executed([hit("2026-11-10T10:05:00Z")]))["outcome"] == "correlated"
    assert run(left, executed([hit("2026-11-10T10:05:01Z")]))["outcome"] == "none_found"
    assert run(left, executed([hit("2026-11-10T10:05:01Z")]), {**SPEC, "tolerance": "6m"})["outcome"] == "correlated"


def test_one_failed_query_makes_the_answer_not_determinable_never_no_correlation():
    ok = executed([hit("2026-11-10T10:00:00Z")], pid="controller_log")
    bad = executed([], status="endpoint_error", error_text="connection refused", pid="fabric_health")
    for left, right in ((ok, bad), (bad, ok)):
        out = run(left, right)
        assert out["outcome"] == "not_determinable" and out["complete"] is False
        assert "NOT a finding of no correlation" in out["reason"] and "connection refused" in out["reason"]
        assert out["pairs"] == []


def test_an_empty_source_in_a_complete_scan_is_none_found():
    out = run(executed([hit("2026-11-10T10:00:00Z")]), executed([], status="empty_result"))
    assert out["outcome"] == "none_found" and out["sources"]["right"]["events_examined"] == 0


def test_a_truncated_source_cannot_rule_a_correlation_out():
    left = executed([hit("2026-11-10T10:00:00Z")], total=1)
    right = executed([hit("2026-11-10T12:00:00Z")], total=500)            # 500 matched, 1 row returned
    out = run(left, right)
    assert out["outcome"] == "none_found_in_examined_rows" and out["complete"] is False
    assert any("not examined in full" in w for w in out["warnings"])
    found = run(left, executed([hit("2026-11-10T10:01:00Z")], total=500))
    assert found["outcome"] == "correlated" and found["complete"] is False   # pairs are real; the set may be incomplete


def test_a_partial_response_is_never_complete():
    out = run(executed([hit("2026-11-10T10:00:00Z")], partial=True), executed([hit("2026-11-10T11:00:00Z")]))
    assert out["outcome"] == "none_found_in_examined_rows"


def test_duplicates_and_rows_without_timestamps_are_handled_and_reported():
    dup = hit("2026-11-10T10:00:00Z", Switch="s1")
    left = executed([hit("2026-11-10T10:00:00Z")])
    right = executed([dup, dict(dup), {"timestamp": None, "fields": {}}, {"fields": {}}])
    out = run(left, right)
    assert out["sources"]["right"]["duplicates_collapsed"] == 1 and out["sources"]["right"]["rows_without_timestamp"] == 2
    assert out["pairs"][0]["right_events_within_tolerance"] == 1
    assert any("counted once" in w for w in out["warnings"]) and any("no timestamp" in w for w in out["warnings"])


def test_rows_none_of_which_has_a_timestamp_are_not_determinable():
    out = run(executed([hit("2026-11-10T10:00:00Z")]), executed([{"fields": {}}, {"fields": {}}]))
    assert out["outcome"] == "not_determinable" and "usable timestamp" in out["reason"]


def test_pairs_are_capped_and_the_cap_is_reported():
    left = executed([hit(f"2026-11-10T10:{m:02d}:00Z") for m in range(0, 30)])
    right = executed([hit("2026-11-10T10:15:00Z")])
    out = correlation.run({**SPEC, "tolerance": "1h"}, [left, right], max_pairs=5)
    assert len(out["pairs"]) == 5 and out["summary"]["pairs_found"] == 30 and any("only the first 5" in w for w in out["warnings"])


# ---- the whole chain: Generator (mock) -> canonicalise -> validate -> executor (fake cluster) -> correlation


def cluster_handler(controller_rows, fabric_rows, fail=None):
    def handler(index, dsl):
        if index == INDEX["fabric_health"]:
            return error(500, "fabric boom") if fail == "fabric_health" else hits_body(fabric_rows, index="slingshot_crayfabrichealth-2026.11.10")
        return error(500, "controller boom") if fail == "controller_log" else hits_body(controller_rows)
    return handler


Q = "Did link-down events occur near fabric availability problems within 5 minutes?"


def test_cl07_end_to_end_with_matching_timestamps(skill_index, settings, monkeypatch):
    handler = cluster_handler([{"@timestamp": "2026-11-10T10:00:00Z", "Body": "LinkStatus Down", "Severity": "error"}],
                              [{"@timestamp": "2026-11-10T10:02:00Z", "fields": {"Switch": "x1000c4r5b0", "PhysicalSubContext": "DEGRADED"}}])
    result, out, cluster = ask(skill_index, settings, monkeypatch, Q, [CTL, FAB], multi(), handler)
    assert sorted(i for i, _ in cluster.searches) == sorted([INDEX["controller_log"], INDEX["fabric_health"]])
    block = out["correlation"]["execution"]
    assert block["outcome"] == "correlated" and block["pairs"][0]["delta_seconds"] == 120
    assert [r["execution"]["execution_status"] for r in out["results"]] == ["success", "success"]


def test_cl07_end_to_end_without_matching_timestamps(skill_index, settings, monkeypatch):
    handler = cluster_handler([{"@timestamp": "2026-11-10T10:00:00Z", "Body": "LinkStatus Down"}],
                              [{"@timestamp": "2026-11-10T18:00:00Z", "fields": {"Switch": "s", "PhysicalSubContext": "DEGRADED"}}])
    _, out, _ = ask(skill_index, settings, monkeypatch, Q, [CTL, FAB], multi(), handler)
    assert out["correlation"]["execution"]["outcome"] == "none_found"


@pytest.mark.parametrize("fail", ["controller_log", "fabric_health"])
def test_cl07_end_to_end_when_one_query_fails_the_other_result_survives(skill_index, settings, monkeypatch, fail):
    handler = cluster_handler([{"@timestamp": "2026-11-10T10:00:00Z", "Body": "LinkStatus Down"}],
                              [{"@timestamp": "2026-11-10T10:01:00Z", "fields": {"Switch": "s", "PhysicalSubContext": "DEGRADED"}}], fail)
    _, out, _ = ask(skill_index, settings, monkeypatch, Q, [CTL, FAB], multi(), handler)
    statuses = [r["execution"]["execution_status"] for r in out["results"]]
    assert statuses.count("success") == 1 and statuses.count("success") != 2
    assert out["correlation"]["execution"]["outcome"] == "not_determinable"


def test_cl07_without_a_tolerance_the_user_is_asked(skill_index, settings, monkeypatch):
    result, out, cluster = ask(skill_index, settings, monkeypatch, "Did link-down events occur near fabric availability problems?",
                               [CTL, FAB], multi(tolerance=None))
    assert result["status"] == "declined" and result["reason"] == "parameter_requires_clarification"
    assert cluster.searches == []                       # nothing ran before the user answered


def test_each_correlation_entry_is_guarded_against_its_own_pattern(os_ctx):
    """One family's fields cannot ride along in the other family's query."""
    c = canon(multi(), os_ctx)
    c["results"][1]["query"]["query"]["bool"]["filter"].append({"term": {"Resource.host.name.keyword": "h"}})
    v = validate(c, os_ctx)
    assert not v.passed and "results[1]" in v.reason


def test_the_executor_isolates_a_bad_correlation_block(settings):
    out = executor._run_correlation({"type": "temporal_proximity", "left": 0, "right": 1, "tolerance": "5m"},
                                    [{"status": "ok"}, {"status": "ok"}], settings)
    assert out["execution"]["outcome"] == "not_determinable" and out["execution"]["pairs"] == []


# ---- the previously malformed multi result: pin the pipeline's handling of it


def test_unresolved_topics_keep_the_correlation_and_its_indices():
    from app import pipeline
    merged = pipeline._merge_unresolved_topics(multi(), [{"description": "something else", "reason": ""}])
    assert merged["correlation"] == multi()["correlation"] and len(merged["results"]) == 3
    assert merged["results"][0]["pattern_id"] == "controller_log"


# =============================================================================== secondary findings


def test_registry_vocabulary_matches_what_the_live_data_showed(os_registry):
    fh = os_registry.get("fabric_health").severity_semantics.fields[0]
    assert {"CRITICAL", "DEGRADED"} <= fh.abnormal and fh.normal == frozenset({"HEALTHY"})
    ilo = os_registry.get("ilo_events").severity_semantics
    assert "Warning" in ilo.field("Severity").abnormal and "Caution" in ilo.field("Oem.Hpe.Severity").abnormal


def test_a_switch_named_without_its_asic_suffix_is_matched_by_prefix_not_wildcard(os_ctx):
    ok = check(os_ctx("fabric_health"), "fabric_health", rows_query(ABN, {"prefix": {"fields.Switch": "x1000c3r1"}}))
    assert ok.ok, ok.reason
    bad = check(os_ctx("fabric_health"), "fabric_health", rows_query(ABN, {"wildcard": {"fields.Switch": "x1000c3r1*"}}))
    assert not bad.ok


def test_root_login_logout_uses_the_event_code_filter_plus_the_user_in_the_message(os_ctx):
    q = rows_query({"terms": {"Oem.Hpe.Code": [1017, 1018]}}, {"match_phrase": {"Message": "root"}})
    assert check(os_ctx("ilo_events"), "ilo_events", q).ok


def test_security_events_use_the_structured_category_field(os_ctx):
    r = check(os_ctx("ilo_events"), "ilo_events", rows_query({"term": {"Oem.Hpe.Categories": "Security"}}))
    assert r.ok and {"term": {"Oem.Hpe.Categories.keyword": "Security"}} in r.query["query"]["bool"]["filter"]


def test_references_state_the_default_log_window_and_the_corrected_guidance():
    overview = (REFS / "overview.md").read_text(encoding="utf-8")
    assert "last 24 hours (`now-24h` to `now`)" in overview and "never\n  `unsupported_metric`" in overview
    fabric = (REFS / "fabric-health.md").read_text(encoding="utf-8")
    assert "DEGRADED" in fabric and "`prefix`" in fabric and "NOT in `entity_location`" in fabric
    ctl = (REFS / "controller-log.md").read_text(encoding="utf-8")
    assert "identical-message recurrence is **not supported**" in ctl and "`tolerance` is the maximum time difference" in ctl
