"""Tests for app/opensearch_catalog/registry.py and the shipped
index_registry.json."""
import copy
import json
from pathlib import Path

import pytest

from app.opensearch_catalog.registry import (
    DEFAULT_REGISTRY_PATH,
    KNOWN_CAVEAT_CODES,
    RegistryError,
    load_registry,
    parse_duration_seconds,
)

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


@pytest.fixture(scope="module")
def registry():
    return load_registry(DEFAULT_REGISTRY_PATH, skills_root=SKILLS_ROOT)


# ---- the shipped registry ---------------------------------------------------------


def test_shipped_registry_represents_the_three_families(registry):
    got = {p.pattern_id: p.index_pattern for p in registry.patterns}
    assert got == {
        "fabric_health": "slingshot_crayfabrichealth-*",
        "controller_log": "controllerlog-*",
        "ilo_events": "log_iml-*",
    }
    assert all(p.enabled for p in registry.patterns)


def test_every_family_filters_on_ingest_time_and_keeps_event_time_display_only(registry):
    for p in registry.patterns:
        assert p.time_field == "@timestamp"
        assert p.display_time_fields, p.pattern_id
        assert not set(p.display_time_fields) & p.queryable_fields


def test_text_fields_are_never_groupable(registry):
    assert "fields.Value" in registry.get("fabric_health").never_group
    assert "Body" in registry.get("controller_log").never_group
    assert "Message" in registry.get("ilo_events").never_group


def test_ilo_binds_both_severity_fields_to_one_role(registry):
    assert registry.get("ilo_events").role_paths("severity") == ("Severity", "Oem.Hpe.Severity")


def test_domain_references_are_routed_to_opensearch_in_skill_md(registry, skill_index):
    for p in registry.patterns:
        row = skill_index.row_for_path(p.domain_reference)
        assert row is not None, p.domain_reference
        assert row.data_sources == ("opensearch",)


def test_max_window_is_a_parseable_duration(registry):
    for p in registry.patterns:
        assert p.max_window_seconds == parse_duration_seconds("7d") == 7 * 86400


def test_shipped_registry_contains_no_sensitive_looking_values():
    text = DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8")
    assert "password" not in text.lower() and "token" not in text.lower()


# ---- lookups ---------------------------------------------------------------------


def test_patterns_for_reference(registry):
    got = registry.patterns_for_reference("references/opensearch-logs/controller-log.md")
    assert [p.pattern_id for p in got] == ["controller_log"]
    assert registry.patterns_for_reference("references/node-exporter/cpu.md") == ()
    assert registry.patterns_for_reference("references/opensearch-logs/overview.md") == ()


def test_index_expression_maps_ids_to_registered_patterns(registry):
    assert registry.index_expression(["fabric_health"]) == "slingshot_crayfabrichealth-*"
    assert registry.index_expression(["controller_log", "ilo_events"]) == "controllerlog-*,log_iml-*"
    assert registry.index_expression(["ilo_events", "ilo_events"]) == "log_iml-*"


@pytest.mark.parametrize("bad", [[], ["nope"], ["fabric_health", "nope"]])
def test_index_expression_rejects_unknown_ids(registry, bad):
    with pytest.raises(KeyError):
        registry.index_expression(bad)


@pytest.mark.parametrize("index", [
    "slingshot_crayfabrichealth-*",
    "controllerlog-*",
    "controllerlog-*,log_iml-*",
    "log_iml-*,slingshot_crayfabrichealth-*,controllerlog-*",
])
def test_allow_list_accepts_exact_registered_patterns(registry, index):
    assert registry.is_allowed_index(index)


@pytest.mark.parametrize("index", [
    "*", "_all", "", " ", ",", ".kibana", ".opendistro-ism-config",
    "controllerlog-2026.10.*",            # narrower wildcard
    "controllerlog*",                      # wider wildcard
    "controllerlog-",                      # truncated
    "CONTROLLERLOG-*",                     # case differs
    "controllerlog-* ",                    # whitespace
    " controllerlog-*",
    "controllerlog-*,",                    # empty part
    ",controllerlog-*",
    "controllerlog-*,*",                   # smuggled wildcard
    "controllerlog-*,controllerlog-*",     # duplicate
    "remote:controllerlog-*",              # cross-cluster
    "controllerlog-*,remote:log_iml-*",
    "-controllerlog-*",                    # exclusion syntax
    "<controllerlog-{now/d}>",             # date-math index name
    "other-*",
])
def test_allow_list_rejects_everything_else(registry, index):
    assert registry.resolve_index(index) is None
    assert registry.is_allowed_index(index) is False


def test_allow_list_rejects_non_strings(registry):
    assert registry.resolve_index(None) is None
    assert registry.resolve_index(["controllerlog-*"]) is None


def test_disabled_pattern_is_not_queryable(tmp_path):
    data = json.loads(DEFAULT_REGISTRY_PATH.read_text())
    data["patterns"][1]["enabled"] = False
    p = tmp_path / "r.json"
    p.write_text(json.dumps(data))
    reg = load_registry(p, skills_root=SKILLS_ROOT)
    assert [e.pattern_id for e in reg.enabled_patterns] == ["fabric_health", "ilo_events"]
    assert not reg.is_allowed_index("controllerlog-*")
    assert reg.patterns_for_reference("references/opensearch-logs/controller-log.md") == ()
    with pytest.raises(KeyError):
        reg.index_expression(["controller_log"])


# ---- validation: every violation fails loudly --------------------------------------


def _base():
    return json.loads(DEFAULT_REGISTRY_PATH.read_text())


def _write(tmp_path, data):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return p


def _mutate(fn):
    data = _base()
    fn(data)
    return data


INVALID = {
    "bad_version": lambda d: d.update(registry_version="2"),
    "no_patterns": lambda d: d.update(patterns=[]),
    "duplicate_pattern_id": lambda d: d["patterns"][1].update(pattern_id="fabric_health"),
    "duplicate_index_pattern": lambda d: d["patterns"][1].update(index_pattern="slingshot_crayfabrichealth-*"),
    "duplicate_domain_reference": lambda d: d["patterns"][1].update(domain_reference=d["patterns"][0]["domain_reference"]),
    "bad_pattern_id": lambda d: d["patterns"][0].update(pattern_id="Fabric-Health"),
    "bare_star": lambda d: d["patterns"][0].update(index_pattern="*"),
    "underscore_all": lambda d: d["patterns"][0].update(index_pattern="_all"),
    "system_index": lambda d: d["patterns"][0].update(index_pattern=".kibana-*"),
    "comma_list": lambda d: d["patterns"][0].update(index_pattern="a-*,b-*"),
    "cluster_prefix": lambda d: d["patterns"][0].update(index_pattern="remote:idx-*"),
    "inner_wildcard": lambda d: d["patterns"][0].update(index_pattern="a*b-*"),
    "missing_reference_file": lambda d: d["patterns"][0].update(domain_reference="references/opensearch-logs/nope.md"),
    "enabled_not_bool": lambda d: d["patterns"][0].update(enabled="yes"),
    "bad_max_window_text": lambda d: d["patterns"][0].update(max_window="7 days"),
    "bad_max_window_zero": lambda d: d["patterns"][0].update(max_window="0d"),
    "time_field_not_queryable": lambda d: d["patterns"][0].update(time_field="fields.Timestamp"),
    "time_field_is_display_only": lambda d: d["patterns"][0]["display_time_fields"].append("@timestamp"),
    "display_time_is_queryable": lambda d: d["patterns"][0]["queryable_fields"].append("timestamp"),
    "groupable_not_queryable": lambda d: d["patterns"][0]["groupable_fields"].append("fields.Asic"),
    "groupable_and_never_group": lambda d: d["patterns"][0]["never_group"].append("fields.Switch"),
    "projection_outside_allowed": lambda d: d["patterns"][0]["output_projection"].append("tags"),
    "role_binds_unqueryable_field": lambda d: d["patterns"][0]["roles"].update(extra=["fields.Asic"]),
    "role_empty_paths": lambda d: d["patterns"][0]["roles"].update(extra=[]),
    "role_bad_name": lambda d: d["patterns"][0]["roles"].update({"Bad Role": ["fields.Switch"]}),
    "no_roles": lambda d: d["patterns"][0].update(roles={}),
    "unknown_caveat": lambda d: d["patterns"][0]["known_caveats"].append("made_up"),
    "duplicate_in_list": lambda d: d["patterns"][0]["queryable_fields"].append("fields.Switch"),
    "missing_required_key": lambda d: d["patterns"][0].pop("queryable_fields"),
    "sensitivity_not_object": lambda d: d["patterns"][0].update(sensitivity="internal"),
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_invalid_registry_is_rejected(name, tmp_path):
    path = _write(tmp_path, _mutate(INVALID[name]))
    with pytest.raises(RegistryError):
        load_registry(path, skills_root=SKILLS_ROOT)


def test_missing_reference_is_only_checked_when_skills_root_is_given(tmp_path):
    data = _mutate(INVALID["missing_reference_file"])
    load_registry(_write(tmp_path, data))  # no skills_root -> existence not checked


def test_unreadable_and_non_json_files_are_registry_errors(tmp_path):
    with pytest.raises(RegistryError):
        load_registry(tmp_path / "missing.json")
    with pytest.raises(RegistryError):
        load_registry(_write(tmp_path, "{not json"))
    with pytest.raises(RegistryError):
        load_registry(_write(tmp_path, "[]"))


def test_all_shipped_caveats_are_in_the_fixed_vocabulary(registry):
    for p in registry.patterns:
        assert set(p.known_caveats) <= KNOWN_CAVEAT_CODES


@pytest.mark.parametrize("value,seconds", [("30m", 1800), ("24h", 86400), ("7d", 604800), ("2w", 1209600)])
def test_parse_duration_seconds(value, seconds):
    assert parse_duration_seconds(value) == seconds


@pytest.mark.parametrize("value", ["", "7", "d", "7D", "1.5h", "-1d", "0h", "7 d"])
def test_parse_duration_seconds_rejects(value):
    with pytest.raises(ValueError):
        parse_duration_seconds(value)


# ---- settings ------------------------------------------------------------------------


def test_settings_defaults_for_opensearch(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv("OPENSEARCH_URL", raising=False)
    s = Settings(gemini_api_key="k", _env_file=None)
    assert s.opensearch_url == "http://localhost:9200"
    assert s.opensearch_field_catalog_ttl_seconds == 600
    assert s.opensearch_field_catalog_stale_ttl_seconds >= s.opensearch_field_catalog_ttl_seconds
    assert s.opensearch_field_catalog_max_indices == 7
    assert s.opensearch_registry_path is None   # None = packaged registry
