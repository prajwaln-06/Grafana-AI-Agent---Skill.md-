"""
app/catalog/generator.py

Generates catalog.json from the skill package's authoritative per-metric
definitions. Exporter overview.md files provide navigation and domain context;
the catalog is the metric-universe source of truth.

This module is intentionally generic over exporter/domain names, mirroring
app/skill_index.py's own "the file structure is the source of truth, this
module is a thin generic reader of it" convention: nothing here hardcodes
"node-exporter", "dcgm-exporter", or any metric name. A new exporter
directory that follows the same references/<exporter>/overview.md +
references/<exporter>/<domain>.md convention (see EXTENDING.md) is picked
up automatically the next time this module runs, with zero code change.

What this module produces per metric, and where each field comes from:

  name            -- domain file's `### `metric_name`` heading (verbatim).
  type            -- domain file's per-metric "- **Type:** `X`" bullet,
                      lowercased to match schema.MetricType's enum values.
                      Source-derived.
  category        -- domain file's per-metric "- **Category:** X" bullet
                      (verbatim) -- NOT the Metric Directory's broader
                      `Domain` column (cpu/memory/...), which is coarser
                      than the per-metric Category already documented at
                      Level 2. Source-derived, not inferred.
  help            -- domain file's per-metric "- **Purpose:** X" bullet
                      (verbatim; continuation lines joined with a single
                      space). Mirrors a Prometheus HELP string in spirit --
                      it IS the one-line description this project already
                      maintains -- but is not literally scraped from a
                      running Prometheus /metrics endpoint. Source-derived.
  unit            -- domain file's per-metric "- **Unit:** X" bullet
                      (verbatim, continuation lines joined), INCLUDING any
                      caveat text the domain file itself uses (e.g.
                      "Not stated in the authoritative document...",
                      "...exact exposed unit should be verified..."). This
                      module does not attempt to classify that prose as
                      "no real unit" and collapse it to null -- doing so
                      would require a judgment call this module has no
                      reliable, deterministic way to make. See the Phase 2
                      batch report for the specific metrics this affects
                      (node_load1/5/15's unstated unit and
                      DCGM_FI_DEV_POWER_VIOLATION's unverified-unit
                      caveat). Source-derived (verbatim), never shortened.
  exporter        -- the exporter directory's own name (e.g.
                      "node-exporter"), taken from the path, never
                      hardcoded. Source-derived.
  reference_path  -- "references/<exporter>/<domain_file>", the domain file
                     containing the metric definition.
  status          -- always "approved" in Phase 2. This catalog is built
                      from the vendor/reference-universe side of the
                      frozen hybrid model ("Vendor/reference universe +
                      Prometheus runtime discovery -> Catalog"); the
                      runtime-discovery side of that model, which could
                      demote an entry to "approved_unavailable" or
                      surface a "discovered_pending_review" entry, is
                      explicitly Phase 4 (reconciler.py) work -- never
                      performed here. Generated (fixed default), not
                      inferred per-metric.
  priority        -- always "Review" in Phase 2. Priority classification
                      is explicitly Phase 5 (rules.py) work per the
                      five-batch plan; Phase 2 must not invent a
                      High/Medium guess to fill the field, so it uses the
                      schema's own explicit "not yet classified" value
                      instead. Generated (fixed default).
  keywords        -- always () (empty) in Phase 2, for the same reason as
                      priority: keyword generation is explicitly Phase 5
                      work. Populating this now from e.g. Purpose-text
                      tokenization would be inventing Phase 5's algorithm
                      early and un-reviewed. Generated (fixed default).
  dimensions      -- always () (empty) in Phase 2. schema.py defines
                      `dimensions` as a small, curated list of *semantic*
                      retrieval dimensions (e.g. "cpu", "mode") -- not raw
                      label keys, and not something this phase's mandate
                      (generate the 43-metric catalog, no retrieval logic
                      yet) covers. Left for a later phase to populate
                      deliberately, not silently defaulted-into by this
                      module based on documentation-page prose. Generated
                      (fixed default).

Generation is deterministic and idempotent: given the same skills_root
content, generate_catalog() always returns the same `metrics` tuple, in
the same order (exporter directories sorted lexicographically by
directory name -- currently "dcgm-exporter" before "node-exporter" --
then Metric Directory table order within each exporter) -- excluding
`generated_at`, which is a timestamp and is the one field this module
does not claim to reproduce byte-for-byte between runs unless the caller
pins it explicitly (tests do this to assert the rest of the document
round-trips identically).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.catalog.schema import (
    Catalog,
    CatalogEntry,
    CatalogStatus,
    Priority,
)

CATALOG_VERSION = "1.0"

# Metric Directory row inside an exporter overview.md, e.g.:
# | cpu | CPU time spent in different modes | `node_cpu_seconds_total` | `cpu.md` |
# Mirrors app/skill_index.py's own METRIC_DIR_ROW_RE, but captures the
# `domain`/`intent` columns too (generator.py needs the row's full shape,
# not just metric -> file) and is duplicated rather than imported so this
# module has no import-time coupling to skill_index.py's own load/validate
# path -- generator.py must be runnable standalone against a skills_root,
# the same way skill_index.py's own parse_routing_table() is usable
# standalone (see that module's docstring).
METRIC_DIR_ROW_RE = re.compile(
    r"^\|\s*(?P<domain>[^|]+?)\s*\|\s*(?P<intent>[^|]+?)\s*\|\s*"
    r"`(?P<metric>[^`]+)`\s*\|\s*`?(?P<domain_file>[A-Za-z0-9_./-]+\.md)`?\s*\|\s*$"
)

# One "- **Field Name:** value" bullet at the top of a "### `metric_name`"
# block, e.g. "- **Type:** `Counter`" or "- **Purpose:** Measures CPU
# time...". Deliberately narrow (letters/spaces/slashes only in the field
# name) -- this module only needs Category/Purpose/Type/Unit, and a bullet
# whose field name doesn't match this shape (e.g. "Metric-specific
# query/result semantics", which contains a hyphen) simply falls through
# to the "unrelated bullet" branch below, which is the correct behavior:
# stop treating following lines as a continuation of the previous field,
# without needing to model every field name this skill package uses.
BULLET_FIELD_RE = re.compile(r"^-\s+\*\*([A-Za-z /]+?):\*\*\s*(.*)$")

METRIC_HEADER_RE = re.compile(r"^###\s+`([^`]+)`\s*$")

REQUIRED_BULLET_FIELDS = ("Category", "Purpose", "Type")


class CatalogGenerationError(Exception):
    """Raised when the skill package's own Markdown doesn't contain what
    generator.py needs to build a catalog entry -- a Metric Directory row
    with no matching domain-file definition, a domain-file metric
    definition missing one of the required bullets (Category/Purpose/
    Type), or a Metric Directory table this module cannot parse at all.
    This is a skill-package authoring problem, not a bug in a user's
    question -- fail loudly at generation time, the same "structural
    problems fail loudly" convention app/skill_index.py's own
    SkillIndexError already establishes, rather than silently emitting an
    incomplete or wrong catalog entry.
    """


@dataclass(frozen=True)
class _MetricDirRow:
    domain: str
    intent: str
    metric: str
    domain_file: str  # e.g. "cpu.md", relative to the overview's own directory


def _find_exporter_overviews(references_dir: Path) -> list[Path]:
    """Every references/<exporter>/overview.md, sorted by exporter
    directory name so generation order (and therefore catalog.json's
    `metrics` array order) is stable across runs and across machines.
    Mirrors app/skill_index.py's own directory-convention approach
    (overview_path_for) rather than hardcoding exporter names."""
    return sorted(references_dir.glob("*/overview.md"))


def _parse_metric_directory(overview_path: Path) -> list[_MetricDirRow]:
    """Parses one exporter overview.md's Metric Directory table, in table
    order. Mirrors app/skill_index.py::SkillIndex.metric_directory()'s
    parsing approach, but preserves row order and the `domain`/`intent`
    columns too (that lookup-only helper only needs metric -> file), and
    fails loudly instead of silently skipping a row it cannot parse --
    generator.py's job is to produce a complete, correct catalog, not a
    best-effort partial one.
    """
    rows: list[_MetricDirRow] = []
    in_table = False
    text = overview_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("| Domain") and "Metric" in stripped:
            in_table = True
            continue
        if in_table:
            if not stripped.startswith("|"):
                in_table = False
                continue
            if stripped.startswith("|---"):
                continue
            m = METRIC_DIR_ROW_RE.match(stripped)
            if not m:
                raise CatalogGenerationError(
                    f"{overview_path}: could not parse Metric Directory row: "
                    f"{stripped!r}"
                )
            rows.append(
                _MetricDirRow(
                    domain=m.group("domain"),
                    intent=m.group("intent"),
                    metric=m.group("metric"),
                    domain_file=m.group("domain_file"),
                )
            )
    if not rows:
        raise CatalogGenerationError(
            f"{overview_path}: no Metric Directory rows found. Expected a "
            f"'| Domain | Intent / Measurement | Metric | Detail File |' "
            f"table."
        )
    return rows


def _parse_domain_metric_rows(exporter_dir: Path) -> list[_MetricDirRow]:
    """Builds metric rows directly from per-metric domain definitions.

    This is the Phase 16 path: overview.md is retained for domain-level
    navigation, but its former duplicate metric table is no longer required.
    """
    rows: list[_MetricDirRow] = []
    for domain_path in sorted(exporter_dir.glob("*.md")):
        if domain_path.name == "overview.md":
            continue
        text = domain_path.read_text(encoding="utf-8")
        metric_names = [m.group(1) for m in map(METRIC_HEADER_RE.match, text.splitlines()) if m]
        for metric_name in metric_names:
            rows.append(
                _MetricDirRow(
                    domain=domain_path.stem,
                    intent="",
                    metric=metric_name,
                    domain_file=domain_path.name,
                )
            )
    if not rows:
        raise CatalogGenerationError(
            f"{exporter_dir}: no metric definitions found in domain Markdown files."
        )
    return rows


def _parse_metric_definition_bullets(
    domain_path: Path, metric_name: str
) -> dict[str, str]:
    """Parses the "- **Field:** value" bullets directly under a domain
    file's "### `metric_name`" header, stopping at the next "### " header
    or "## " section boundary. A bullet's value may wrap across multiple
    source lines (e.g. node_load1's Unit bullet, or
    node_cpu_seconds_total's Purpose bullet); continuation lines --
    indented text that is not itself a new "- **Field:**" bullet and is
    not blank -- are joined onto the previous field's value with a single
    space, so the round-tripped string reads as one coherent sentence
    rather than being truncated at the first source line break. A blank
    line, or any other kind of bullet, ends the current field's
    continuation (matches this skill package's own authoring convention,
    verified by inspection of every domain file this module reads).
    """
    text = domain_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    header_idx = None
    for i, line in enumerate(lines):
        m = METRIC_HEADER_RE.match(line.strip())
        if m and m.group(1) == metric_name:
            header_idx = i
            break
    if header_idx is None:
        raise CatalogGenerationError(
            f"{domain_path}: no '### `{metric_name}`' section found."
        )

    end = len(lines)
    for i in range(header_idx + 1, len(lines)):
        s = lines[i].strip()
        if s.startswith("### ") or s.startswith("## "):
            end = i
            break

    fields: dict[str, str] = {}
    current_field: str | None = None
    for line in lines[header_idx + 1 : end]:
        stripped = line.strip()
        if not stripped:
            current_field = None
            continue
        m = BULLET_FIELD_RE.match(stripped)
        if m:
            field_name = m.group(1).strip()
            value = m.group(2).strip()
            fields[field_name] = value
            current_field = field_name
            continue
        if stripped.startswith("- "):
            # A different/unmodeled kind of bullet -- stop treating
            # subsequent lines as a continuation of the previously seen
            # field, but do not treat this as an error: this module only
            # needs Category/Purpose/Type/Unit, not every bullet a domain
            # file happens to use.
            current_field = None
            continue
        if current_field is not None:
            fields[current_field] = f"{fields[current_field]} {stripped}".strip()
    return fields


def _build_entry(
    exporter: str, row: _MetricDirRow, references_dir: Path
) -> CatalogEntry:
    domain_path = references_dir / exporter / row.domain_file
    if not domain_path.exists():
        raise CatalogGenerationError(
            f"references/{exporter}/overview.md: Metric Directory points at "
            f"{row.domain_file!r} for metric {row.metric!r}, but that file "
            f"does not exist at {domain_path}."
        )
    bullets = _parse_metric_definition_bullets(domain_path, row.metric)

    missing = [f for f in REQUIRED_BULLET_FIELDS if f not in bullets]
    if missing:
        raise CatalogGenerationError(
            f"{domain_path}: metric `{row.metric}` definition is missing "
            f"required field(s) {missing} -- cannot generate a catalog "
            f"entry without them."
        )

    raw_type = bullets["Type"].strip("`").strip().lower()

    return CatalogEntry(
        name=row.metric,
        type=raw_type,
        category=bullets["Category"],
        priority=Priority.REVIEW.value,
        exporter=exporter,
        status=CatalogStatus.APPROVED.value,
        help=bullets["Purpose"],
        unit=bullets.get("Unit"),
        keywords=(),
        reference_path=f"references/{exporter}/{row.domain_file}",
        dimensions=(),
    )


def generate_catalog(skills_root: Path, generated_at: str | None = None) -> Catalog:
    """Generates a Catalog from skills_root's exporter overview.md Metric
    Directory tables and their domain files' per-metric definitions.

    Deterministic and idempotent for a fixed `generated_at`: the same
    skills_root content always produces the same `metrics` tuple, in the
    same order (exporter directories sorted, then Metric Directory table
    order within each exporter). If `generated_at` is omitted, the current
    UTC time is used, which is naturally the one non-reproducible field
    between two separate calls -- pass a fixed value (as the test suite
    does) to assert the rest of the document is byte-for-byte identical.

    Raises CatalogGenerationError (a skill-package authoring problem this
    module cannot resolve on its own) or CatalogSchemaError (schema.py
    rejects a value this module produced -- e.g. an undocumented Type
    value) rather than emitting a partially correct catalog.
    """
    references_dir = skills_root / "references"
    if not references_dir.exists():
        raise CatalogGenerationError(f"{references_dir} does not exist.")

    entries: list[CatalogEntry] = []
    for overview_path in _find_exporter_overviews(references_dir):
        exporter = overview_path.parent.name
        text = overview_path.read_text(encoding="utf-8")
        if "## Metric Directory" in text:
            rows = _parse_metric_directory(overview_path)
        else:
            rows = _parse_domain_metric_rows(overview_path.parent)
        for row in rows:
            entries.append(_build_entry(exporter, row, references_dir))

    if generated_at is None:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return Catalog(
        catalog_version=CATALOG_VERSION,
        generated_at=generated_at,
        metrics=tuple(entries),
    )


def write_catalog(catalog: Catalog, output_path: Path) -> None:
    """Writes a Catalog to disk as pretty-printed JSON. Kept separate from
    generate_catalog() so tests can call generate_catalog() without
    touching disk, and so a CLI entry point can generate-then-validate-
    then-write (Phase 3) without a second code path re-deriving the same
    document.
    """
    output_path.write_text(
        json.dumps(catalog.to_dict(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:  # pragma: no cover -- thin CLI wrapper, exercised via
    # generate_catalog()/write_catalog() unit tests, not as a subprocess.
    """CLI entry point: `python -m app.catalog.generator [skills_root] [output_path]`.
    Defaults to skills_root="skills", output_path="app/catalog/catalog.json"
    -- the same default skills_root app/config.py's own Settings.skills_root
    uses, so running this with no arguments matches the running
    application's own configuration.
    """
    import sys

    skills_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("skills")
    output_path = (
        Path(sys.argv[2]) if len(sys.argv) > 2 else Path("app/catalog/catalog.json")
    )
    catalog = generate_catalog(skills_root)
    write_catalog(catalog, output_path)
    print(
        f"Generated {len(catalog.metrics)} catalog entries "
        f"(catalog_version={catalog.catalog_version}) -> {output_path}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
