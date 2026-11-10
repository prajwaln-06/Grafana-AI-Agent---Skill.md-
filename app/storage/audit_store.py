"""
app/storage/audit_store.py

Append-only security audit log repository.

The log is append-only: this module exposes no update and no delete
operation, so no application API -- authenticated or not -- can rewrite or
remove a historical record.

Every payload that is stored is passed through :func:`redact_sensitive`
first. Passwords, cookies, bearer/access/refresh/ID tokens, session secrets
and API keys are replaced with a placeholder at any nesting depth, so a
caller that accidentally puts a credential into an audit ``details`` mapping
cannot persist it.
"""
from __future__ import annotations

import datetime
from typing import Any, Mapping

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.storage.models import AuditLogModel

#: The placeholder written in place of a redacted value.
REDACTED = "[redacted]"

#: Key fragments that mark a value as secret. A key matches when it contains
#: one of these fragments (case-insensitively), so ``refresh_token``,
#: ``X-Access-Token`` and ``userPassword`` are all caught.
SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "authorization",
    "auth_header",
    "credential",
    "api_key",
    "apikey",
    "private_key",
    "session",
    "jwt",
    "bearer",
)

#: Values that are themselves secret regardless of the key they are stored
#: under (for example a JWT pasted into a free-text field).
_SECRET_VALUE_PATTERNS = (
    "eyJ",       # JWT
    "glsa_",     # Grafana service account token
    "Bearer ",
)


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def _is_secret_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return any(pattern in value for pattern in _SECRET_VALUE_PATTERNS)


def redact_sensitive(value: Any) -> Any:
    """Return a copy of ``value`` with every secret replaced by a placeholder.

    Redaction is applied recursively to mappings and sequences. A mapping key
    that names a secret has its whole value replaced; a free-text value that
    looks like a credential is replaced as well.
    """
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                redacted[str(key)] = REDACTED
            else:
                redacted[str(key)] = redact_sensitive(item)
        return redacted
    if isinstance(value, (list, tuple, set)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str) and _is_secret_value(value):
        return REDACTED
    return value


class AuditStore:
    """Manages append-only audit log records."""

    @staticmethod
    def log_event(
        session: Session,
        *,
        event_type: str,
        actor_id: str | None = None,
        actor_issuer: str | None = None,
        actor_subject: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        decision: str | None = None,
        reason: str | None = None,
        previous_state: Mapping[str, Any] | None = None,
        new_state: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
        timestamp: datetime.datetime | None = None,
    ) -> AuditLogModel:
        """Record a security-relevant event to the append-only log."""
        record = AuditLogModel(
            event_type=event_type,
            actor_id=actor_id,
            actor_issuer=actor_issuer,
            actor_subject=actor_subject,
            target_type=target_type,
            target_id=target_id,
            decision=decision,
            reason=reason,
            previous_state=redact_sensitive(dict(previous_state)) if previous_state is not None else None,
            new_state=redact_sensitive(dict(new_state)) if new_state is not None else None,
            details=redact_sensitive(dict(details)) if details is not None else None,
            timestamp=timestamp or datetime.datetime.now(datetime.timezone.utc),
        )
        session.add(record)
        session.flush()
        return record

    @staticmethod
    def list_events(
        session: Session,
        *,
        limit: int = 100,
        offset: int = 0,
        event_type: str | None = None,
    ) -> list[AuditLogModel]:
        """Fetch audit log records ordered by timestamp descending."""
        stmt = select(AuditLogModel).order_by(desc(AuditLogModel.timestamp)).limit(limit).offset(offset)
        if event_type:
            stmt = stmt.where(AuditLogModel.event_type == event_type)
        return list(session.execute(stmt).scalars().all())


__all__ = [
    "REDACTED",
    "SENSITIVE_KEY_FRAGMENTS",
    "AuditStore",
    "redact_sensitive",
]
