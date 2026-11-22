"""
cli/main.py

``agent-cli`` -- the command line client of the Grafana AI Agent.

One command group, one command:

    agent-cli admin users add --subject <subject> [options]

It creates a new account in the application's User & Role Store. The design
and the flag names follow section 6 ("CLI architecture and command examples")
of "RBAC_Grafana_ADK_Final (1).md"; the account lifecycle follows section 5.2
(user lifecycle) and section 5.3 (administrator operations).

The command is a *host-side management command* (section 5.2): it writes to
the same database the service uses and applies the application's own
user-creation and user-management logic, so the audit log, the
``authz_version`` counter and the administrative guardrails are the ones the
service enforces.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# The ``app`` package lives next to this one; make it importable no matter
# which directory the command is started from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.auth.policy import VALID_ROLES, VALID_STATUSES  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DUPLICATE = 3

_EPILOG = """\
example:
  # create a pending account (no role, no scopes)
  agent-cli admin users add --subject ak_sub_new_hire --username morgan.lee \\
      --name "Morgan Lee (New Hire)" --email newhire@example.com

  # create an active maintainer with a folder scope, audited against an administrator
  agent-cli admin users add --subject ak_sub_maintainer_01 --username alex.rivera \\
      --name "Alex Rivera (Maintainer)" --email maintainer@example.com \\
      --admin usr_admin_01 --role maintainer --allow-folder fld-payments \\
      --allow-dashboard dash-checkout-slo --allow-env staging

scope flags (one value per flag, repeatable):
  --allow-folder     Grafana folder UID the account may use
  --allow-dashboard  Grafana dashboard UID the account may use
  --deny-dashboard   Grafana dashboard UID the account may never use
  --allow-env        environment name the account may query
"""


def build_parser() -> argparse.ArgumentParser:
    """Build the ``agent-cli`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="agent-cli",
        description=(
            "Command line client of the Grafana AI Agent. Creates accounts in "
            "the application's User & Role Store."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    admin = parser.add_subparsers(dest="group", metavar="<group>")
    admin_parser = admin.add_parser(
        "admin",
        help="Administrative commands.",
        description="Administrative commands (administrators only).",
    )
    users = admin_parser.add_subparsers(
        dest="group_command",
        metavar="<command>",
    )
    users_group = users.add_parser(
        "users",
        help="Manage the accounts of the role store.",
        description="Manage the accounts of the role store.",
    )
    users_parser = users_group.add_subparsers(
        dest="command",
        metavar="<command>",
    )
    add_parser = users_parser.add_parser(
        "add",
        help="Add a new account to the role store.",
        description=(
            "Add a new account to the role store. The account is identified by "
            "its Authentik subject (and, optionally, its issuer); the name and "
            "the address are profile fields and never identify anybody."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_parser.add_argument(
        "--subject",
        required=True,
        help="The Authentik subject of the account.",
    )
    add_parser.add_argument(
        "--issuer",
        default=None,
        help="The Authentik issuer (default: the configured one).",
    )
    add_parser.add_argument(
        "--username",
        default=None,
        help="Login name of the account.",
    )
    add_parser.add_argument(
        "--name",
        dest="display_name",
        default=None,
        help="Display name of the account.",
    )
    add_parser.add_argument(
        "--email",
        default=None,
        help="Email address of the account (informational only).",
    )
    add_parser.add_argument(
        "--admin",
        dest="admin_user_id",
        default=None,
        help=(
            "The administrator the role, status or scope change is audited "
            "against. Required whenever a role, a status other than 'pending' "
            "or a scope is assigned."
        ),
    )
    add_parser.add_argument(
        "--role",
        choices=sorted(VALID_ROLES),
        default=None,
        help="Application role of the account (assigning a role activates it).",
    )
    add_parser.add_argument(
        "--status",
        choices=sorted(VALID_STATUSES),
        default=None,
        help="Account status (default: pending).",
    )
    add_parser.add_argument(
        "--allow-folder",
        dest="allowed_folder_uids",
        action="append",
        default=[],
        metavar="UID",
        help="Grafana folder UID the account may use. Repeatable.",
    )
    add_parser.add_argument(
        "--allow-dashboard",
        dest="allowed_dashboard_uids",
        action="append",
        default=[],
        metavar="UID",
        help="Grafana dashboard UID the account may use. Repeatable.",
    )
    add_parser.add_argument(
        "--deny-dashboard",
        dest="denied_dashboard_uids",
        action="append",
        default=[],
        metavar="UID",
        help="Grafana dashboard UID the account may never use. Repeatable.",
    )
    add_parser.add_argument(
        "--allow-env",
        dest="allowed_environments",
        action="append",
        default=[],
        metavar="ENV",
        help="Environment the account may query. Repeatable.",
    )
    add_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the created account as JSON.",
    )

    return parser


def _run_add(args: argparse.Namespace) -> int:
    from cli.users import DuplicateUserError, UserInputError, add_user

    try:
        summary = add_user(
            subject=args.subject,
            issuer=args.issuer,
            username=args.username,
            display_name=args.display_name,
            email=args.email,
            role=args.role,
            status=args.status,
            admin_user_id=args.admin_user_id,
            allowed_folder_uids=args.allowed_folder_uids,
            allowed_dashboard_uids=args.allowed_dashboard_uids,
            denied_dashboard_uids=args.denied_dashboard_uids,
            allowed_environments=args.allowed_environments,
        )
    except DuplicateUserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DUPLICATE
    except UserInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return EXIT_OK

    scopes = summary["scopes"]
    print(f"[200 OK] Created user {summary['user_id']} status={summary['status']} "
          f"role={summary['role'] or 'none'} authz_version={summary['authz_version']}")
    print(f"        Folders:       {', '.join(scopes['allowed_folder_uids']) or '-'}")
    print(f"        Dashboards:    {', '.join(scopes['allowed_dashboard_uids']) or '-'}")
    print(f"        Denied:        {', '.join(scopes['denied_dashboard_uids']) or '-'}")
    print(f"        Environments:  {', '.join(scopes['allowed_environments']) or '-'}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``agent-cli`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if getattr(args, "group", None) != "admin":
        parser.print_help()
        return EXIT_ERROR

    if getattr(args, "group_command", None) != "users":
        print("error: 'agent-cli admin' has no default command; "
              "use 'agent-cli admin users add --help'.", file=sys.stderr)
        return EXIT_ERROR

    if getattr(args, "command", None) != "add":
        print("error: 'agent-cli admin users' has no default command; "
              "use 'agent-cli admin users add --help'.", file=sys.stderr)
        return EXIT_ERROR

    return _run_add(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
