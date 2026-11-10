"""
app/auth/refresh.py

Re-resolve the *current* authorization data for an already-authenticated
identity.

A Principal is a snapshot: it carries the role, the account status and the
four resource-scope lists as they were when the identity was resolved. An
administrator can change any of them while a request -- or a whole agent
turn -- is still running. A snapshot taken at the start of such a turn would
therefore keep authorizing operations that the administrator has already
revoked.

This module closes that window. It re-reads the caller's row from the User
and Role Store using the **already authenticated identity key**
(issuer, subject) and rebuilds the Principal from the row's *current* data.

Two rules hold everywhere it is used:

* The identity is never re-established. Only the authorization data is
  refreshed: the issuer and the subject -- the pair Authentik vouches for --
  are the ones the store is queried with, and the user id, role, status and
  scopes are whatever the store says *now*. A replacement user id or role
  arriving from the model, from a tool argument or from a request body is
  never accepted.
* A failure to establish the current state is a refusal, not a fallback to
  the snapshot. Falling back to the stale snapshot would silently resurrect
  revoked permissions.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

from app.auth.principal import Principal, set_current_principal

logger = logging.getLogger(__name__)

#: The principal that was refreshed for the operation currently in progress.
#:
#: The cache exists only for the duration of one protected operation, and it
#: is *not* a general-purpose cache: it is discarded as soon as the operation
#: ends, so the next operation always reads the store again and a
#: revocation that lands in between is always seen. Within an operation the
#: authorization data cannot change in a way that matters -- the operation
#: has already decided what it is going to do.
_OPERATION_PRINCIPAL: ContextVar[Principal | None] = ContextVar(
    "operation_principal", default=None
)


@contextmanager
def authorization_operation() -> Iterator[None]:
    """Scope the current authorization data to a single protected operation.

    The scope is opened by the operation's entry point (an agent tool call,
    for example) and closed when it ends. Nothing survives it: the next
    operation starts from the store again.
    """
    token = _OPERATION_PRINCIPAL.set(None)
    try:
        yield
    finally:
        try:
            _OPERATION_PRINCIPAL.reset(token)
        except ValueError:  # pragma: no cover - the context was replaced meanwhile
            _OPERATION_PRINCIPAL.set(None)


class PrincipalRefreshError(Exception):
    """The current authorization data could not be established.

    The store could not be reached, or it answered in a way that cannot be
    interpreted. The caller must fail closed.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def is_refreshable(principal: Principal | None) -> bool:
    """Can this principal be re-resolved from the User and Role Store?

    Only a principal that was itself resolved from the store qualifies: it
    carries the identity key the store is keyed by. A principal built in
    memory (a development-anonymous principal, or a test double) has no
    store row to be re-resolved from, so it is used as it is.
    """
    if principal is None:
        return False
    if principal.development_mode:
        return False
    return bool(principal.store_backed and principal.issuer and principal.subject)


def refresh_principal(principal: Principal | None) -> Principal | None:
    """Return the principal with its *current* authorization data.

    Returns:
        The refreshed principal, or ``None`` when the identity is no longer
        known to the store (the account was removed) or when the principal
        is not refreshable -- in which case it is returned unchanged.

    Raises:
        PrincipalRefreshError: the store could not be read, so the current
            authorization state is unknown. The caller must fail closed.
    """
    if not is_refreshable(principal):
        return principal

    assert principal is not None  # narrowed by is_refreshable
    issuer, subject = principal.issuer, principal.subject

    # Within one protected operation the authorization data has already been
    # established; re-reading it would be a redundant round trip to the
    # store. The cache never outlives the operation.
    cached = _OPERATION_PRINCIPAL.get()
    if cached is not None and cached.identity_key == (issuer, subject):
        return cached

    try:
        from app.storage.database import get_db_session
        from app.storage.user_store import UserStore

        with get_db_session() as session:
            user = UserStore.get_user_by_identity(session, issuer, subject)
            if user is None:
                logger.info(
                    "The authenticated identity (%s, %s) is no longer present in the "
                    "role store; the request is refused.",
                    issuer, subject,
                )
                return None
            refreshed = Principal.from_user_model(user, app=principal.app)
    except PrincipalRefreshError:
        raise
    except Exception as exc:  # noqa: BLE001 - the store is a hard dependency here
        raise PrincipalRefreshError(
            f"The current permissions of this account could not be verified: {exc}"
        ) from exc

    # The identity key is the one the store was queried with, so it cannot
    # have changed; everything else is the store's current answer.
    return refreshed


def has_changed(original: Principal | None, refreshed: Principal | None) -> bool:
    """Did the authorization data change between the two resolutions?"""
    if original is None or refreshed is None:
        return original is not refreshed
    return (
        original.status != refreshed.status
        or (original.role or "") != (refreshed.role or "")
        or original.authz_version != refreshed.authz_version
        or original.allowed_folder_uids != refreshed.allowed_folder_uids
        or original.allowed_dashboard_uids != refreshed.allowed_dashboard_uids
        or original.denied_dashboard_uids != refreshed.denied_dashboard_uids
        or original.allowed_environments != refreshed.allowed_environments
    )


def current_principal(
    principal: Principal | None,
    *,
    error: Callable[[str], BaseException],
) -> Principal:
    """Return the principal with its current permissions, or raise.

    ``error`` builds the exception the caller raises when the current state
    cannot be established -- the HTTP layer uses ``AuthorizationDenied``,
    the mutation paths use ``PermissionError``. The two callers differ only
    in the exception they surface; the decision itself is identical and is
    always a refusal.
    """
    if principal is None:
        raise error("An authenticated identity is required for this operation.")

    try:
        refreshed = refresh_principal(principal)
    except PrincipalRefreshError as exc:
        logger.error("Refusing an operation: %s", exc.reason)
        raise error(
            "The current permissions of this account could not be verified."
        ) from exc

    if refreshed is None:
        raise error("This account is no longer present in the role store.")
    if not refreshed.is_active:
        raise error("Account is not active.")

    if has_changed(principal, refreshed):
        logger.info(
            "Refreshing the principal of %s (status %s -> %s, role %s -> %s, "
            "authz_version %s -> %s).",
            principal.user_id,
            principal.status, refreshed.status,
            principal.role, refreshed.role,
            principal.authz_version, refreshed.authz_version,
        )
        # Rebind the context so that the checks which follow -- in this
        # operation and in the rest of the turn -- use the current
        # permissions. The binding is context-local, so a concurrent
        # request is unaffected.
        set_current_principal(refreshed)

    # Remember the current data for the rest of this operation, so that the
    # internal steps of one operation do not each re-read the store.
    _OPERATION_PRINCIPAL.set(refreshed)

    return refreshed


__all__ = [
    "PrincipalRefreshError",
    "authorization_operation",
    "current_principal",
    "has_changed",
    "is_refreshable",
    "refresh_principal",
]
