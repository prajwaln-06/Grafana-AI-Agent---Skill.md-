"""Text -> keyword resolution and guard transformations (checkpoint-2 review, item 3).

The exact-match target of an analysed text field is a keyword-typed sub-field CONFIRMED by the live mapping
(present in every index that has the parent, one consistent type). Nothing named `.keyword` is ever assumed.
Mappings here are small hand-built SYNTHETIC ones, one per scenario."""
import copy
import json

import pytest

from app.opensearch_catalog import canonical, guard
from app.opensearch_catalog.context import Limits, OpenSearchContext, PatternContext
from app.opensearch_catalog.field_catalog import FieldCatalogCache, build_field_view

W = {"range": {"@timestamp": {"gte": "now-24h", "lte": "now"}}}
HOST = "Resource.host.name"


def base_props(host_field):
    """Controller-log-shaped mapping where only Resource.host.name varies."""
    kw = {"type": "keyword", "ignore_above": 256}
    tk = {"type": "text", "fields": {"keyword": kw}}
    return {
        "@timestamp": {"type": "date"}, "Timestamp": {"type": "text"},
        "Severity": tk, "Body": tk,
        "Resource": {"properties": {"host": {"properties": {"name": host_field}}, "service": {"properties": {"name": tk}}}},
        "Attributes": {"properties": {"facility": tk, "PortName": {"type": "text"}, "PortNumber": tk}},
    }


@pytest.fixture()
def ctx_for(os_registry):
    def make(*host_fields):
        """One concrete index per host_field variant (newest first by name)."""
        mapping = {f"controllerlog-2026.10.{28 - i}": {"mappings": {"properties": base_props(hf)}}
                   for i, hf in enumerate(host_fields)}
        cache = FieldCatalogCache(lambda ip: mapping, ttl_seconds=60, stale_ttl_seconds=60, max_indices=50,
                                  failure_backoff_seconds=1)
        entry = os_registry.get("controller_log")
        return OpenSearchContext(patterns={"controller_log": PatternContext(entry=entry, view=build_field_view(entry, cache.get(entry)))},
                                 limits=Limits())
    return make


def run(ctx, filt, **extra):
    q = {"size": 5, "query": {"bool": {"filter": [W, filt]}}}
    return guard.check_entry({"pattern_id": "controller_log", "query": q, **extra}, ctx)


def run_agg(ctx, field):
    q = {"size": 0, "query": {"bool": {"filter": [W]}}, "aggs": {"a": {"terms": {"field": field}}}}
    return guard.check_entry({"pattern_id": "controller_log", "query": q}, ctx)


def text_with(**subfields):
    return {"type": "text", "fields": subfields}


KW = {"type": "keyword", "ignore_above": 256}


# ---- resolution uses only a confirmed sub-field -----------------------------------------------------------------------------


def test_confirmed_keyword_subfield_is_used_and_recorded(ctx_for):
    r = run(ctx_for(text_with(keyword=KW)), {"term": {HOST: "x1c0"}})
    assert r.ok, r.reason
    assert r.query["query"]["bool"]["filter"][1] == {"term": {f"{HOST}.keyword": "x1c0"}}
    assert any(HOST in a and f"{HOST}.keyword" in a for a in r.applied)


def test_no_subfield_means_no_exact_match_and_nothing_is_invented(ctx_for):
    ctx = ctx_for({"type": "text"})
    for filt in ({"term": {HOST: "x"}}, {"terms": {HOST: ["x"]}}, {"prefix": {HOST: "x"}}):
        r = run(ctx, filt)
        assert not r.ok and r.code == guard.CODE_OPERATION_NOT_SUPPORTED, filt
        assert "keyword sub-field" in r.reason
    assert f"{HOST}.keyword" not in ctx.patterns["controller_log"].view.allowed_fields


def test_model_written_keyword_path_that_does_not_exist_is_rejected_not_accepted(ctx_for):
    r = run(ctx_for({"type": "text"}), {"term": {f"{HOST}.keyword": "x"}})
    assert not r.ok and r.code == guard.CODE_UNKNOWN_FIELD


def test_group_by_and_sort_need_a_confirmed_subfield_too(ctx_for):
    ctx = ctx_for({"type": "text"})
    assert not run_agg(ctx, HOST).ok and not run_agg(ctx, f"{HOST}.keyword").ok
    r = guard.check_entry({"pattern_id": "controller_log",
                           "query": {"size": 5, "query": {"bool": {"filter": [W]}}, "sort": [{HOST: "asc"}]}}, ctx)
    assert not r.ok


def test_text_field_still_supports_match_without_a_subfield(ctx_for):
    # match is only allowed on the family's text role (Body); the host field is not that role
    r = run(ctx_for({"type": "text"}), {"match": {HOST: "x"}})
    assert not r.ok and r.code == guard.CODE_OPERATION_NOT_SUPPORTED


def test_differently_named_confirmed_keyword_subfield_is_used_not_dot_keyword(ctx_for):
    ctx = ctx_for(text_with(raw=KW))
    r = run(ctx, {"term": {HOST: "x1c0"}})
    assert r.ok, r.reason
    flat = json.dumps(r.query)
    assert f"{HOST}.raw" in flat and f"{HOST}.keyword" not in flat
    assert not run(ctx, {"term": {f"{HOST}.keyword": "x"}}).ok


def test_subfield_called_keyword_but_not_keyword_typed_is_not_confirmed(ctx_for):
    r = run(ctx_for(text_with(keyword={"type": "text"})), {"term": {HOST: "x"}})
    assert not r.ok and r.code == guard.CODE_OPERATION_NOT_SUPPORTED


def test_subfield_present_in_only_some_indices_is_not_confirmed(ctx_for):
    ctx = ctx_for(text_with(keyword=KW), {"type": "text"})   # newest has it, older does not
    r = run(ctx, {"term": {HOST: "x"}})
    assert not r.ok and r.code == guard.CODE_OPERATION_NOT_SUPPORTED


def test_subfield_with_different_types_across_indices_is_not_confirmed(ctx_for):
    ctx = ctx_for(text_with(keyword=KW), text_with(keyword={"type": "text"}))
    assert not run(ctx, {"term": {HOST: "x"}}).ok


def test_ambiguous_keyword_subfields_are_rejected_not_guessed(ctx_for):
    ctx = ctx_for(text_with(raw=KW, exact=KW))
    r = run(ctx, {"term": {HOST: "x"}})
    assert not r.ok and r.code == guard.CODE_OPERATION_NOT_SUPPORTED
    assert not run(ctx, {"term": {f"{HOST}.raw": "x"}}).ok      # neither candidate is offered to the model


def test_dot_keyword_wins_when_several_keyword_subfields_exist(ctx_for):
    r = run(ctx_for(text_with(keyword=KW, raw=KW)), {"term": {HOST: "x"}})
    assert r.ok and f"{HOST}.keyword" in json.dumps(r.query)


def test_natively_keyword_field_needs_no_substitution_and_no_note(ctx_for):
    r = run(ctx_for(KW), {"term": {HOST: "x1c0"}})
    assert r.ok, r.reason
    assert r.query["query"]["bool"]["filter"][1] == {"term": {HOST: "x1c0"}}
    assert not any("sub-field" in a for a in r.applied)


def test_type_conflict_between_indices_disables_every_operation(ctx_for):
    ctx = ctx_for(text_with(keyword=KW), {"type": "long"})
    assert not run(ctx, {"term": {HOST: "x"}}).ok
    assert not run_agg(ctx, f"{HOST}.keyword").ok


def test_ignore_above_is_not_misrepresented(ctx_for):
    info = ctx_for(text_with(keyword={"type": "keyword", "ignore_above": 64})).patterns["controller_log"].view.allowed_fields[HOST]
    assert info.keyword_subfield == f"{HOST}.keyword" and info.keyword_ignore_above == 64


# ---- unsupported / ambiguous operations ---------------------------------------------------------------------------------


@pytest.mark.parametrize("filt", [
    {"range": {"Severity": {"gte": "a", "lte": "z"}}},          # range on a text field
    {"match": {"Severity": "error"}},                           # match outside the text role
    {"term": {"@timestamp": "2026-10-28"}},                     # term on a date
    {"exists": {"field": "Nope"}},                              # unknown field
    {"term": {"Severity": ["error", "crit"]}},                  # list where a scalar is required
    {"term": {"Severity": {"value": "error", "boost": 2}}},     # extra option
    {"terms": {"Severity": "error"}},                           # scalar where a list is required
    {"term": {"Severity": "error", HOST: "x"}},                 # two fields in one clause
])
def test_unsupported_or_malformed_operations_are_rejected(ctx_for, filt):
    assert not run(ctx_for(text_with(keyword=KW)), filt).ok


# ---- canonical output and guard_notes describe exactly what changed -----------------------------------------------------------


def test_notes_name_the_substitution_and_the_output_matches_them(ctx_for):
    ctx = ctx_for(text_with(keyword=KW))
    entry = {"status": "ok", "data_source": "opensearch", "pattern_id": "controller_log",
             "query": {"size": 5, "query": {"bool": {"filter": [W, {"term": {HOST: "x1c0"}}]}}}}
    before = copy.deepcopy(entry)
    out = canonical.canonicalise_contract(entry, ctx)
    assert entry == before                                            # input untouched
    assert any(f"{HOST}'" in a and f"{HOST}.keyword" in a for a in out["guard_notes"]["applied"])
    assert f"{HOST}.keyword" in json.dumps(out["query"])
    assert out["index"] == "controllerlog-*"


def test_applied_is_empty_exactly_when_the_query_is_unchanged(ctx_for):
    """Every transformation is recorded and nothing is recorded that did not happen."""
    ctx = ctx_for(text_with(keyword=KW))
    explicit = {"size": 5, "timeout": "10s", "sort": [{"@timestamp": "desc"}],
                "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
                "query": {"bool": {"filter": [W, {"term": {f"{HOST}.keyword": "x"}}]}}}
    cases = [
        explicit,
        {**explicit, "query": {"bool": {"filter": [W, {"term": {HOST: "x"}}]}}},
        {"size": 5, "query": {"bool": {"filter": [W]}}},
        {"size": 0, "query": {"bool": {"filter": [W, {"named_condition": "abnormal_severity"}]}},
         "aggs": {"a": {"terms": {"field": f"{HOST}.keyword"}}}},
        {"size": 0, "query": {"bool": {"filter": [W]}}, "aggs": {"a": {"terms": {"field": f"{HOST}.keyword", "size": 7}}},
         "timeout": "10s"},
    ]
    for q in cases:
        r = guard.check_entry({"pattern_id": "controller_log", "query": copy.deepcopy(q)}, ctx)
        assert r.ok, (q, r.reason)
        assert (r.query != q) == bool(r.applied), (q, r.applied)


def test_notes_are_absent_when_nothing_was_transformed(ctx_for):
    ctx = ctx_for(text_with(keyword=KW))
    entry = {"status": "ok", "data_source": "opensearch", "pattern_id": "controller_log",
             "query": {"size": 5, "timeout": "10s", "sort": [{"@timestamp": "desc"}],
                       "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
                       "query": {"bool": {"filter": [W]}}}}
    assert "guard_notes" not in canonical.canonicalise_contract(entry, ctx)


def test_canonical_output_is_a_fixed_point(ctx_for):
    ctx = ctx_for(text_with(keyword=KW))
    q = {"size": 0, "query": {"bool": {"filter": [W, {"term": {HOST: "x"}}, {"named_condition": "abnormal_severity"}]}},
         "aggs": {"a": {"terms": {"field": f"{HOST}.keyword"}}}}
    first = guard.check_entry({"pattern_id": "controller_log", "query": q}, ctx)
    second = guard.check_entry({"pattern_id": "controller_log", "query": copy.deepcopy(first.query)}, ctx)
    assert first.ok and second.ok and second.query == first.query and not second.applied


def test_rejected_entry_is_passed_through_unchanged_for_the_validator(ctx_for):
    ctx = ctx_for({"type": "text"})
    entry = {"status": "ok", "data_source": "opensearch", "pattern_id": "controller_log",
             "query": {"size": 5, "query": {"bool": {"filter": [W, {"term": {HOST: "x"}}]}}}}
    assert canonical.canonicalise_contract(entry, ctx) == entry
