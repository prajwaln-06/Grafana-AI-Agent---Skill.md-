"""
app/catalog/runtime.py

Glue between the three catalog inputs and the effective catalog:

    vendor catalog.json + review decisions (reviews.json) + live Prometheus
                                  |
                              reconciler
                                  |
                          effective catalog

Part 1 (this section): loading the two on-disk inputs and reconciling them
against discovery -- shared by the CLIs and the runtime refresh so they cannot
drift apart. No state lives here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.catalog.loader import load_catalog
from app.catalog.reconciler import (
    DiscoveryScope,
    ReconciliationReport,
    run_reconciliation,
)
from app.catalog.reviews import ReviewStore, build_approved_universe, load_reviews
from app.catalog.schema import Catalog

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogInputs:
    """The two on-disk inputs, loaded and validated together.

    `universe` is the approved metric universe (vendor entries + approved
    review records, vendor winning on a duplicate name); `superseded` lists
    approval records a vendor entry overrode (kept on disk, never applied).
    """

    vendor_catalog: Catalog
    store: ReviewStore
    universe: Catalog
    superseded: tuple[str, ...]

    @property
    def vendor_names(self) -> frozenset[str]:
        return frozenset(e.name for e in self.vendor_catalog.metrics)


def load_inputs(catalog_path: Path, reviews_path: Path) -> CatalogInputs:
    """Raises CatalogLoadError (bad vendor catalog) or ReviewError (bad review
    store) -- never returns a partial result."""
    vendor = load_catalog(catalog_path)
    store = load_reviews(reviews_path)
    universe, superseded = build_approved_universe(vendor, store)
    if superseded:
        logger.warning(
            "Approval record(s) %s are superseded by vendor catalog entries of the "
            "same name; the vendor definition is used. The records remain in the "
            "review store and can be removed with `review_catalog clear`.",
            list(superseded),
        )
    return CatalogInputs(vendor, store, universe, superseded)


def reconcile_inputs(
    inputs: CatalogInputs,
    base_url: str,
    timeout: float,
    *,
    scope: DiscoveryScope | None = None,
    previous_catalog: Catalog | None = None,
) -> ReconciliationReport:
    """Live discovery against `base_url`, reconciled with the approved
    universe and the store's rejections. Raises ReconciliationSkipped if
    discovery fails (see reconciler.run_reconciliation)."""
    return run_reconciliation(
        inputs.universe,
        base_url,
        timeout=timeout,
        previous_catalog=previous_catalog,
        discovery_scope=scope,
        rejections=inputs.store.rejections(),
    )
