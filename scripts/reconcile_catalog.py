"""
scripts/reconcile_catalog.py

Offline reconciliation report -- runs the existing reconciler against a live
Prometheus, independently of the query-serving path:

    python -m scripts.reconcile_catalog [--prometheus-url URL] [--catalog PATH] [--all]

What it does: loads the vendor-derived catalog (Settings.catalog_path) and the
review store (Settings.catalog_reviews_path), runs the reconciler against
Settings.prometheus_url (Settings.prometheus_timeout_seconds) with the approved
universe and the persisted rejections applied, and prints what it found.

What it deliberately does NOT do: write anything. It never touches
catalog.json or reviews.json (or any other file), so it is safe to run as often
as you like.

Exit codes:
    0  reconciliation ran (including "Prometheus reachable but empty" -- that
       is reported explicitly, it is not an error)
    1  discovery FAILED (Prometheus unreachable / bad response): nothing was
       reconciled, no conclusions about availability are drawn
    2  configuration problem (vendor catalog or review store unreadable /
       invalid, bad scope pattern)

Availability is reported as "currently available / unavailable", not as
transitions: a one-shot script has no previous run to compare against. Real
transitions are tracked by the in-process runtime refresh (app/catalog/
runtime.py) between its successive snapshots.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from app.catalog.loader import CatalogLoadError
from app.catalog.reconciler import (
    DiscoveryScope,
    ReconciliationReport,
    ReconciliationSkipped,
)
from app.catalog.reviews import APPROVED, REJECTED, ReviewError
from app.catalog.runtime import CatalogInputs, load_inputs, reconcile_inputs
from app.catalog.schema import CatalogStatus
from app.config import Settings
from scripts._catalog_cli import format_names, load_settings

EXIT_OK = 0
EXIT_DISCOVERY_FAILED = 1
EXIT_CONFIG_ERROR = 2


def format_report(
    report: ReconciliationReport,
    *,
    base_url: str,
    inputs: CatalogInputs,
    show_all: bool = False,
) -> str:
    catalog = report.catalog
    approved = [m.name for m in catalog.by_status(CatalogStatus.APPROVED.value)]
    unavailable = [m.name for m in catalog.by_status(CatalogStatus.APPROVED_UNAVAILABLE.value)]
    pending = [m.name for m in catalog.by_status(CatalogStatus.DISCOVERED_PENDING_REVIEW.value)]

    lines = [
        f"Reconciliation report -- Prometheus: {base_url}",
        f"Vendor catalog: {len(inputs.vendor_catalog.metrics)} entries "
        f"(version {inputs.vendor_catalog.catalog_version})",
        f"Review decisions: {len(inputs.store.by_decision(APPROVED))} approved, "
        f"{len(inputs.store.by_decision(REJECTED))} rejected",
        f"Discovery: succeeded, {report.runtime_name_count} metric names at runtime",
    ]
    if report.runtime_name_count == 0:
        lines.append(
            "  NOTE: discovery SUCCEEDED but returned zero metric names. This is "
            "reported as 'nothing is exposed', not as a failure -- check that "
            "Prometheus has finished its first scrapes."
        )

    if inputs.superseded:
        lines.append(
            f"WARNING: approval record(s) superseded by a vendor entry of the same name "
            f"(vendor definition used): {', '.join(inputs.superseded)}"
        )

    lines.append(f"Approved metrics available at runtime: {len(approved)}")
    lines.append(f"Approved metrics currently unavailable: {len(unavailable)}")
    lines.extend(format_names(unavailable, show_all=show_all))

    lines.append(f"New discoveries pending review: {len(pending)}")
    lines.extend(
        f"    {name} ({catalog.get(name).type})" for name in sorted(pending)[: None if show_all else 25]
    )
    if len(pending) > 25 and not show_all:
        lines.append(f"    ... and {len(pending) - 25} more (use --all to list)")

    lines.append(f"Preserved rejections: {len(report.kept_rejected)}")
    lines.extend(format_names(report.kept_rejected, show_all=show_all))

    lines.append(
        f"Type undetermined (skipped, never guessed): {len(report.undetermined_type_skipped)}"
    )
    lines.extend(format_names(report.undetermined_type_skipped, show_all=show_all))

    lines.append(f"Out of configured discovery scope: {len(report.out_of_scope_skipped)}")
    return "\n".join(lines)


def run(
    settings: Settings,
    *,
    prometheus_url: str | None = None,
    catalog_path: Path | None = None,
    reviews_path: Path | None = None,
    show_all: bool = False,
    emit: Callable[[str], None] = print,
) -> int:
    base_url = prometheus_url or settings.prometheus_url
    try:
        inputs = load_inputs(
            catalog_path or settings.catalog_path,
            reviews_path or settings.catalog_reviews_path,
        )
        scope = DiscoveryScope.from_settings(settings)
    except (CatalogLoadError, ReviewError, ValueError) as e:
        emit(f"CONFIGURATION ERROR: {e}")
        return EXIT_CONFIG_ERROR

    try:
        report = reconcile_inputs(
            inputs, base_url, settings.prometheus_timeout_seconds, scope=scope
        )
    except ReconciliationSkipped as e:
        emit(f"DISCOVERY FAILED: {e}")
        emit("No reconciliation was performed; this says nothing about which metrics exist.")
        return EXIT_DISCOVERY_FAILED

    emit(format_report(report, base_url=base_url, inputs=inputs, show_all=show_all))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- thin wrapper
    parser = argparse.ArgumentParser(
        prog="python -m scripts.reconcile_catalog",
        description="Run catalog reconciliation against Prometheus and print a report (read-only).",
    )
    parser.add_argument("--prometheus-url", help="Override Settings.prometheus_url.")
    parser.add_argument("--catalog", type=Path, help="Override Settings.catalog_path.")
    parser.add_argument("--reviews", type=Path, help="Override Settings.catalog_reviews_path.")
    parser.add_argument("--all", action="store_true", dest="show_all", help="List every name, untruncated.")
    args = parser.parse_args(argv)
    return run(
        load_settings(),
        prometheus_url=args.prometheus_url,
        catalog_path=args.catalog,
        reviews_path=args.reviews,
        show_all=args.show_all,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
