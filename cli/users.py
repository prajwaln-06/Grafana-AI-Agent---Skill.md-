"""
cli/users.py

``agent-cli admin users add`` -- create a new account in the application's
User & Role Store.

Design reference
----------------
"RBAC_Grafana_ADK_Final (1).md":

* section 5.2 (user lifecycle) -- an unknown identity is created ``pending``,
  with no role and no privileges; an administrator activates it and assigns
  the role and the scopes;
* section 5.3 (administrator operations) -- the four scope lists are
  ``allowed_folder_uids``, ``allowed_dashboard_uids``,
  ``denied_dashboard_uids`` and ``allowed_environments``, and folder and
  dashboard UIDs are validated against Grafana through the reader account;
* section 5.4 (administrative security) -- no self-privilege change, no
  change to the last active administrator, and every change is audited in
  the same transaction as the change itself;
* section 6 (CLI architecture and command examples) -- the command groups and
  the four scope flags (``--allow-folder``, ``--allow-dashboard``,
  ``--deny-dashboard``, ``--allow-env``).

Section 5.2 also recommends that a command which needs direct database access
is shipped "as a server-side management command that needs database access on
the host rather than as a subcommand of the user-facing CLI, so the user CLI
contains no privileged path". This command is that management command: it
runs on the host, against the same database the service uses.

Everything the command does is the application's own logic. The record is
created by :meth:`app.storage.user_store.UserStore.create_pending_user` and
the role, status and scopes are applied by
:meth:`app.storage.user_store.UserStore.update_user_access`, so the audit
log, the ``authz_version`` counter and the guardrails behave exactly as they
do for the Admin UI.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from app.auth.policy import VALID_ROLES, VALID_STATUSES
from app.storage.database import get_db_session
from app.storage.seed import DEFAULT_ISSUER
from app.storage.user_store import AdminSecurityError, UserStore


class UserInputError(ValueError):
    """The command was called with an argument the store cannot accept."""


class DuplicateUserError(UserInputError):
    """A user with the same ``(issuer, subject)`` identity already exists."""


#: A Grafana UID (folder or dashboard) or a scope wildcard.
_UID_RE = re.compile(r"^[A-Za-z0-9_.:*-]+$")

#: An environment name.
_ENV_RE = re.compile(r"^[A-Za-z0-9_.*-]+$")

#: A syntactically plausible email address.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_MAX_SUBJECT = 255
_MAX_ISSUER = 512


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def _required_text(value: str | None, option: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise UserInputError(f"{option} is required.")
    return cleaned


def _optional_text(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def validate_subject(subject: str | None) -> str:
    """Validate the Authentik subject, the only mandatory attribute."""
    cleaned = _required_text(subject, "--subject")
    if len(cleaned) > _MAX_SUBJECT:
        raise UserInputError("--subject is longer than 255 characters.")
    if any(char.isspace() for char in cleaned):
        raise UserInputError("--subject must not contain whitespace.")
    return cleaned


def validate_issuer(issuer: str | None) -> str:
    """Validate the identity issuer. The configured one is the default."""
    if issuer is None:
        return DEFAULT_ISSUER
    cleaned = _required_text(issuer, "--issuer")
    if len(cleaned) > _MAX_ISSUER:
        raise UserInputError("--issuer is longer than 512 characters.")
    if not cleaned.startswith(("http://", "https://")):
        raise UserInputError("--issuer must be an http:// or https:// URL.")
    return cleaned


def validate_email(email: str | None) -> str | None:
    """Validate the informational email address."""
    cleaned = _optional_text(email)
    if cleaned is None:
        return None
    if not _EMAIL_RE.match(cleaned):
        raise UserInputError("--email is not a valid email address.")
    return cleaned


def validate_role(role: str | None) -> str | None:
    """Validate the application role against the roles the Policy Module knows."""
    cleaned = _optional_text(role)
    if cleaned is None:
        return None
    cleaned = cleaned.lower()
    if cleaned not in VALID_ROLES:
        raise UserInputError(
            f"--role must be one of: {', '.join(sorted(VALID_ROLES))}."
        )
    return cleaned


def validate_status(status: str | None) -> str:
    """Validate the account status."""
    cleaned = _optional_text(status)
    if cleaned is None:
        return "pending"
    cleaned = cleaned.lower()
    if cleaned not in VALID_STATUSES:
        raise UserInputError(
            f"--status must be one of: {', '.join(sorted(VALID_STATUSES))}."
        )
    return cleaned


def validate_uid_list(values: Iterable[str] | None, option: str) -> list[str]:
    """Validate a list of Grafana UIDs (or the ``*`` wildcard)."""
    result: list[str] = []
    for value in values or ():
        cleaned = _required_text(value, option)
        if "," in cleaned:
            raise UserInputError(
                f"{option} takes one UID per flag; '{cleaned}' contains a comma."
            )
        if not _UID_RE.match(cleaned):
            raise UserInputError(
                f"{option} accepts Grafana UIDs only; '{cleaned}' is not one."
            )
        if cleaned not in result:
            result.append(cleaned)
    return result


def validate_environments(values: Iterable[str] | None) -> list[str]:
    """Validate the environment names of the environment scope."""
    result: list[str] = []
    for value in values or ():
        cleaned = _required_text(value, "--allow-env")
        if not _ENV_RE.match(cleaned):
            raise UserInputError(
                f"--allow-env accepts environment names only; '{cleaned}' is not one."
            )
        if cleaned not in result:
            result.append(cleaned)
    return result


# ---------------------------------------------------------------------------
# The command itself
# ---------------------------------------------------------------------------

def add_user(
    *,
    subject: str,
    issuer: str | None = None,
    username: str | None = None,
    display_name: str | None = None,
    email: str | None = None,
    role: str | None = None,
    status: str | None = None,
    admin_user_id: str | None = None,
    allowed_folder_uids: Iterable[str] | None = None,
    allowed_dashboard_uids: Iterable[str] | None = None,
    denied_dashboard_uids: Iterable[str] | None = None,
    allowed_environments: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Create a new user in the User & Role Store and return a summary.

    The account is created in the ``pending`` state with no role and no
    scopes, exactly as an unknown identity is auto-registered on first
    sign-in. When a role, a status other than ``pending`` or any scope is
    requested, the account is activated through
    :meth:`UserStore.update_user_access`, which audits the change and bumps
    ``authz_version``.
    """
    subject = validate_subject(subject)
    issuer = validate_issuer(issuer)
    username = _optional_text(username)
    display_name = _optional_text(display_name)
    email = validate_email(email)
    role = validate_role(role)
    status = validate_status(status)
    folders = validate_uid_list(allowed_folder_uids, "--allow-folder")
    dashboards = validate_uid_list(allowed_dashboard_uids, "--allow-dashboard")
    denied = validate_uid_list(denied_dashboard_uids, "--deny-dashboard")
    environments = validate_environments(allowed_environments)

    # A role means the account is activated (the Admin UI does the same when
    # an administrator edits a pending account).
    if role is not None and status == "pending":
        status = "active"

    grants_requested = bool(
        role is not None
        or status != "pending"
        or folders
        or dashboards
        or denied
        or environments
    )

    admin_user_id = _optional_text(admin_user_id)
    if grants_requested and not admin_user_id:
        raise UserInputError(
            "--admin is required to assign a role, a status or a scope: the "
            "change is audited against that administrator."
        )

    with get_db_session() as session:
        if grants_requested:
            admin = UserStore.get_user_by_id(session, admin_user_id)
            if admin is None:
                raise UserInputError(f"Administrator {admin_user_id} not found.")
            if admin.status != "active" or (admin.role or "").lower() != "admin":
                raise UserInputError(
                    f"{admin_user_id} is not an active administrator."
                )

        existing = UserStore.get_user_by_identity(session, issuer, subject)
        if existing is not None:
            raise DuplicateUserError(
                f"A user with issuer '{issuer}' and subject '{subject}' already "
                f"exists ({existing.user_id})."
            )

        user = UserStore.create_pending_user(
            session,
            issuer=issuer,
            subject=subject,
            email=email,
            username=username,
            display_name=display_name,
        )

        if grants_requested:
            scopes: Mapping[str, Any] = {
                "allowed_folder_uids": folders,
                "allowed_dashboard_uids": dashboards,
                "denied_dashboard_uids": denied,
                "allowed_environments": environments,
                # The role grant mirrors the primary role, so the account
                # behaves like one created through the Admin UI.
                "role_grants": [
                    {
                        "role": role or "observer",
                        "allowed_folder_uids": folders,
                        "allowed_dashboard_uids": dashboards,
                        "allowed_environments": environments,
                    }
                ],
            }
            try:
                user = UserStore.update_user_access(
                    session,
                    admin_user_id=admin_user_id,
                    target_user_id=user.user_id,
                    role=role,
                    status=status,
                    scopes=scopes,
                    admin_role="admin",
                )
            except AdminSecurityError as exc:
                raise UserInputError(str(exc)) from exc

        summary = {
            "user_id": user.user_id,
            "issuer": user.issuer,
            "subject": user.subject,
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "status": user.status,
            "role": user.role,
            "authz_version": user.authz_version,
            "scopes": {
                "allowed_folder_uids": list(user.scopes.allowed_folder_uids) if user.scopes else [],
                "allowed_dashboard_uids": list(user.scopes.allowed_dashboard_uids) if user.scopes else [],
                "denied_dashboard_uids": list(user.scopes.denied_dashboard_uids) if user.scopes else [],
                "allowed_environments": list(user.scopes.allowed_environments) if user.scopes else [],
                "role_grants": list(getattr(user.scopes, "role_grants", None) or []) if user.scopes else [],
            },
        }

    return summary


__all__ = [
    "DuplicateUserError",
    "UserInputError",
    "add_user",
    "validate_email",
    "validate_environments",
    "validate_issuer",
    "validate_role",
    "validate_status",
    "validate_subject",
    "validate_uid_list",
]
