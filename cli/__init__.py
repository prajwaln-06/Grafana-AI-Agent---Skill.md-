"""
cli/__init__.py

``agent-cli`` -- the command line client of the Grafana AI Agent.

Only one command is implemented: ``agent-cli admin users add``, which creates
a new account in the application's User & Role Store. The design and the
usage follow section 6 ("CLI architecture and command examples") of the RBAC
design document, "RBAC_Grafana_ADK_Final (1).md".
"""
from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (see ``pyproject.toml``)."""
    from cli.main import main as _main

    return _main(argv)
