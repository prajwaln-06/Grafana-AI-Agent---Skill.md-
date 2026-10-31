"""
redaction.py

Masks identifiable user names and IP addresses in values that are about to be
returned to a user. Applies to string values only: timestamps, counts,
severity values, field names and aggregation metadata are structured data and
are preserved.

This is a best-effort, pattern-based control, not a guarantee: a user name that
appears in free text without one of the recognised cues (e.g. "login: <name>",
"for user <name>", "user <name>") is NOT detected. The allow-listed projection
(registry `output_projection`) remains the primary control; redaction is the
second layer. Limits are documented in the registry's open_question OQ-PII.

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

_NOT_A_USER = (r"(?!(?:from|by|as|for|to|the|a|an|on|in|at|failed|failure|success|successful|"
               r"attempt|attempts|event|events|session|sessions|timeout|error|errors|via|using|"
               r"with|name|user|users|id)\b)")
_USER_TOKEN = r"[A-Za-z0-9_][\w.\-@\\$]*"
_USER_PATTERNS = (
    # "login: root", "logged in as bob", "logout by alice"
    re.compile(rf"(?i)(\b(?:log(?:in|out)|logged\s+(?:in|out))\b\s*(?:by|as|for)?\s*[:\-]?\s*){_NOT_A_USER}(?P<u>{_USER_TOKEN})"),
    # "for user root", "user 'bob'", "username=alice"
    re.compile(rf"(?i)(\b(?:for\s+user|user(?:name)?)\b\s*[:=]?\s*['\"]?){_NOT_A_USER}(?P<u>{_USER_TOKEN})"),
)


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
