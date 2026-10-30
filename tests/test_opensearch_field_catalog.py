"""Tests for app/opensearch_catalog/field_catalog.py (flattening, capability
derivation, merging, cache failure behaviour, registry x catalog view).

Mappings come from tests/fixtures/opensearch/*.json, which are SYNTHETIC
(see that directory's README) -- these tests verify the code paths, not the
live cluster's actual mappings."""
import json
from pathlib import Path

import pytest

from app.opensearch_catalog.field_catalog import (
    OP_EXISTS, OP_GROUP_BY, OP_MATCH, OP_METRIC, OP_RANGE, OP_SORT, OP_TERM,
    REASON_FETCH_FAILED, REASON_NO_MATCHING_INDICES, REASON_REFRESH_FAILED,
    STATUS_FRESH, STATUS_STALE, STATUS_UNAVAILABLE,
    FieldCatalogCache, build_field_catalog, build_field_view, flatten_index_mapping,
    make_mapping_fetcher,
)
from app.opensearch_catalog.registry import DEFAULT_REGISTRY_PATH, load_registry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "opensearch"
SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(scope="module")
def registry():
    return load_registry(DEFAULT_REGISTRY_PATH, skills_root=SKILLS_ROOT)


def _tkw(ignore=256):
    return {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": ignore}}}


def _one(props, name="idx-2026.10.28"):
    return {name: {"mappings": {"properties": props}}}


# ---- flattening ---------------------------------------------------------------------


def test_flatten_walks_nested_objects_and_multifields():
    flat = flatten_index_mapping(_one({
        "Resource": {"properties": {"host": {"properties": {"name": _tkw()}}}},
        "n": {"type": "long"},
    })["idx-2026.10.28"])
    assert set(flat) == {"Resource.host.name", "Resource.host.name.keyword", "n"}
    assert flat["Resource.host.name.keyword"].parent == "Resource.host.name"
    assert flat["Resource.host.name.keyword"].ignore_above == 256
    assert "Resource" not in flat and "Resource.host" not in flat  # objects are not leaves


@pytest.mark.parametrize("body", [{}, {"mappings": {}}, {"mappings": {"properties": {}}}, None, "x"])
def test_flatten_tolerates_empty_or_odd_bodies(body):
    assert flatten_index_mapping(body) == {}


def test_flatten_marks_fields_under_nested_type():
    flat = flatten_index_mapping({"mappings": {"properties": {
        "items": {"type": "nested", "properties": {"sku": {"type": "keyword"}}}}}})
    assert flat["items.sku"].nested is True


# ---- capability derivation ------------------------------------------------------------


def _cat(props):
    return build_field_catalog("p", _one(props))


def test_keyword_field_capabilities():
    f = _cat({"k": {"type": "keyword"}}).get("k")
    assert set(f.ops) == {OP_EXISTS, OP_TERM, OP_GROUP_BY, OP_SORT}
    assert f.ops[OP_TERM] == "k" and f.keyword_subfield is None


def test_text_with_keyword_subfield_uses_the_subfield_for_exact_operations():
    cat = _cat({"t": _tkw()})
    f = cat.get("t")
    assert f.keyword_subfield == "t.keyword"
    assert f.ops[OP_MATCH] == "t"
    assert f.ops[OP_TERM] == f.ops[OP_GROUP_BY] == f.ops[OP_SORT] == "t.keyword"
    assert cat.field_for("t", OP_TERM) == "t.keyword"
    assert cat.get("t.keyword").parent == "t"


def test_text_without_keyword_subfield_cannot_be_used_for_exact_operations():
    cat = _cat({"t": {"type": "text"}})
    f = cat.get("t")
    assert f.keyword_subfield is None
    assert set(f.ops) == {OP_EXISTS, OP_MATCH}
    assert not cat.supports("t", OP_TERM) and not cat.supports("t", OP_GROUP_BY)


def test_keyword_subfield_name_is_not_assumed():
    """Only a sub-field the mapping defines counts; a differently named exact
    sub-field is found, and a text field with a sub-field that is NOT a keyword
    gets no exact capability."""
    cat = _cat({
        "a": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
        "b": {"type": "text", "fields": {"sort": {"type": "long"}}},
    })
    assert cat.get("a").keyword_subfield == "a.raw"
    assert cat.field_for("a", OP_GROUP_BY) == "a.raw"
    assert cat.get("b").keyword_subfield is None
    assert not cat.supports("b", OP_TERM)


def test_ignore_above_is_recorded_for_the_keyword_subfield():
    f = _cat({"t": _tkw(ignore=128)}).get("t")
    assert f.keyword_ignore_above == 128


def test_date_numeric_boolean_and_other_types():
    cat = _cat({
        "d": {"type": "date"}, "n": {"type": "long"}, "b": {"type": "boolean"},
        "g": {"type": "geo_point"}, "ip": {"type": "ip"},
    })
    assert {OP_RANGE, OP_SORT, OP_GROUP_BY} <= set(cat.get("d").ops) and OP_TERM not in cat.get("d").ops
    assert {OP_TERM, OP_RANGE, OP_METRIC} <= set(cat.get("n").ops)
    assert set(cat.get("b").ops) == {OP_EXISTS, OP_TERM, OP_GROUP_BY, OP_SORT}
    assert cat.get("g").ops == {}                       # unknown type -> nothing allowed
    assert OP_RANGE in cat.get("ip").ops


def test_time_field_confirmation():
    cat = _cat({"@timestamp": {"type": "date"}, "ts": {"type": "keyword"}})
    assert cat.time_field_confirmed("@timestamp")
    assert not cat.time_field_confirmed("ts")           # present but not a date
    assert not cat.time_field_confirmed("missing")


# ---- merging daily indices ----------------------------------------------------------------


def test_fields_present_in_only_some_indices_are_kept_and_flagged():
    cat = build_field_catalog("fabric_health", _load("mapping_fabric_health.json"))
    assert cat.mapping_variation == "additive"
    assert cat.get("fields.Port").present_in == "some"
    assert cat.get("fields.Switch").present_in == "all"
    assert OP_TERM in cat.get("fields.Port").ops        # usable, but known to be partial
    assert cat.concrete_indices_seen == ("slingshot_crayfabrichealth-2026.10.28",
                                         "slingshot_crayfabrichealth-2026.10.27")


def test_keyword_subfield_missing_from_one_index_is_not_confirmed():
    mapping = {
        "i-2026.10.28": {"mappings": {"properties": {"t": _tkw()}}},
        "i-2026.10.27": {"mappings": {"properties": {"t": {"type": "text"}}}},
    }
    cat = build_field_catalog("p", mapping)
    assert cat.get("t").keyword_subfield is None
    assert cat.get("t.keyword") is None
    assert not cat.supports("t", OP_TERM)


def test_type_family_conflict_between_indices_disables_operations():
    mapping = {
        "i-2026.10.28": {"mappings": {"properties": {"c": {"type": "keyword"}}}},
        "i-2026.10.27": {"mappings": {"properties": {"c": {"type": "long"}}}},
    }
    cat = build_field_catalog("p", mapping)
    assert cat.mapping_variation == "type_conflict"
    assert cat.get("c").conflict is True and cat.get("c").ops == {}


def test_same_family_different_type_is_not_a_conflict():
    mapping = {
        "i-2026.10.28": {"mappings": {"properties": {"c": {"type": "long"}}}},
        "i-2026.10.27": {"mappings": {"properties": {"c": {"type": "integer"}}}},
    }
    assert build_field_catalog("p", mapping).mapping_variation == "none"


def test_only_the_newest_n_indices_are_merged():
    mapping = {f"i-2026.10.{d:02d}": {"mappings": {"properties": {f"f{d}": {"type": "keyword"}}}}
               for d in range(1, 11)}
    cat = build_field_catalog("p", mapping, max_indices=3)
    assert cat.concrete_indices_seen == ("i-2026.10.10", "i-2026.10.09", "i-2026.10.08")
    assert set(cat.fields) == {"f10", "f9", "f8"}


def test_system_indices_are_ignored():
    cat = build_field_catalog("p", {".kibana_1": {"mappings": {"properties": {"x": {"type": "keyword"}}}},
                                    "i-1": {"mappings": {"properties": {"y": {"type": "keyword"}}}}})
    assert set(cat.fields) == {"y"}


def test_nested_type_fields_get_no_operations():
    cat = _cat({"items": {"type": "nested", "properties": {"sku": {"type": "keyword"}}}})
    assert cat.get("items.sku").nested is True and cat.get("items.sku").ops == {}


@pytest.mark.parametrize("mapping", [{}, {".only_system": {"mappings": {"properties": {"x": {"type": "keyword"}}}}},
                                     {"i-1": {"mappings": {"properties": {}}}}, {"i-1": {}}])
def test_no_usable_indices_or_fields_is_unavailable_not_empty_success(mapping):
    cat = build_field_catalog("p", mapping)
    assert cat.discovery_status == STATUS_UNAVAILABLE
    assert cat.reason == REASON_NO_MATCHING_INDICES
    assert cat.usable is False and cat.fields == {}


# ---- the three representative families ----------------------------------------------------------


def test_fabric_health_fixture_matches_documented_shape():
    cat = build_field_catalog("fabric_health", _load("mapping_fabric_health.json"))
    assert cat.time_field_confirmed("@timestamp")
    assert cat.field_for("fields.PhysicalSubContext", OP_TERM) == "fields.PhysicalSubContext.keyword"
    assert cat.field_for("fields.Switch", OP_GROUP_BY) == "fields.Switch.keyword"
    assert cat.get("fields.Value").supports(OP_MATCH)
    assert cat.get("fields.Timestamp").family == "numeric"   # display-time stays a number, not a date


def test_controller_log_fixture_matches_documented_shape():
    cat = build_field_catalog("controller_log", _load("mapping_controller_log.json"))
    assert cat.field_for("Resource.host.name", OP_GROUP_BY) == "Resource.host.name.keyword"
    assert cat.field_for("Severity", OP_TERM) == "Severity.keyword"
    # A text field with no keyword sub-field: searchable only.
    assert cat.get("Attributes.PortName").ops.keys() == {OP_EXISTS, OP_MATCH}
    assert cat.get("Attributes.PortName").present_in == "some"


def test_ilo_fixture_matches_documented_shape():
    cat = build_field_catalog("ilo_events", _load("mapping_ilo_events.json"))
    assert cat.get("Oem.Hpe.Repaired").family == "boolean" and cat.supports("Oem.Hpe.Repaired", OP_TERM)
    assert cat.get("Oem.Hpe.Code").family == "numeric"
    assert cat.field_for("Oem.Hpe.Severity", OP_TERM) == "Oem.Hpe.Severity.keyword"
    assert cat.get("Created").family == "date"


# ---- cache ----------------------------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class _Fetcher:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def __call__(self, index_pattern):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _cache(fetcher, clock, **kw):
    kw.setdefault("ttl_seconds", 600)
    kw.setdefault("stale_ttl_seconds", 3600)
    kw.setdefault("failure_backoff_seconds", 30)
    return FieldCatalogCache(fetcher, clock=clock, **kw)


@pytest.fixture()
def entry(registry):
    return registry.get("controller_log")


def test_cache_serves_fresh_results_without_refetching(entry):
    clock, fetcher = _Clock(), _Fetcher(_load("mapping_controller_log.json"))
    cache = _cache(fetcher, clock)
    first = cache.get(entry)
    clock.now += 599
    second = cache.get(entry)
    assert first.discovery_status == STATUS_FRESH and second is first
    assert fetcher.calls == 1


def test_cache_refetches_after_ttl(entry):
    clock, fetcher = _Clock(), _Fetcher(_load("mapping_controller_log.json"))
    cache = _cache(fetcher, clock)
    cache.get(entry)
    clock.now += 601
    cache.get(entry)
    assert fetcher.calls == 2


def test_failed_refresh_serves_stale_then_expires_to_unavailable(entry):
    clock, fetcher = _Clock(), _Fetcher(_load("mapping_controller_log.json"))
    cache = _cache(fetcher, clock)
    good = cache.get(entry)
    fetcher.result = None                       # OpenSearch goes down
    clock.now += 601
    stale = cache.get(entry)
    assert stale.discovery_status == STATUS_STALE and stale.reason == REASON_REFRESH_FAILED
    assert stale.usable and stale.fields == good.fields
    clock.now += 3600                           # past the stale window (and the backoff)
    gone = cache.get(entry)
    assert gone.discovery_status == STATUS_UNAVAILABLE and gone.reason == REASON_FETCH_FAILED
    assert gone.usable is False and gone.fields == {}


def test_failure_with_nothing_cached_is_unavailable_not_empty_success(entry):
    cache = _cache(_Fetcher(None), _Clock())
    cat = cache.get(entry)
    assert cat.discovery_status == STATUS_UNAVAILABLE and cat.reason == REASON_FETCH_FAILED


def test_failures_are_not_retried_inside_the_backoff_window(entry):
    clock, fetcher = _Clock(), _Fetcher(None)
    cache = _cache(fetcher, clock)
    cache.get(entry)
    clock.now += 10
    cache.get(entry)
    assert fetcher.calls == 1
    clock.now += 21                              # 31s after the failure
    cache.get(entry)
    assert fetcher.calls == 2


def test_recovery_after_an_outage(entry):
    clock, fetcher = _Clock(), _Fetcher(None)
    cache = _cache(fetcher, clock)
    assert cache.get(entry).discovery_status == STATUS_UNAVAILABLE
    fetcher.result = _load("mapping_controller_log.json")
    clock.now += 31
    assert cache.get(entry).discovery_status == STATUS_FRESH


def test_a_fetcher_that_raises_is_treated_as_a_failed_fetch(entry):
    cat = _cache(_Fetcher(RuntimeError("boom")), _Clock()).get(entry)
    assert cat.discovery_status == STATUS_UNAVAILABLE and cat.reason == REASON_FETCH_FAILED


def test_a_non_object_response_is_treated_as_a_failed_fetch(entry):
    assert _cache(_Fetcher(["not", "a", "dict"]), _Clock()).get(entry).discovery_status == STATUS_UNAVAILABLE


def test_no_matching_indices_is_distinguished_from_a_failed_fetch(entry):
    cat = _cache(_Fetcher({}), _Clock()).get(entry)
    assert cat.discovery_status == STATUS_UNAVAILABLE and cat.reason == REASON_NO_MATCHING_INDICES


def test_a_non_failure_empty_result_does_not_overwrite_a_good_cache(entry):
    clock, fetcher = _Clock(), _Fetcher(_load("mapping_controller_log.json"))
    cache = _cache(fetcher, clock)
    cache.get(entry)
    fetcher.result = {}
    clock.now += 601
    cat = cache.get(entry)
    assert cat.discovery_status == STATUS_STALE and cat.usable


def test_invalidate_clears_cache_and_backoff(entry):
    clock, fetcher = _Clock(), _Fetcher(None)
    cache = _cache(fetcher, clock)
    cache.get(entry)
    fetcher.result = _load("mapping_controller_log.json")
    cache.invalidate()
    assert cache.get(entry).discovery_status == STATUS_FRESH


def test_patterns_are_cached_independently(registry):
    clock = _Clock()
    seen = []

    def fetch(pattern):
        seen.append(pattern)
        return _load({"controllerlog-*": "mapping_controller_log.json",
                      "log_iml-*": "mapping_ilo_events.json"}[pattern])

    cache = _cache(fetch, clock)
    cache.get(registry.get("controller_log"))
    cache.get(registry.get("ilo_events"))
    cache.get(registry.get("controller_log"))
    assert seen == ["controllerlog-*", "log_iml-*"]


def test_cache_rejects_inconsistent_ttls():
    with pytest.raises(ValueError):
        FieldCatalogCache(lambda p: {}, ttl_seconds=600, stale_ttl_seconds=60)


def test_fetcher_uses_the_opensearch_client(monkeypatch):
    seen = {}

    def fake(base_url, pattern, timeout, auth):
        seen.update(base_url=base_url, pattern=pattern, timeout=timeout, auth=auth)
        return {"ok": True}

    monkeypatch.setattr("app.opensearch_client.get_field_mapping", fake)
    assert make_mapping_fetcher("http://os:9200", auth=("u", "p"), timeout=3)("controllerlog-*") == {"ok": True}
    assert seen == {"base_url": "http://os:9200", "pattern": "controllerlog-*", "timeout": 3, "auth": ("u", "p")}


# ---- registry x catalog view ------------------------------------------------------------------------------


def test_view_allows_only_registry_permitted_fields(registry):
    """A field that exists live but is not registry-queryable is not usable:
    discovery widens knowledge, never permission."""
    entry = registry.get("controller_log")
    live = _load("mapping_controller_log.json")
    live["controllerlog-2026.10.28"]["mappings"]["properties"]["Resource"]["properties"]["process_id"] = _tkw()
    view = build_field_view(entry, build_field_catalog("controller_log", live))
    assert "Resource.process_id" not in view.allowed_fields
    assert "Resource.host.name" in view.allowed_fields
    assert view.usable


def test_view_resolves_effective_group_by_paths_and_excludes_text(registry):
    entry = registry.get("controller_log")
    view = build_field_view(entry, build_field_catalog("controller_log", _load("mapping_controller_log.json")))
    assert view.group_by_fields["Resource.host.name"] == "Resource.host.name.keyword"
    assert view.group_by_fields["Severity"] == "Severity.keyword"
    assert "Body" not in view.group_by_fields


def test_view_drops_registry_bindings_missing_from_the_live_mapping_with_a_warning(registry):
    entry = registry.get("controller_log")
    live = _load("mapping_controller_log.json")
    del live["controllerlog-2026.10.28"]["mappings"]["properties"]["Resource"]["properties"]["service"]
    del live["controllerlog-2026.10.27"]["mappings"]["properties"]["Resource"]["properties"]["service"]
    view = build_field_view(entry, build_field_catalog("controller_log", live))
    assert "Resource.service.name" in view.missing_fields
    assert "service" not in view.roles_confirmed
    assert ("service", "Resource.service.name") in view.dropped_role_bindings
    assert any("Resource.service.name" in w for w in view.warnings)
    assert view.usable                                  # still usable for what remains


def test_view_partially_confirms_a_multi_field_role(registry):
    entry = registry.get("ilo_events")
    live = _load("mapping_ilo_events.json")
    del live["log_iml-2026.10.26"]["mappings"]["properties"]["Oem"]
    view = build_field_view(entry, build_field_catalog("ilo_events", live))
    assert view.roles_confirmed["severity"] == ("Severity",)      # Oem.Hpe.Severity dropped
    assert ("severity", "Oem.Hpe.Severity") in view.dropped_role_bindings


def test_view_is_not_usable_when_the_time_field_is_not_a_date(registry):
    entry = registry.get("controller_log")
    live = _load("mapping_controller_log.json")
    for body in live.values():
        body["mappings"]["properties"]["@timestamp"] = {"type": "keyword"}
    view = build_field_view(entry, build_field_catalog("controller_log", live))
    assert view.time_field_confirmed is False and view.usable is False


def test_view_fails_closed_when_discovery_is_unavailable(registry, entry):
    cat = _cache(_Fetcher(None), _Clock()).get(entry)
    view = build_field_view(entry, cat)
    assert view.usable is False and view.discovery_status == STATUS_UNAVAILABLE
    assert view.allowed_fields == {} and view.group_by_fields == {}


def test_view_carries_stale_status_and_a_warning(registry, entry):
    clock, fetcher = _Clock(), _Fetcher(_load("mapping_controller_log.json"))
    cache = _cache(fetcher, clock)
    cache.get(entry)
    fetcher.result = None
    clock.now += 601
    view = build_field_view(entry, cache.get(entry))
    assert view.discovery_status == STATUS_STALE and view.usable
    assert any("stale" in w for w in view.warnings)


@pytest.mark.parametrize("pattern_id,fixture", [("fabric_health", "mapping_fabric_health.json"),
                                                ("controller_log", "mapping_controller_log.json"),
                                                ("ilo_events", "mapping_ilo_events.json")])
def test_every_shipped_registry_field_exists_in_the_matching_fixture(registry, pattern_id, fixture):
    """Registry <-> documented-shape consistency: nothing the registry permits
    is missing from the representative mapping (display-only time fields
    excluded, since they are not queryable)."""
    entry = registry.get(pattern_id)
    view = build_field_view(entry, build_field_catalog(pattern_id, _load(fixture)))
    assert view.missing_fields == ()
    assert view.dropped_role_bindings == ()
    assert view.usable
    for path in entry.groupable_fields:
        assert path in view.group_by_fields, path
