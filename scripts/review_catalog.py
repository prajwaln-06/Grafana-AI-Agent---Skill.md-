"""
scripts/review_catalog.py

Human review of runtime-discovered metrics. Decisions are written to the
review store (Settings.catalog_reviews_path, default app/catalog/reviews.json)
-- never to catalog.json.

    python -m scripts.review_catalog list [--status pending|approved|rejected|all]
    python -m scripts.review_catalog approve NAME --category C --exporter E \\
            --reference-path references/E/domain.md [--note TEXT] [--replace]
    python -m scripts.review_catalog reject NAME [--note TEXT] [--replace]
    python -m scripts.review_catalog clear NAME

Common options: --reviews PATH, --catalog PATH, --prometheus-url URL,
--skills-root PATH (defaults come from Settings).

Rules enforced here (the logic lives in app/catalog/reviews.py):
  - `approve` needs category, exporter and reference_path from YOU; none is
    inferred from the metric name. The type and HELP come from what Prometheus
    reports right now, and a metric Prometheus gave no usable type for cannot
    be approved. The reference_path must be an existing domain reference of
    the stated exporter (references/<exporter>/<file>.md).
  - A metric already in the vendor catalog cannot be approved (it already is);
    it can be rejected, and a rejection can be lifted with `clear`.
  - Changing an existing decision needs --replace. Repeating an identical
    command changes nothing.
  - `list --status approved|rejected`, `reject` of a vendor metric, and
    `clear` need no Prometheus; the others do (discovery failure -> exit 1).

A running server picks decisions up on its next catalog refresh
(CATALOG_RUNTIME_REFRESH_SECONDS); nothing here signals it.

Exit codes: 0 ok / nothing to change, 1 discovery failed, 2 refused or
configuration/store error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from app.catalog import reviews as rv
from app.catalog.loader import CatalogLoadError
from app.catalog.reconciler import (
    DiscoveryScope,
    ReconciliationReport,
    ReconciliationSkipped,
    RuntimeMetricInfo,
)
from app.catalog.runtime import CatalogInputs, load_inputs, reconcile_inputs
from app.catalog.schema import CatalogStatus
from app.config import Settings
from scripts._catalog_cli import load_settings

EXIT_OK = 0
EXIT_DISCOVERY_FAILED = 1
EXIT_REFUSED = 2

_HELP_WIDTH = 70


def _short(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _HELP_WIDTH else text[: _HELP_WIDTH - 3] + "..."


def _why_not_in_report(name: str, report: ReconciliationReport) -> str:
    if name in report.out_of_scope_skipped:
        return (f"{name!r} is excluded by the configured discovery scope "
                f"(CATALOG_DISCOVERY_*_PATTERNS); adjust the scope if it should be reviewable.")
    if name in report.undetermined_type_skipped:
        return (f"Prometheus reported no usable single type for {name!r}; it will not be guessed, "
                f"so it cannot be reviewed until Prometheus reports one.")
    return f"{name!r} is not currently exposed by Prometheus."


def _discover(settings: Settings, inputs: CatalogInputs, prometheus_url: str | None):
    return reconcile_inputs(
        inputs,
        prometheus_url or settings.prometheus_url,
        settings.prometheus_timeout_seconds,
        scope=DiscoveryScope.from_settings(settings),
    )


def _persist(outcome: rv.ReviewOutcome, reviews_path: Path) -> None:
    if outcome.action != "unchanged":
        rv.save_reviews(outcome.store, reviews_path)


def _list(args, settings, inputs, emit) -> int:
    want = args.status
    if want in ("pending", "all"):
        try:
            report = _discover(settings, inputs, args.prometheus_url)
        except ReconciliationSkipped as e:
            emit(f"DISCOVERY FAILED: {e}")
            return EXIT_DISCOVERY_FAILED
        pending = report.catalog.by_status(CatalogStatus.DISCOVERED_PENDING_REVIEW.value)
        emit(f"Pending review: {len(pending)}")
        for entry in sorted(pending, key=lambda e: e.name):
            emit(f"  {entry.name} ({entry.type}) {_short(entry.help)}")
        if report.out_of_scope_skipped or report.undetermined_type_skipped:
            emit(f"  (not listed: {len(report.out_of_scope_skipped)} out of scope, "
                 f"{len(report.undetermined_type_skipped)} with no usable type)")
    for decision in (rv.APPROVED, rv.REJECTED):
        if want in (decision, "all"):
            records = inputs.store.by_decision(decision)
            emit(f"{decision.capitalize()} by review: {len(records)}")
            for r in records:
                extra = f" [{r.category} / {r.exporter} / {r.reference_path}]" if decision == rv.APPROVED else ""
                superseded = " (superseded by vendor entry)" if r.name in inputs.superseded else ""
                emit(f"  {r.name} ({r.type or 'n/a'}) {r.decided_at}{extra}{superseded}")
    return EXIT_OK


def _approve(args, settings, inputs, emit) -> int:
    name = args.name
    discovered = RuntimeMetricInfo()
    if name not in inputs.vendor_names:  # a vendor name is refused by rv.approve() before the type matters
        try:
            report = _discover(settings, inputs, args.prometheus_url)
        except ReconciliationSkipped as e:
            emit(f"DISCOVERY FAILED: {e}")
            return EXIT_DISCOVERY_FAILED
        entry = report.catalog.get(name)
        if entry is None:
            raise rv.ReviewOperationError(_why_not_in_report(name, report))
        if entry.status == CatalogStatus.REJECTED.value:
            # A rejected entry's type may be a placeholder; trust only what was stored.
            record = inputs.store.get(name)
            discovered = RuntimeMetricInfo(type=record.type if record else None,
                                           help=record.help if record else None)
        else:
            discovered = RuntimeMetricInfo(type=entry.type, help=entry.help)
    outcome = rv.approve(
        inputs.store, name=name, category=args.category, exporter=args.exporter,
        reference_path=args.reference_path, discovered=discovered,
        vendor_names=inputs.vendor_names, skills_root=args.skills_root or settings.skills_root,
        note=args.note, replace=args.replace,
    )
    _persist(outcome, args.reviews_path)
    emit(f"Approve {name}: {outcome.action}.")
    return EXIT_OK


def _reject(args, settings, inputs, emit) -> int:
    name = args.name
    discovered = None
    if name not in inputs.vendor_names:
        try:
            report = _discover(settings, inputs, args.prometheus_url)
        except ReconciliationSkipped as e:
            emit(f"DISCOVERY FAILED: {e}")
            return EXIT_DISCOVERY_FAILED
        entry = report.catalog.get(name)
        if entry is None:
            raise rv.ReviewOperationError(_why_not_in_report(name, report))
        discovered = RuntimeMetricInfo(type=entry.type, help=entry.help)
    outcome = rv.reject(
        inputs.store, name=name, discovered=discovered, vendor_names=inputs.vendor_names,
        note=args.note, replace=args.replace,
    )
    _persist(outcome, args.reviews_path)
    emit(f"Reject {name}: {outcome.action}.")
    return EXIT_OK


def _clear(args, settings, inputs, emit) -> int:
    outcome = rv.clear(inputs.store, name=args.name)
    _persist(outcome, args.reviews_path)
    emit(f"Clear {args.name}: removed the {outcome.record.decision} decision.")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.review_catalog",
        description="List, approve, reject or clear metric review decisions.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--reviews", type=Path, dest="reviews_path", help="Override Settings.catalog_reviews_path.")
    common.add_argument("--catalog", type=Path, dest="catalog_path", help="Override Settings.catalog_path.")
    common.add_argument("--prometheus-url", help="Override Settings.prometheus_url.")
    common.add_argument("--skills-root", type=Path, help="Override Settings.skills_root.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", parents=[common], help="List pending discoveries or stored decisions.")
    p_list.add_argument("--status", choices=["pending", "approved", "rejected", "all"], default="pending")
    p_list.set_defaults(handler=_list)

    p_ap = sub.add_parser("approve", parents=[common], help="Approve a pending discovery.")
    p_ap.add_argument("name")
    p_ap.add_argument("--category", required=True)
    p_ap.add_argument("--exporter", required=True)
    p_ap.add_argument("--reference-path", required=True)
    p_ap.add_argument("--note", default="")
    p_ap.add_argument("--replace", action="store_true", help="Overwrite an existing, different decision.")
    p_ap.set_defaults(handler=_approve)

    p_rj = sub.add_parser("reject", parents=[common], help="Reject a metric.")
    p_rj.add_argument("name")
    p_rj.add_argument("--note", default="")
    p_rj.add_argument("--replace", action="store_true", help="Overwrite an existing, different decision.")
    p_rj.set_defaults(handler=_reject)

    p_cl = sub.add_parser("clear", parents=[common], help="Remove a stored decision (e.g. lift a rejection).")
    p_cl.add_argument("name")
    p_cl.set_defaults(handler=_clear)
    return parser


def run(argv: list[str], settings: Settings, emit: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    args.reviews_path = args.reviews_path or settings.catalog_reviews_path
    try:
        inputs = load_inputs(args.catalog_path or settings.catalog_path, args.reviews_path)
        return args.handler(args, settings, inputs, emit)
    except (CatalogLoadError, rv.ReviewError, ValueError, OSError) as e:
        emit(f"REFUSED: {e}")
        return EXIT_REFUSED


def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- thin wrapper
    return run(sys.argv[1:] if argv is None else argv, load_settings())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
