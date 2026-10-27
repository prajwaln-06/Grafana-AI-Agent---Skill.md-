"""
app/catalog/reviews.py

The human-review store: explicit approve/reject decisions about runtime-
discovered (or vendor) metrics, persisted in their own small file
(`reviews.json`), separate from BOTH the vendor-derived catalog.json and any
runtime snapshot.

    vendor catalog.json   -- vendor-derived, regenerated offline, never edited here
    reviews.json          -- explicit human decisions (this module)
    runtime snapshot      -- last reconciled result held in memory (runtime.py)

Nothing in this module talks to Prometheus or an LLM. Metadata that only
Prometheus can supply (type, HELP) is passed in by the caller; metadata only a
human can supply (category, exporter, reference_path) is validated, never
inferred from the metric name.

---- File format (schema_version 1)

    {
      "schema_version": 1,
      "decisions": [
        {"name": "acme_queue_depth", "decision": "approved",
         "decided_at": "2026-10-27T05:00:00Z",
         "type": "gauge", "help": "...",
         "category": "Queues", "exporter": "node-exporter",
         "reference_path": "references/node-exporter/cpu.md", "note": "..."},
        {"name": "go_goroutines", "decision": "rejected",
         "decided_at": "2026-10-27T05:01:00Z", "type": "gauge"}
      ]
    }

Records are strict: unknown keys, wrong types, a duplicate name, an approval
missing required metadata, or a rejection carrying approval metadata all make
the whole file fail to load (ReviewStoreError). The store never "repairs" or
drops a record it does not understand -- silently discarding a human decision
would be worse than refusing to start refreshing.

---- Precedence (vendor entries V, review records R)

    R rejected            -> rejected, whether or not the metric is in V.
    in V, no R            -> vendor entry (reconciler decides availability).
    R approved, not in V  -> joins the approved universe from the record.
    R approved, in V      -> the vendor entry wins (the record is reported as
                             superseded, kept on disk, never applied): a
                             vendor entry is already approved, so the record
                             cannot make anything less restricted, and two
                             definitions of one metric are never merged.
    no V, no R            -> reconciler's discovered_pending_review path.

A rejection is therefore never undone by rediscovery, by a vendor catalog
reload, or by the metric appearing in the vendor catalog. The only ways to
lift it are an explicit `approve --replace` (non-vendor metrics) or `clear`.

---- Durability

save_reviews() writes a temporary file in the same directory, fsyncs it, and
os.replace()s it over the target, so an interrupted write leaves either the
old file or the new file, never a truncated one. Output is deterministic
(records sorted by name), and an operation that changes nothing does not
rewrite the file. Two review commands running at the same instant can still
lose one update (read-modify-write without a lock); the file itself cannot be
corrupted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from app.catalog.reconciler import UNKNOWN_EXPORTER, RuntimeMetricInfo
from app.catalog.schema import (
    UNCATEGORIZED,
    Catalog,
    CatalogEntry,
    CatalogStatus,
    MetricType,
    Priority,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
APPROVED = "approved"
REJECTED = "rejected"
_DECISIONS = {APPROVED, REJECTED}
_VALID_TYPES = {t.value for t in MetricType}
_METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_ALLOWED_KEYS = {
    "name", "decision", "decided_at", "type", "help",
    "category", "exporter", "reference_path", "note",
}
_APPROVAL_ONLY_KEYS = ("category", "exporter", "reference_path")


class ReviewError(ValueError):
    """Base class for every review-store failure a CLI should report."""


class ReviewRecordError(ReviewError):
    """One record is invalid on its own."""


class ReviewStoreError(ReviewError):
    """The reviews file is unreadable, malformed, or self-conflicting.
    Callers must fail closed: keep the previous snapshot, never fall back to
    "no decisions" for a file that exists but cannot be trusted."""


class ReviewOperationError(ReviewError):
    """A requested approve/reject/clear was refused (not a storage problem)."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_TIMESTAMP_FORMAT)


@dataclass(frozen=True)
class ReviewRecord:
    name: str
    decision: str
    decided_at: str
    type: str | None = None
    help: str = ""
    category: str | None = None
    exporter: str | None = None
    reference_path: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        name = self.name
        if not isinstance(name, str) or not _METRIC_NAME_RE.match(name):
            raise ReviewRecordError(
                f"Review record name {name!r} is not a valid Prometheus metric name."
            )
        if self.decision not in _DECISIONS:
            raise ReviewRecordError(
                f"Review record {name!r}: decision {self.decision!r} must be one of "
                f"{sorted(_DECISIONS)}."
            )
        try:
            datetime.strptime(self.decided_at, _TIMESTAMP_FORMAT)
        except (TypeError, ValueError) as e:
            raise ReviewRecordError(
                f"Review record {name!r}: decided_at {self.decided_at!r} must look "
                f"like 2026-01-31T23:59:59Z."
            ) from e
        if self.type is not None and self.type not in _VALID_TYPES:
            raise ReviewRecordError(
                f"Review record {name!r}: type {self.type!r} must be one of "
                f"{sorted(_VALID_TYPES)} (or omitted for a rejection)."
            )
        if self.decision == APPROVED:
            if self.type is None:
                raise ReviewRecordError(
                    f"Review record {name!r}: an approval requires the type Prometheus "
                    f"reported; it is never guessed."
                )
            for key in _APPROVAL_ONLY_KEYS:
                value = getattr(self, key)
                if not isinstance(value, str) or not value.strip():
                    raise ReviewRecordError(
                        f"Review record {name!r}: an approval requires a non-empty {key}."
                    )
            if self.category.strip().lower() == UNCATEGORIZED:
                raise ReviewRecordError(
                    f"Review record {name!r}: category {UNCATEGORIZED!r} is the 'nobody "
                    f"classified this' placeholder, not a valid approved category."
                )
            if self.exporter.strip().lower() == UNKNOWN_EXPORTER:
                raise ReviewRecordError(
                    f"Review record {name!r}: exporter {UNKNOWN_EXPORTER!r} is the "
                    f"'nobody said' placeholder, not a valid approved exporter."
                )
        else:
            extra = [k for k in _APPROVAL_ONLY_KEYS if getattr(self, k) is not None]
            if extra:
                raise ReviewRecordError(
                    f"Review record {name!r}: a rejection must not carry approval "
                    f"metadata ({', '.join(extra)}); that would make the decision ambiguous."
                )

    # ---- (de)serialization -------------------------------------------

    @classmethod
    def from_dict(cls, data: Any) -> "ReviewRecord":
        if not isinstance(data, dict):
            raise ReviewRecordError(f"Review record must be an object, got {type(data).__name__}.")
        unknown = sorted(set(data) - _ALLOWED_KEYS)
        if unknown:
            raise ReviewRecordError(
                f"Review record {data.get('name')!r} has unknown field(s) {unknown}; "
                f"allowed: {sorted(_ALLOWED_KEYS)}."
            )
        for required in ("name", "decision", "decided_at"):
            if required not in data:
                raise ReviewRecordError(f"Review record is missing required field {required!r}.")
        for key, value in data.items():
            if value is not None and not isinstance(value, str):
                raise ReviewRecordError(
                    f"Review record {data.get('name')!r}: field {key!r} must be a string."
                )
        return cls(
            name=data["name"],
            decision=data["decision"],
            decided_at=data["decided_at"],
            type=data.get("type"),
            help=data.get("help") or "",
            category=data.get("category"),
            exporter=data.get("exporter"),
            reference_path=data.get("reference_path"),
            note=data.get("note") or "",
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "decision": self.decision,
            "decided_at": self.decided_at,
        }
        for key in ("type", "help", "category", "exporter", "reference_path", "note"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out

    def same_decision_as(self, other: "ReviewRecord") -> bool:
        """Semantic equality for idempotence: ignores decided_at, note and
        HELP (informational snapshots). Two rejections are the same decision;
        two approvals are the same only if every field that affects query
        integration matches."""
        if self.decision != other.decision:
            return False
        if self.decision == REJECTED:
            return True
        return all(
            getattr(self, k) == getattr(other, k)
            for k in ("type", "category", "exporter", "reference_path")
        )


@dataclass(frozen=True)
class ReviewStore:
    records: tuple[ReviewRecord, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for record in self.records:
            if record.name in seen:
                raise ReviewStoreError(
                    f"Conflicting review records: {record.name!r} appears more than once. "
                    f"Each metric may have exactly one decision."
                )
            seen.add(record.name)

    def get(self, name: str) -> ReviewRecord | None:
        for record in self.records:
            if record.name == name:
                return record
        return None

    def by_decision(self, decision: str) -> tuple[ReviewRecord, ...]:
        return tuple(r for r in self.records if r.decision == decision)

    def rejections(self) -> dict[str, RuntimeMetricInfo]:
        """{name: stored discovery snapshot} for reconcile()'s `rejections`
        input. The type/HELP are only a fallback for building a rejected
        entry for a metric no vendor entry describes."""
        return {
            r.name: RuntimeMetricInfo(type=r.type, help=r.help or None)
            for r in self.by_decision(REJECTED)
        }

    def _with(self, records: list[ReviewRecord]) -> "ReviewStore":
        return ReviewStore(records=tuple(sorted(records, key=lambda r: r.name)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "decisions": [r.to_dict() for r in sorted(self.records, key=lambda r: r.name)],
        }


# ---- persistence ----------------------------------------------------------


def load_reviews(path: Path) -> ReviewStore:
    """Loads `path`. A MISSING file is an empty store (a deployment that has
    never reviewed anything). A file that exists but cannot be read, parsed,
    or validated raises ReviewStoreError -- it is never treated as empty."""
    if not path.exists():
        logger.info("No review store at %s; treating as no decisions yet.", path)
        return ReviewStore()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ReviewStoreError(f"Could not read review store {path}: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ReviewStoreError(f"Review store {path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ReviewStoreError(f"Review store {path} must contain a JSON object at the top level.")
    unknown = sorted(set(data) - {"schema_version", "decisions"})
    if unknown:
        raise ReviewStoreError(f"Review store {path} has unknown top-level field(s) {unknown}.")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ReviewStoreError(
            f"Review store {path} has schema_version {data.get('schema_version')!r}; "
            f"this code understands only {SCHEMA_VERSION}."
        )
    raw_decisions = data.get("decisions")
    if not isinstance(raw_decisions, list):
        raise ReviewStoreError(f"Review store {path}: 'decisions' must be a list.")
    try:
        records = [ReviewRecord.from_dict(d) for d in raw_decisions]
        return ReviewStore(records=tuple(sorted(records, key=lambda r: r.name)))
    except ReviewStoreError as e:
        raise ReviewStoreError(f"Review store {path}: {e}") from e
    except ReviewRecordError as e:
        raise ReviewStoreError(f"Review store {path} failed validation: {e}") from e


def save_reviews(store: ReviewStore, path: Path) -> None:
    """Atomic write: temp file in the same directory, fsync, os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(store.to_dict(), indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates 0600; the server may run as a different user than
        # whoever ran the CLI, and decisions are not secrets.
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:  # best effort: make the rename itself durable
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


# ---- operations (pure: they return a new store, the caller persists it) ---


@dataclass(frozen=True)
class ReviewOutcome:
    store: ReviewStore
    action: str  # "added" | "unchanged" | "replaced" | "removed"
    record: ReviewRecord | None


def validate_reference_path(skills_root: Path, exporter: str, reference_path: str) -> None:
    """A reference_path must point at a real DOMAIN reference of the stated
    exporter: references/<exporter>/<domain>.md, not an overview or
    fundamentals file, and the exporter must have an overview.md (that file is
    how the pipeline finds an exporter's metric universe)."""
    pure = PurePosixPath(reference_path)
    parts = pure.parts
    if pure.is_absolute() or ".." in parts or len(parts) != 3 or parts[0] != "references":
        raise ReviewOperationError(
            f"reference_path {reference_path!r} must look like "
            f"references/<exporter>/<domain>.md."
        )
    _, exporter_dir, filename = parts
    if not filename.endswith(".md") or filename == "overview.md" or filename.endswith("-fundamentals.md"):
        raise ReviewOperationError(
            f"reference_path {reference_path!r} must be a domain reference "
            f"(not overview.md or a *-fundamentals.md file)."
        )
    if exporter != exporter_dir:
        raise ReviewOperationError(
            f"exporter {exporter!r} does not match the directory of reference_path "
            f"{reference_path!r} ({exporter_dir!r}); state the same exporter in both."
        )
    if not (skills_root / reference_path).is_file():
        raise ReviewOperationError(
            f"reference_path {reference_path!r} does not exist under {skills_root}."
        )
    if not (skills_root / "references" / exporter_dir / "overview.md").is_file():
        raise ReviewOperationError(
            f"exporter {exporter!r} has no references/{exporter_dir}/overview.md under "
            f"{skills_root}, so the pipeline could not locate its metrics."
        )


def approve(
    store: ReviewStore,
    *,
    name: str,
    category: str,
    exporter: str,
    reference_path: str,
    discovered: RuntimeMetricInfo,
    vendor_names: set[str] | frozenset[str],
    skills_root: Path,
    note: str = "",
    replace: bool = False,
    now: str | None = None,
) -> ReviewOutcome:
    """Records an explicit approval. `discovered` supplies the Prometheus-
    reported type/HELP; category, exporter and reference_path are the human's
    statement and are validated, never derived from `name`."""
    if name in vendor_names:
        raise ReviewOperationError(
            f"{name!r} is defined by the vendor catalog, which is already approved; a review "
            f"approval would be a second definition. To exclude it use `reject`; to lift an "
            f"earlier rejection use `clear`."
        )
    if discovered.type not in _VALID_TYPES:
        raise ReviewOperationError(
            f"Prometheus did not report a valid type for {name!r} (got {discovered.type!r}); "
            f"it will not be guessed, so it cannot be approved."
        )
    validate_reference_path(skills_root, exporter, reference_path)
    record = ReviewRecord(
        name=name, decision=APPROVED, decided_at=now or _now(), type=discovered.type,
        help=discovered.help or "", category=category, exporter=exporter,
        reference_path=reference_path, note=note,
    )
    return _store_record(store, record, replace=replace)


def reject(
    store: ReviewStore,
    *,
    name: str,
    discovered: RuntimeMetricInfo | None,
    vendor_names: set[str] | frozenset[str],
    note: str = "",
    replace: bool = False,
    now: str | None = None,
) -> ReviewOutcome:
    """Records an explicit rejection. The metric must be something the system
    knows about: a vendor metric, or one currently discovered with a valid
    type -- a typo must not mint a phantom rejection."""
    snapshot_type = discovered.type if discovered and discovered.type in _VALID_TYPES else None
    if name not in vendor_names and snapshot_type is None:
        raise ReviewOperationError(
            f"{name!r} is neither a vendor metric nor currently discovered with a valid "
            f"type, so there is nothing to reject."
        )
    record = ReviewRecord(
        name=name, decision=REJECTED, decided_at=now or _now(), type=snapshot_type,
        help=(discovered.help or "") if discovered else "", note=note,
    )
    return _store_record(store, record, replace=replace)


def clear(store: ReviewStore, *, name: str) -> ReviewOutcome:
    """Removes the explicit decision for `name` (e.g. lifts a rejection)."""
    existing = store.get(name)
    if existing is None:
        raise ReviewOperationError(f"There is no review record for {name!r}.")
    remaining = [r for r in store.records if r.name != name]
    return ReviewOutcome(store=store._with(remaining), action="removed", record=existing)


def _store_record(store: ReviewStore, record: ReviewRecord, *, replace: bool) -> ReviewOutcome:
    existing = store.get(record.name)
    if existing is None:
        return ReviewOutcome(store._with([*store.records, record]), "added", record)
    if existing.same_decision_as(record):
        return ReviewOutcome(store, "unchanged", existing)
    if not replace:
        raise ReviewOperationError(
            f"{record.name!r} already has a conflicting {existing.decision} decision "
            f"({existing.decided_at}). Re-run with --replace to overwrite it deliberately, "
            f"or use `clear` to remove it."
        )
    others = [r for r in store.records if r.name != record.name]
    return ReviewOutcome(store._with([*others, record]), "replaced", record)


# ---- precedence: building the approved universe ---------------------------


def build_approved_universe(
    vendor_catalog: Catalog, store: ReviewStore
) -> tuple[Catalog, tuple[str, ...]]:
    """vendor catalog + approved review records -> the approved universe that
    reconcile() then checks against runtime. See the module docstring's
    precedence table. Returns (catalog, names of approval records superseded
    by a vendor entry). Rejections are NOT applied here -- they are passed to
    reconcile() as an explicit input so they also cover runtime-only metrics.
    """
    vendor_names = {e.name for e in vendor_catalog.metrics}
    added: list[CatalogEntry] = []
    superseded: list[str] = []
    for record in store.by_decision(APPROVED):
        if record.name in vendor_names:
            superseded.append(record.name)
            continue
        added.append(
            CatalogEntry(
                name=record.name,
                type=record.type,
                category=record.category,
                # rules.apply_rules() may refine REVIEW from the category.
                priority=Priority.REVIEW.value,
                exporter=record.exporter,
                status=CatalogStatus.APPROVED.value,
                help=record.help,
                reference_path=record.reference_path,
            )
        )
    universe = Catalog(
        catalog_version=vendor_catalog.catalog_version,
        generated_at=vendor_catalog.generated_at,
        metrics=(*vendor_catalog.metrics, *added),
    )
    return universe, tuple(sorted(superseded))
