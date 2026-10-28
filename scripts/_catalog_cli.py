"""
scripts/_catalog_cli.py

Small helpers shared by the catalog CLIs (scripts/reconcile_catalog.py,
scripts/review_catalog.py). Not a CLI itself.
"""

from __future__ import annotations

import os

from app.config import Settings, get_settings


def load_settings() -> Settings:
    """The process-wide Settings (env vars / .env, same as the server).

    Settings declares GEMINI_API_KEY as required because the server needs it;
    these CLIs never call an LLM, so a missing key must not stop an operator
    from running a reconciliation or a review. A placeholder is supplied only
    when the variable is unset -- the same `setdefault` tests/conftest.py
    uses -- and is never sent anywhere.
    """
    os.environ.setdefault("GEMINI_API_KEY", "unused-by-catalog-cli")
    return get_settings()


def format_names(names, limit: int = 25, show_all: bool = False) -> list[str]:
    """Indented, one-per-line rendering of a (possibly long) name list.
    Truncated to `limit` with an explicit "... and N more" marker unless
    `show_all` -- a Prometheus with hundreds of undetermined metrics must not
    bury the rest of the report."""
    ordered = sorted(names)
    shown = ordered if show_all else ordered[:limit]
    lines = [f"    {n}" for n in shown]
    if len(ordered) > len(shown):
        lines.append(f"    ... and {len(ordered) - len(shown)} more (use --all to list)")
    return lines
