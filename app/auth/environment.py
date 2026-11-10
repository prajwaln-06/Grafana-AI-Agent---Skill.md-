"""
app/auth/environment.py

Environment-scope resolution (RBAC Section 8.2, environment dimension).

The environment dimension is a first-class part of every user's RBAC scope
record (`allowed_environments` in the User & Role Store). Nothing about an
environment is trusted from the client: the set of environments a deployment
recognises is derived from server-side configuration plus the role store, and
a requested environment is only ever accepted after it has been checked
against BOTH that catalogue and the authenticated principal's own permitted
environments.

Every enforcement point (PromQL execution, alert-rule proposal / approval /
execution) resolves the environment through :func:`resolve_environment`
*before* touching Prometheus or Grafana, so a rejected operation never
reaches the query executor or a Grafana write.
"""
from __future__ import annotations

import logging
import threading
import time
from app.auth.principal import Principal
from app.config import get_settings

logger = logging.getLogger(__name__)

#: How long the deployment-wide environment catalogue is cached.
_CATALOGUE_TTL_SECONDS = 30.0

_catalogue_lock = threading.Lock()
_catalogue_at: float = 0.0
_catalogue: tuple[str, ...] = ()


class EnvironmentDenied(Exception):
    """The requested environment is unknown to this deployment or not permitted.

    Maps to HTTP 403.
    """

    status_code = 403

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EnvironmentRequired(Exception):
    """No usable environment context could be resolved for the operation.

    Maps to HTTP 400.
    """

    status_code = 400

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def reset_environment_catalogue() -> None:
    """Drop the cached environment catalogue (used after admin scope changes)."""
    global _catalogue_at, _catalogue
    with _catalogue_lock:
        _catalogue_at = 0.0
        _catalogue = ()


def _store_environments() -> set[str]:
    """Distinct environments present in the User & Role Store."""
    try:
        from app.storage.database import get_db_session
        from app.storage.user_store import UserStore

        with get_db_session() as session:
            return {
                str(name).strip().lower()
                for name in UserStore.list_known_environments(session)
                if str(name).strip()
            }
    except Exception as exc:  # pragma: no cover - defensive: store may be unavailable
        logger.debug("Could not read the environment catalogue from the role store: %s", exc)
        return set()


def deployment_environments() -> list[str]:
    """The environments this deployment recognises, from server-side data only."""
    global _catalogue_at, _catalogue

    now = time.monotonic()
    with _catalogue_lock:
        if _catalogue and (now - _catalogue_at) < _CATALOGUE_TTL_SECONDS:
            return list(_catalogue)

    settings = get_settings()
    names = {
        str(name).strip().lower()
        for name in (settings.environments or [])
        if str(name).strip()
    }
    names |= _store_environments()

    ordered = tuple(sorted(names))
    with _catalogue_lock:
        _catalogue_at = now
        _catalogue = ordered
    return list(ordered)


def known_environments(principal: Principal | None = None) -> list[str]:
    """The deployment catalogue plus the principal's own permitted environments."""
    names = set(deployment_environments())
    if principal is not None:
        names |= permitted_environments(principal)
    return sorted(names)


def permitted_environments(principal: Principal) -> set[str]:
    """The principal's permitted environment names (lower-cased, no wildcard)."""
    return {
        str(name).strip().lower()
        for name in (principal.allowed_environments or ())
        if str(name).strip() and str(name).strip() != "*"
    }


def _is_admin(principal: Principal) -> bool:
    return (principal.role or "").lower() == "admin"


def resolve_environment(
    principal: Principal | None,
    requested: str | None = None,
    *,
    required: bool = False,
) -> str | None:
    """Resolve and validate the environment an operation targets.

    Resolution order for the *target* environment:

    1. An explicitly requested environment (client supplied, but validated here).
    2. The deployment's configured default environment, when the principal may
       use it.
    3. The principal's only permitted environment.
    4. The principal's first permitted environment (deterministic).

    The resolved environment is then checked against the deployment catalogue
    and against the principal's own permitted environments. Anything unknown,
    unassigned or ambiguous is rejected -- the function never falls back to
    "no restriction".

    Raises:
        EnvironmentDenied: the environment is unknown or not permitted (403).
        EnvironmentRequired: no environment context could be resolved (400).
    """
    if principal is None:
        raise EnvironmentDenied("An authenticated identity is required for this operation.")
    if not principal.is_active:
        raise EnvironmentDenied("Account is not active.")

    role = (principal.role or "").lower()
    permitted = permitted_environments(principal)
    catalogue = set(deployment_environments()) | permitted
    requested_env = str(requested or "").strip().lower()

    # The environment dimension is only inert when the deployment does not
    # have one at all. As soon as the deployment knows environments (or the
    # principal has some), an operation must resolve to one of them.
    has_environment_dimension = bool(catalogue)

    if requested_env:
        if catalogue and requested_env not in catalogue:
            raise EnvironmentDenied(
                f"Environment '{requested_env}' is not recognised by this deployment."
            )
        target = requested_env
    else:
        target = ""
        configured_default = str(get_settings().default_environment or "").strip().lower()
        if configured_default and (
            configured_default in permitted or role == "admin"
        ):
            target = configured_default
        elif len(permitted) == 1:
            target = next(iter(permitted))
        elif permitted:
            target = sorted(permitted)[0]
        elif role == "admin" and catalogue:
            target = sorted(catalogue)[0]

        if not target:
            if required:
                raise EnvironmentRequired(
                    "An environment must be specified for this operation."
                )
            if has_environment_dimension:
                raise EnvironmentDenied(
                    "No environments are assigned to your account; environment-scoped "
                    "operations are not permitted."
                )
            return None

    if role == "admin":
        # Administrators retain access to every environment this deployment knows.
        return target

    if not permitted:
        raise EnvironmentDenied(
            "No environments are assigned to your account; environment-scoped "
            "operations are not permitted."
        )

    if target not in permitted:
        raise EnvironmentDenied(
            f"Environment '{target}' is outside your permitted environments."
        )

    return target


__all__ = [
    "EnvironmentDenied",
    "EnvironmentRequired",
    "deployment_environments",
    "known_environments",
    "permitted_environments",
    "reset_environment_catalogue",
    "resolve_environment",
]
