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
