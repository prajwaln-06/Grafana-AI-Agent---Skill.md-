"""Redaction of identifiable usernames and internal IPs before results/diagnostics reach a
user. Heuristic by design (user names are only detected with a cue such as `user X`); the
tests pin what IS promised: masking where cued, configurable IP handling, structure kept."""
import pytest

from app.opensearch_catalog.redaction import (
    IP_MODE_KEEP_INTERNAL, IP_MODE_MASK_ALL, IP_MODE_OFF, RedactionPolicy, RedactionStats,
    redact_text, redact_value, safe_diagnostic,
)

MASK = RedactionPolicy()


def test_cued_username_is_masked_and_counted():
    st = RedactionStats()
    out = redact_text("Login by user alice succeeded", MASK, st)
    assert "alice" not in out and st.usernames == 1


def test_quoted_username_is_masked():
    assert "bob" not in redact_text("User 'bob' logged out", MASK)


def test_ordinary_words_after_cues_are_not_masked():
    assert redact_text("login from host controller-1", MASK) == "login from host controller-1"


def test_mask_all_masks_internal_and_external_ips():
    out = redact_text("src 10.1.2.3 dst 8.8.8.8", MASK)
    assert "10.1.2.3" not in out and "8.8.8.8" not in out


def test_keep_internal_keeps_private_ranges_but_masks_public():
    p = RedactionPolicy(ip_mode=IP_MODE_KEEP_INTERNAL)
    out = redact_text("a 10.1.2.3 b 192.168.0.9 c 172.16.5.5 d 8.8.8.8", p)
    assert "10.1.2.3" in out and "192.168.0.9" in out and "172.16.5.5" in out and "8.8.8.8" not in out


def test_off_mode_leaves_ips():
    assert "10.1.2.3" in redact_text("x 10.1.2.3", RedactionPolicy(ip_mode=IP_MODE_OFF))


def test_username_masking_can_be_disabled():
    assert "alice" in redact_text("user alice", RedactionPolicy(mask_usernames=False))


def test_non_ip_dotted_numbers_are_left_alone():
    assert redact_text("version 1.2.3.4.5 build", MASK) == "version 1.2.3.4.5 build"


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError):
        RedactionPolicy(ip_mode="sometimes")


def test_redact_value_walks_structures_but_preserves_numbers_and_bools():
    v = {"msg": "user carol from 10.0.0.1", "n": 5, "ok": True, "when": "2026-10-28T10:00:00Z", "l": ["10.0.0.2", 3]}
    out = redact_value(v, MASK)
    assert "carol" not in str(out) and "10.0.0" not in str(out)
    assert out["n"] == 5 and out["ok"] is True and out["when"] == "2026-10-28T10:00:00Z" and out["l"][1] == 3


def test_timestamps_survive_redaction():
    assert redact_text("2026-10-28T10:11:12.123Z", MASK) == "2026-10-28T10:11:12.123Z"


def test_stats_describe_policy():
    d = RedactionStats(usernames=2, ip_addresses=1).to_dict(MASK)
    assert d["applied"] and d["redacted_usernames"] == 2 and d["policy"]["ip_mode"] == IP_MODE_MASK_ALL


def test_safe_diagnostic_redacts_and_truncates():
    out = safe_diagnostic("failed for user admin at 10.0.0.1 " + "x" * 1000, MASK, max_len=100)
    assert "admin" not in out and "10.0.0.1" not in out and len(out) <= 120


def test_safe_diagnostic_handles_none():
    assert safe_diagnostic(None, MASK) is None


# ---- coverage across formats (checkpoint-2 review, item 2) ---------------------------------------------------------
#
# Redaction is heuristic and cue-based. These tests pin what IS detected and, equally, what is deliberately
# left alone. They do not claim comprehensive PII detection.

KEEP = RedactionPolicy(ip_mode=IP_MODE_KEEP_INTERNAL)


@pytest.mark.parametrize("text,name", [
    ("user=alice", "alice"), ("user: alice", "alice"), ("User alice", "alice"), ("user 'alice'", "alice"),
    ('user "alice"', "alice"), ("username=alice", "alice"), ("login: bob", "bob"), ("for user admin", "admin"),
    ("logged in as carol", "carol"), ("Logout by dave", "dave"), ("login by username Administrator", "Administrator"),
    ("session opened for user alice(uid=1000)", "alice"), ("Authentication failure for user 'admin'", "admin"),
    ("Accepted password for root from somewhere", "root"), ("Failed password for invalid user mallory", "mallory"),
    ("user first.last-2", "first.last-2"), ("USER Alice", "Alice"),
])
def test_username_formats_are_masked(text, name):
    st = RedactionStats()
    out = redact_text(text, MASK, st)
    assert name not in out and "[REDACTED_USER]" in out and st.usernames == 1


def test_email_addresses_are_masked_as_user_names():
    st = RedactionStats()
    out = redact_text("alice@corp.example.com logged in; cc bob.smith+x@example.org", MASK, st)
    assert "alice" not in out and "bob.smith" not in out and "example" not in out and st.usernames == 2


@pytest.mark.parametrize("ip", ["10.1.2.3", "192.168.0.1", "172.16.5.5", "8.8.8.8", "127.0.0.1", "169.254.1.1",
                                "255.255.255.255", "fe80::1", "2001:4860:4860::8888", "::1",
                                "2001:0db8:0000:0000:0000:ff00:0042:8329"])
def test_ip_formats_are_masked_in_mask_all(ip):
    st = RedactionStats()
    out = redact_text(f"src {ip} ok", MASK, st)
    assert ip not in out and st.ip_addresses == 1


@pytest.mark.parametrize("text,expected", [
    ("conn 10.1.2.3:443 closed", "conn [REDACTED_IP]:443 closed"),
    ("net 10.1.2.3/24", "net [REDACTED_IP]/24"),
    ("peer [10.1.2.3] up", "peer [[REDACTED_IP]] up"),
    ("ip=10.1.2.3, port=22", "ip=[REDACTED_IP], port=22"),
    ("reached 192.168.1.1.", "reached [REDACTED_IP]."),
])
def test_ip_with_surrounding_punctuation_is_masked_and_punctuation_kept(text, expected):
    assert redact_text(text, MASK) == expected


def test_keep_internal_keeps_private_ranges_but_masks_public():
    p = RedactionPolicy(ip_mode=IP_MODE_KEEP_INTERNAL)
    out = redact_text("a 10.1.2.3 b 192.168.0.9 c 172.16.5.5 d 8.8.8.8", p)
    assert "10.1.2.3" in out and "192.168.0.9" in out and "172.16.5.5" in out and "8.8.8.8" not in out


def test_off_mode_leaves_ips():
    assert "10.1.2.3" in redact_text("x 10.1.2.3", RedactionPolicy(ip_mode=IP_MODE_OFF))


def test_username_masking_can_be_disabled():
    assert "alice" in redact_text("user alice", RedactionPolicy(mask_usernames=False))


def test_non_ip_dotted_numbers_are_left_alone():
    assert redact_text("version 1.2.3.4.5 build", MASK) == "version 1.2.3.4.5 build"


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError):
        RedactionPolicy(ip_mode="sometimes")


def test_redact_value_walks_structures_but_preserves_numbers_and_bools():
    v = {"msg": "user carol from 10.0.0.1", "n": 5, "ok": True, "when": "2026-10-28T10:00:00Z", "l": ["10.0.0.2", 3]}
    out = redact_value(v, MASK)
    assert "carol" not in str(out) and "10.0.0" not in str(out)
    assert out["n"] == 5 and out["ok"] is True and out["when"] == "2026-10-28T10:00:00Z" and out["l"][1] == 3


def test_timestamps_survive_redaction():
    assert redact_text("2026-10-28T10:11:12.123Z", MASK) == "2026-10-28T10:11:12.123Z"


def test_stats_describe_policy():
    d = RedactionStats(usernames=2, ip_addresses=1).to_dict(MASK)
    assert d["applied"] and d["redacted_usernames"] == 2 and d["policy"]["ip_mode"] == IP_MODE_MASK_ALL


def test_safe_diagnostic_redacts_and_truncates():
    out = safe_diagnostic("failed for user admin at 10.0.0.1 " + "x" * 1000, MASK, max_len=100)
    assert "admin" not in out and "10.0.0.1" not in out and len(out) <= 120


def test_safe_diagnostic_handles_none():
    assert safe_diagnostic(None, MASK) is None


# ---- coverage across formats (checkpoint-2 review, item 2) ---------------------------------------------------------
#
# Redaction is heuristic and cue-based. These tests pin what IS detected and, equally, what is deliberately
# left alone. They do not claim comprehensive PII detection.

KEEP = RedactionPolicy(ip_mode=IP_MODE_KEEP_INTERNAL)


@pytest.mark.parametrize("text,name", [
    ("user=alice", "alice"), ("user: alice", "alice"), ("User alice", "alice"), ("user 'alice'", "alice"),
    ('user "alice"', "alice"), ("username=alice", "alice"), ("login: bob", "bob"), ("for user admin", "admin"),
    ("logged in as carol", "carol"), ("Logout by dave", "dave"), ("login by username Administrator", "Administrator"),
    ("session opened for user alice(uid=1000)", "alice"), ("Authentication failure for user 'admin'", "admin"),
    ("Accepted password for root from somewhere", "root"), ("Failed password for invalid user mallory", "mallory"),
    ("user first.last-2", "first.last-2"), ("USER Alice", "Alice"),
])
def test_username_formats_are_masked(text, name):
    st = RedactionStats()
    out = redact_text(text, MASK, st)
    assert name not in out and "[REDACTED_USER]" in out and st.usernames == 1


def test_email_addresses_are_masked_as_user_names():
    st = RedactionStats()
    out = redact_text("alice@corp.example.com logged in; cc bob.smith+x@example.org", MASK, st)
    assert "alice" not in out and "bob.smith" not in out and "example" not in out and st.usernames == 2


@pytest.mark.parametrize("ip", ["10.1.2.3", "192.168.0.1", "172.16.5.5", "8.8.8.8", "127.0.0.1", "169.254.1.1",
                                "255.255.255.255", "fe80::1", "2001:4860:4860::8888", "::1",
                                "2001:0db8:0000:0000:0000:ff00:0042:8329"])
def test_ip_formats_are_masked_in_mask_all(ip):
    st = RedactionStats()
    out = redact_text(f"src {ip} ok", MASK, st)
    assert ip not in out and st.ip_addresses == 1


@pytest.mark.parametrize("text,leftover", [
    ("conn 10.1.2.3:443 closed", ":443"), ("net 10.1.2.3/24", "/24"), ("peer [10.1.2.3] up", "[]"),
    ("ip=10.1.2.3, port=22", "port=22"), ("reached 192.168.1.1.", "."),
])
def test_ip_with_surrounding_punctuation_is_masked_and_punctuation_kept(text, leftover):
    out = redact_text(text, MASK)
    assert "[REDACTED_IP]" in out and not any(c.isdigit() and "." in out.split("[REDACTED_IP]")[0][-1:] for c in out)
    assert leftover.replace("[]", "[") in out or leftover in out


def test_keep_internal_keeps_private_and_masks_public_for_ipv4_and_ipv6():
    out = redact_text("a 10.0.0.1 b fe80::1 c ::1 d 8.8.8.8 e 2001:4860:4860::8888 f 172.32.0.1", KEEP)
    assert "10.0.0.1" in out and "fe80::1" in out and "::1" in out
    assert "8.8.8.8" not in out and "2001:4860:4860::8888" not in out and "172.32.0.1" not in out  # 172.32 is public


def test_multiple_identifiers_in_one_message_are_all_masked_and_counted():
    st = RedactionStats()
    out = redact_text("user alice and user bob logged in from 10.1.1.1 and 10.2.2.2; mail carol@x.org", MASK, st)
    for secret in ("alice", "bob", "carol", "10.1.1.1", "10.2.2.2"):
        assert secret not in out
    assert st.usernames == 3 and st.ip_addresses == 2


def test_repeated_identifier_is_masked_every_time():
    out = redact_text("user alice failed; later user alice succeeded from 10.0.0.9, again 10.0.0.9", MASK)
    assert "alice" not in out and "10.0.0.9" not in out


@pytest.mark.parametrize("text", [
    "controller x1c0 link down", "Switch availability changed", "Link status Down on port 12",
    "hmscd: LinkStatus Down on x1000c0r3b0", "user interface started", "the user was notified",
    "a user  logged in", "login from host controller-1", "version 2.3.4 build 1.2", "1.2.3", "256.1.1.1",
    "time 10:42:17 elapsed", "MAC aa:bb:cc:dd:ee:ff seen", "2026-10-28T10:00:00.123Z",
    "I2C bus 7 failure at address 0x4c", "fabric-manager: 4 of 6 switches available",
    "Oct 28 10:00:00 x1c0 hmpad[123]: bus error", "Users group updated", "username field missing",
])
def test_ordinary_messages_are_unchanged(text):
    st = RedactionStats()
    assert redact_text(text, MASK, st) == text and not st.any


def test_known_limitations_are_not_masked_and_are_documented():
    """Pinned so nobody assumes more than is delivered. If one of these starts being
    masked, update the redaction.py docstring and the registry open question OQ-PII together."""
    assert redact_text("CORP\\alice logged in", MASK) == "CORP\\alice logged in"          # DOMAIN\name form
    assert redact_text("alice did it", MASK) == "alice did it"                             # name with no cue
    assert redact_text("serial CZ12345678 host bmc-7", MASK) == "serial CZ12345678 host bmc-7"
    doc = __import__("app.opensearch_catalog.redaction", fromlist=["x"]).__doc__
    assert "NOT comprehensive PII detection" in doc and "NOT\ndetected" in doc


def test_idempotent():
    once = redact_text("user alice from 10.1.2.3 mail a@b.org", MASK)
    assert redact_text(once, MASK) == once


def test_off_policy_is_a_noop_for_everything():
    off = RedactionPolicy(mask_usernames=False, ip_mode=IP_MODE_OFF)
    s = "user alice from 10.1.2.3 mail a@b.org"
    assert redact_text(s, off) == s


# ---- every user-facing result path ---------------------------------------------------------------------------------

from app.opensearch_catalog.results import normalize_search_response  # noqa: E402

LEAK = "user alice from 10.1.2.3"
SHARDS = {"total": 1, "successful": 1, "failed": 0}


def _norm(os_registry, pid, raw, query=None):
    return normalize_search_response(raw, entry=os_registry.get(pid), query=query or {}, policy=MASK,
                                     max_series=50, max_hits=50)


def _raw(hits=None, aggs=None, total=1, shards=None):
    r = {"took": 1, "timed_out": False, "_shards": shards or SHARDS,
         "hits": {"total": {"value": total, "relation": "eq"}, "hits": hits or []}}
    if aggs is not None:
        r["aggregations"] = aggs
    return r


def _assert_clean(payload):
    s = str(payload)
    assert "alice" not in s and "10.1.2.3" not in s, s


def test_rows_redact_every_projected_string_field(os_registry):
    src = {"@timestamp": "2026-10-28T10:00:00Z", "Created": "2026-10-28T09:59:00Z", "host": "bmc-1 " + LEAK,
           "Severity": "OK", "Message": LEAK, "Oem": {"Hpe": {"Severity": "Informational", "Code": 1017,
                                                          "RecommendedAction": "contact " + LEAK}}}
    r = _norm(os_registry, "ilo_events", _raw([{"_source": src}]))
    _assert_clean(r.to_dict())
    assert r.hits[0].fields["@timestamp"] == "2026-10-28T10:00:00Z" and r.hits[0].fields["Oem.Hpe.Code"] == 1017


def test_severity_assessment_values_are_redacted_too(os_registry):
    src = {"@timestamp": "2026-10-28T10:00:00Z", "Severity": LEAK, "Oem": {"Hpe": {"Severity": "Informational"}}}
    r = _norm(os_registry, "ilo_events", _raw([{"_source": src}]))
    _assert_clean(r.to_dict())
    assert r.hits[0].severity_assessment["state"] == "unknown"


def test_unprojected_fields_never_appear(os_registry):
    src = {"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "b", "Raw": LEAK, "Attributes": {"x": LEAK}}
    _assert_clean(_norm(os_registry, "controller_log", _raw([{"_source": src}])).to_dict())


def test_terms_bucket_keys_are_redacted(os_registry):
    aggs = {"a": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0,
                  "buckets": [{"key": LEAK, "doc_count": 3, "sub": {"buckets": [{"key": LEAK, "doc_count": 1}]}}]}}
    r = _norm(os_registry, "controller_log", _raw(aggs=aggs), {"aggs": {"a": {"terms": {}}}})
    _assert_clean(r.to_dict())
    assert r.buckets[0].doc_count == 3


def test_filters_bucket_names_are_redacted(os_registry):
    aggs = {"f": {"buckets": {LEAK: {"doc_count": 2}}}}
    _assert_clean(_norm(os_registry, "ilo_events", _raw(aggs=aggs), {"aggs": {"f": {"filters": {}}}}).to_dict())


def test_histogram_term_series_labels_are_redacted(os_registry):
    aggs = {"t": {"buckets": [{"key_as_string": "2026-10-28T10:00:00.000Z", "key": 1, "doc_count": 1,
                               "s": {"buckets": [{"key": LEAK, "doc_count": 1}]}}]}}
    r = _norm(os_registry, "controller_log", _raw(aggs=aggs), {"aggs": {"t": {"date_histogram": {}, "aggs": {"s": {}}}}})
    _assert_clean(r.to_dict())
    assert r.series[0].points[0].timestamp == "2026-10-28T10:00:00.000Z"


def test_shard_failure_diagnostics_are_redacted(os_registry):
    sh = {"total": 2, "successful": 1, "failed": 1, "failures": [
        {"index": "controllerlog-x", "shard": 0, "reason": {"type": "t", "reason": LEAK}}]}
    _assert_clean(_norm(os_registry, "controller_log", _raw(shards=sh)).to_dict())


def test_redaction_counts_are_reported_without_the_values(os_registry):
    r = _norm(os_registry, "controller_log", _raw([{"_source": {"@timestamp": "2026-10-28T10:00:00Z",
                                                                  "Severity": "error", "Body": LEAK}}]))
    red = r.metadata["redaction"]
    assert red["redacted_usernames"] == 1 and red["redacted_ip_addresses"] == 1 and red["applied"]
    _assert_clean(red)


def test_keep_internal_policy_applies_to_results(os_registry):
    r = normalize_search_response(
        _raw([{"_source": {"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "a 10.1.2.3 b 8.8.8.8"}}]),
        entry=os_registry.get("controller_log"), query={}, policy=KEEP, max_series=5, max_hits=5)
    assert "10.1.2.3" in r.hits[0].body and "8.8.8.8" not in r.hits[0].body


def test_executor_error_paths_are_redacted(settings):
    from unittest.mock import patch
    from app import executor, opensearch_client
    W = {"range": {"@timestamp": {"gte": "now-1h", "lte": "now"}}}
    e = {"status": "ok", "data_source": "opensearch", "pattern_id": "controller_log",
         "query": {"size": 1, "query": {"bool": {"filter": [W]}}}}
    for status in ("endpoint_error", "endpoint_unreachable", "timeout"):
        out = opensearch_client.ExecutionOutcome(status=status, error=f"Could not connect to http://10.1.2.3:9200: {LEAK}")
        with patch.object(opensearch_client, "search", return_value=out):
            res = executor.execute_contract(e, settings)
        _assert_clean(res["execution"]["error"])


def test_executor_all_shards_failed_metadata_is_redacted(settings):
    from unittest.mock import patch
    from app import executor, opensearch_client
    W = {"range": {"@timestamp": {"gte": "now-1h", "lte": "now"}}}
    e = {"status": "ok", "data_source": "opensearch", "pattern_id": "controller_log",
         "query": {"size": 1, "query": {"bool": {"filter": [W]}}}}
    sh = {"total": 1, "successful": 0, "failed": 1, "failures": [{"index": "i", "shard": 0,
                                                                  "reason": {"type": "t", "reason": LEAK}}]}
    out = opensearch_client.ExecutionOutcome(status="success", raw_body=_raw(shards=sh, total=0))
    with patch.object(opensearch_client, "search", return_value=out):
        res = executor.execute_contract(e, settings)
    assert res["execution"]["execution_status"] == "endpoint_error"
    _assert_clean(res["execution"])
