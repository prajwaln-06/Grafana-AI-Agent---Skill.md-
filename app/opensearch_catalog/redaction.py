"""
redaction.py

Masks identifiable user names and IP addresses in values that are about to be
returned to a user. Applies to string values only: timestamps, counts,
severity values, field names and aggregation metadata are structured data and
are preserved.

This is a best-effort, pattern-based control, NOT comprehensive PII detection.
Detected: a name after a cue ("login: <name>", "login by [username] <name>",
"logged in as <name>", "for user <name>", "user[name] <name>", "password for
[invalid user] <name>"), e-mail addresses, IPv4 and IPv6 addresses. NOT
detected: a name with no cue; "DOMAIN\\name" forms; names in a field of their
own; hostnames, MAC addresses, serial numbers or other identifiers; an address
written in an unusual form (e.g. integer or hex IPv4). A cue followed by an
ordinary word ("user interface") is deliberately left alone, which also means a
real name equal to one of those words is not masked. The allow-listed projection
(registry `output_projection`) remains the primary control; redaction is the
second layer. Limits are documented in skills/references/opensearch-logs/overview.md (Result metadata and limits).

IP modes (configurable because internal addresses help infrastructure
troubleshooting):
  "mask_all"      every IPv4/IPv6 address is masked (default)
  "keep_internal" private, loopback and link-local addresses are kept; public
                  addresses are masked
  "off"           no IP masking
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

IP_MODE_MASK_ALL = "mask_all"
IP_MODE_KEEP_INTERNAL = "keep_internal"
IP_MODE_OFF = "off"
IP_MODES = frozenset({IP_MODE_MASK_ALL, IP_MODE_KEEP_INTERNAL, IP_MODE_OFF})

USER_PLACEHOLDER = "[REDACTED_USER]"
IP_PLACEHOLDER = "[REDACTED_IP]"

_IPV4_RE = re.compile(r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\w]|\.\d)")
_H = r"[0-9A-Fa-f]{1,4}"
# Only the full 8-group form and forms containing "::" -- a plain "10:42:17"
# time or a MAC address is NOT an IPv6 address and must not match.
_IPV6_RE = re.compile(
    rf"(?<![\w:.])(?:(?:{_H}:){{7}}{_H}|(?:{_H}:){{1,7}}:(?:{_H}(?::{_H}){{0,6}})?|::(?:{_H}(?::{_H}){{0,6}})?)(?![\w:.])"
)

# Words that follow a cue like "user" in ordinary prose ("user interface",
# "the user was notified") and are not names.
_NOT_A_USER = (r"(?!(?:from|by|as|for|to|the|a|an|on|in|at|of|and|or|is|was|were|are|be|been|being|has|have|had|"
               r"did|does|do|will|would|can|could|may|might|should|must|not|no|failed|failure|success|successful|"
               r"attempt|attempts|event|events|session|sessions|timeout|error|errors|via|using|with|name|names|"
               r"user|users|id|interface|interfaces|account|accounts|access|authentication|auth|permission|"
               r"permissions|group|groups|role|roles|password|passwords|process|mode|agent|input|data|logged|"
               r"login|logins|logout|logouts|that|this|which|when|while|after|before|unknown|invalid|none|null|field|fields|value|values|missing|required|list|table|column|attribute|attributes|record|records|database|directory|database|profile|profiles|type|types|level|state|status|key|keys|file|files|login|home|shell|limit|limits|quota|count|counts|total|table|prompt|message|messages|login)\b)")
_USER_TOKEN = r"[A-Za-z0-9_][\w.\-@\\$]*"
_USER_PATTERNS = (
    # "login: root", "logged in as bob", "logout by alice", "login by username Administrator"
    re.compile(rf"(?i)(\b(?:log(?:in|out)|logged\s+(?:in|out))\b\s*(?:by|as|for)?\s*[:\-]?\s*"
               rf"(?:user(?:name)?\b\s*[:=]?\s*)?['\"]?){_NOT_A_USER}(?P<u>{_USER_TOKEN})"),
    # "for user root", "user 'bob'", "username=alice"
    re.compile(rf"(?i)(\b(?:for\s+user|user(?:name)?)\b\s*[:=]?\s*['\"]?){_NOT_A_USER}(?P<u>{_USER_TOKEN})"),
    # "Accepted password for root", "Failed password for invalid user admin"
    re.compile(rf"(?i)(\bpassword\s+for\s+(?:invalid\s+user\s+)?){_NOT_A_USER}(?P<u>{_USER_TOKEN})"),
)
# An e-mail address identifies a person; masked as a user name.
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


@dataclass(frozen=True)
class RedactionPolicy:
    mask_usernames: bool = True
    ip_mode: str = IP_MODE_MASK_ALL

    def __post_init__(self):
        if self.ip_mode not in IP_MODES:
            raise ValueError(f"ip_mode must be one of {sorted(IP_MODES)}, got {self.ip_mode!r}")

    @property
    def active(self) -> bool:
        return self.mask_usernames or self.ip_mode != IP_MODE_OFF

    def describe(self) -> dict:
        return {"usernames": self.mask_usernames, "ip_mode": self.ip_mode}


@dataclass
class RedactionStats:
    usernames: int = 0
    ip_addresses: int = 0

    @property
    def any(self) -> bool:
        return bool(self.usernames or self.ip_addresses)

    def to_dict(self, policy: RedactionPolicy) -> dict:
        return {"applied": policy.active, "policy": policy.describe(),
                "redacted_usernames": self.usernames, "redacted_ip_addresses": self.ip_addresses}


def _mask_ip(match: re.Match, policy: RedactionPolicy, stats: RedactionStats) -> str:
    text = match.group(0)
    if policy.ip_mode == IP_MODE_KEEP_INTERNAL:
        try:
            addr = ipaddress.ip_address(text)
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                return text
        except ValueError:
            return text
    stats.ip_addresses += 1
    return IP_PLACEHOLDER


def redact_text(text: str, policy: RedactionPolicy, stats: RedactionStats | None = None) -> str:
    if not isinstance(text, str) or not text or not policy.active:
        return text
    stats = stats if stats is not None else RedactionStats()
    out = text
    if policy.mask_usernames:
        def _user(m: re.Match) -> str:
            stats.usernames += 1
            return m.group(1) + USER_PLACEHOLDER
        for pattern in _USER_PATTERNS:
            out = pattern.sub(_user, out)

        def _email(_m: re.Match) -> str:
            stats.usernames += 1
            return USER_PLACEHOLDER
        out = _EMAIL_RE.sub(_email, out)
    if policy.ip_mode != IP_MODE_OFF:
        out = _IPV4_RE.sub(lambda m: _mask_ip(m, policy, stats), out)
        out = _IPV6_RE.sub(lambda m: _mask_ip(m, policy, stats), out)
    return out


def redact_value(value: Any, policy: RedactionPolicy, stats: RedactionStats | None = None) -> Any:
    """Recursively redact strings inside lists/dicts. Dict KEYS and non-string
    scalars (numbers, booleans, None) are preserved."""
    stats = stats if stats is not None else RedactionStats()
    if isinstance(value, str):
        return redact_text(value, policy, stats)
    if isinstance(value, list):
        return [redact_value(v, policy, stats) for v in value]
    if isinstance(value, dict):
        return {k: redact_value(v, policy, stats) for k, v in value.items()}
    return value


def safe_diagnostic(text: str | None, policy: RedactionPolicy, *, max_len: int = 300) -> str | None:
    """Redact then truncate an error/diagnostic string. Backend error text can
    echo document content, so it is never returned unmodified."""
    if text is None:
        return None
    cleaned = redact_text(str(text), policy)
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1] + "…"
    return cleaned
