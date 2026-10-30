"""
app/storage/audit_store.py

Append-only security audit log repository.
"""
from __future__ import annotations

import datetime
from typing import Any, Mapping

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.storage.models import AuditLogModel


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
            previous_state=dict(previous_state) if previous_state is not None else None,
            new_state=dict(new_state) if new_state is not None else None,
            details=dict(details) if details is not None else None,
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
