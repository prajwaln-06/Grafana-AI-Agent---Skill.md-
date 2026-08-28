"""
skill_index.py

Everything the pipeline needs to know about the skill package's structure,
derived by PARSING SKILL.md itself -- never by hardcoding exporter names,
metric lists, or file paths in this module. This is the direct replacement
for the old registry.py, which used to scan reference-file frontmatter to
build a dynamic exporter registry; the new skill architecture moved routing
into a single static table (SKILL.md Section 4), so this module's job is
narrower and more robust: parse that table (and a few other sections) at
startup, and expose small, generic lookups the pipeline calls at runtime.

Nothing in this file encodes "node-exporter" or "dcgm-exporter" or any
specific metric/domain name. If a new exporter, domain, or datasource is
added to SKILL.md -- including the OpenSearch domains that don't exist yet
-- this module picks them up automatically the next time it parses the file.
That's the whole point: the routing table is the single source of truth, and
this module is a thin, generic reader of it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
NAME_RE = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
DESCRIPTION_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)
VERSION_RE = re.compile(r'version:\s*"?([^"\n]+)"?', re.MULTILINE)

# One row of SKILL.md's §4 routing table, e.g.:
# | CPU utilization, ... | prometheus | [references/node-exporter/cpu.md](references/node-exporter/cpu.md) |
ROUTING_ROW_RE = re.compile(
    r"^\|\s*(?P<topic>.+?)\s*\|\s*(?P<data_source>.+?)\s*\|\s*"
    r"\[(?P<link_text>[^\]]+)\]\((?P<link_path>[^)]+)\)\s*(?P<trailing>.*)\|\s*$"
)

# Legacy Metric Directory row inside an exporter overview.md, e.g.:
# | cpu | CPU time spent in different modes | `node_cpu_seconds_total` | `cpu.md` |
METRIC_DIR_ROW_RE = re.compile(
    r"^\|.*\|\s*`([^`]+)`\s*\|\s*`?([A-Za-z0-9_./-]+\.md)`?\s*\|\s*$"
)

PENDING_MARKER = "pending addition"


class SkillIndexError(Exception):
    """Raised for structural problems with the skill package itself (missing
    SKILL.md, a routing-table link that resolves to nothing on disk, etc.) --
    distinct from an LLM producing a bad answer, this means the skill package
    or its config is broken and the caller should fail loudly, not silently
    degrade."""


@dataclass(frozen=True)
class RoutingRow:
    topic: str
    data_sources: tuple[str, ...]   # usually one entry; kept as a tuple since
                                     # the skill's own authors have flagged
                                     # (blueprint §J.6) that a row may become
                                     # multi-valued once a domain file carries
                                     # evidence from more than one backend.
    reference_path: str             # e.g. "references/node-exporter/cpu.md",
                                     # exactly as written in SKILL.md -- never
                                     # rewritten or normalized by this module.
    note: str = ""                  # trailing caveat text on the row, if any
                                     # (e.g. opensearch-fundamentals.md's
                                     # "infrastructure only" note).
    pending: bool = False            # True when the row is explicitly marked
                                     # "pending addition" -- the file may not
                                     # exist on disk yet; never treat that as
                                     # a broken skill package.

    @property
    def is_real_datasource(self) -> bool:
        """Excludes the "n/a" row (execution-contract.md is a downstream
        result-shape reference, not something queried against a live
        backend) from anywhere the pipeline collects "which datasources are
        actually in play for this request."""
        return any(ds.strip().lower() not in ("", "n/a") for ds in self.data_sources)


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    version: str


@dataclass
class SkillIndex:
    """Loaded, parsed view of one skill package. Build once via
    SkillIndex.load(skills_root); reuse across requests. Nothing here is
    request-specific -- this is static skill content, not runtime schema
    (that's label_discovery.py / field_discovery.py's job)."""

    skills_root: Path
    raw_text: str
    metadata: SkillMetadata
    routing_rows: list[RoutingRow] = field(default_factory=list)
    catalog_path: Path | None = None

    # ---- construction ----------------------------------------------------

    @classmethod
    def load(cls, skills_root: Path, catalog_path: Path | None = None) -> "SkillIndex":
        skill_md_path = skills_root / "SKILL.md"
        if not skill_md_path.exists():
            raise SkillIndexError(
                f"SKILL.md not found at {skill_md_path}. Set SKILLS_ROOT to "
                f"the directory that directly contains SKILL.md."
            )
        raw_text = skill_md_path.read_text(encoding="utf-8")
        metadata = _parse_frontmatter(raw_text)
        routing_rows = parse_routing_table(raw_text)
        if not routing_rows:
            raise SkillIndexError(
                "SKILL.md was read successfully but no routing-table rows "
                "were parsed out of its §4 section -- either the file's "
                "structure changed in a way this parser doesn't recognize, "
                "or the table is genuinely empty. Refusing to start with an "
                "empty routing table, since every request would silently "
                "fall through to 'unmapped'."
            )
        index = cls(
            skills_root=skills_root,
            raw_text=raw_text,
            metadata=metadata,
            routing_rows=routing_rows,
            catalog_path=catalog_path or skills_root.parent / "app" / "catalog" / "catalog.json",
        )
        index.validate()
        return index

    # ---- validation --------------------------------------------------------

    def validate(self) -> None:
        """Fail loudly at startup, not silently mid-request, if a
        non-pending routing row points at a file that doesn't exist. Mirrors
        scripts/check_metric_directory.py's own invariant #2, run here as a
        runtime guard rather than only as a maintainer-run script."""
        problems = []
        for row in self.routing_rows:
            if row.pending:
                continue
            target = self.skills_root / row.reference_path
            if not target.exists():
                problems.append(
                    f"Routing row {row.reference_path!r} (topic: "
                    f"{row.topic[:60]!r}) does not exist on disk and is not "
                    f"marked '{PENDING_MARKER}'."
                )
        if problems:
            raise SkillIndexError(
                "SKILL.md routing table references missing files:\n  - "
                + "\n  - ".join(problems)
            )

    # ---- section access -----------------------------------------------------

    def section(self, header_prefix: str) -> str:
        """Extract one top-level ('## ') section's body from SKILL.md by its
        header prefix (e.g. "## 5. Operating Principles"), up to the next
        '## ' header. Raises SkillIndexError if the header isn't found, so a
        header-text change in SKILL.md fails loudly at startup instead of
        silently sending an empty section to an LLM prompt forever."""
        lines = self.raw_text.splitlines()
        start = None
        for i, line in enumerate(lines):
            if line.strip().startswith(header_prefix):
                start = i
                break
        if start is None:
            raise SkillIndexError(
                f"SKILL.md has no section starting with {header_prefix!r}. "
                f"Section extraction is keyed to exact header text; if "
                f"SKILL.md's headers were renumbered or reworded, update the "
                f"header prefixes this module extracts by."
            )
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].startswith("## "):
                end = i
                break
        return "\n".join(lines[start:end]).strip()

    # ---- routing ------------------------------------------------------------

    def reference_paths(self) -> list[str]:
        return [row.reference_path for row in self.routing_rows if not row.pending]

    def row_for_path(self, reference_path: str) -> RoutingRow | None:
        for row in self.routing_rows:
            if row.reference_path == reference_path:
                return row
        return None

    def datasources_in_play(self, reference_paths: list[str]) -> set[str]:
        """Given a list of matched reference paths (Phase 1's routing
        output), returns the set of real data sources (excludes "n/a") those
        references collectively use, so the pipeline knows which
        *-fundamentals.md file(s) to load for query construction. Not
        hardcoded to "prometheus"/"opensearch" specifically -- whatever
        values actually appear in the table's Data source column are what
        gets returned."""
        sources: set[str] = set()
        for path in reference_paths:
            row = self.row_for_path(path)
            if row is None or not row.is_real_datasource:
                continue
            for ds in row.data_sources:
                ds = ds.strip().lower()
                if ds and ds != "n/a":
                    sources.add(ds)
        return sources

    # ---- reference file access -----------------------------------------------

    def read_reference(self, reference_path: str) -> str:
        target = self.skills_root / reference_path
        if not target.exists():
            raise SkillIndexError(
                f"Routing table points at {reference_path!r}, which does not "
                f"exist on disk. This should have been caught by validate() "
                f"at startup -- if you're seeing this, the skill package "
                f"changed underneath a running process; restart to re-load "
                f"SKILL.md."
            )
        return target.read_text(encoding="utf-8")

    def overview_path_for(self, reference_path: str) -> str | None:
        """Per SKILL.md §6 Step 3b: a domain file's Metric Directory lives in
        its exporter's overview.md, not in the domain file itself. Given a
        matched reference path, returns the path to its sibling overview.md
        -- or None if the reference path IS already an overview.md, or if it
        has no sibling overview.md at all (true for files that live directly
        under references/, like *-fundamentals.md or execution-contract.md;
        those aren't exporter/domain content and have no Metric Directory
        concept to begin with).

        This is a directory-convention rule, not a hardcoded map: it works
        for node-exporter, dcgm-exporter, and identically for any future
        exporter or OpenSearch-side "opensearch-logs/" directory that
        follows the same references/<name>/overview.md + references/<name>/
        <domain>.md layout EXTENDING.md documents as the required
        convention for adding new coverage.
        """
        rel_parts = reference_path.split("/")
        if rel_parts[-1] == "overview.md":
            return None
        if len(rel_parts) < 2:
            # Lives directly under references/ (e.g. references/prometheus-
            # fundamentals.md) -- no exporter directory, so no overview.md.
            return None
        candidate = "/".join(rel_parts[:-1]) + "/overview.md"
        if (self.skills_root / candidate).exists():
            return candidate
        return None

    def fundamentals_reference_for(self, data_source: str) -> str | None:
        """Finds the `*-fundamentals.md` reference path for a given data
        source by scanning the routing table itself -- NOT a hardcoded
        {data_source: path} map. A third data source's fundamentals file
        (e.g. a future `loki-fundamentals.md`) is picked up automatically
        the moment a routing row for it exists in SKILL.md, with zero code
        change here. This mirrors every other lookup in this module:
        the routing table is the only place a data-source-to-file mapping
        is allowed to live."""
        data_source = data_source.strip().lower()
        for row in self.routing_rows:
            if not row.reference_path.endswith("-fundamentals.md"):
                continue
            if any(ds.strip().lower() == data_source for ds in row.data_sources):
                return row.reference_path
        return None

    def metric_directory(self, overview_reference_path: str) -> dict[str, str]:
        """Returns catalog-backed metric paths for an exporter overview.

        A legacy table is still parsed when present so older skill packages can
        be loaded during migration. Current overviews intentionally omit that
        duplicate table; their metric lookup comes from catalog.json.
        """
        text = self.read_reference(overview_reference_path)
        overview_dir = "/".join(overview_reference_path.split("/")[:-1])
        metrics: dict[str, str] = {}
        in_table = False
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
                if m:
                    metric_name, domain_file = m.group(1), m.group(2)
                    metrics[metric_name] = f"{overview_dir}/{domain_file}" if overview_dir else domain_file
        if metrics:
            return metrics
        if self.catalog_path is None:
            return {}
        from app.catalog.loader import load_catalog

        exporter = overview_reference_path.split("/")[-2]
        catalog = load_catalog(self.catalog_path)
        return {
            entry.name: entry.reference_path
            for entry in catalog.metrics
            if entry.exporter == exporter and entry.reference_path
        }
        return metrics


# ---- module-level parsing helpers --------------------------------------------


def _parse_frontmatter(raw_text: str) -> SkillMetadata:
    m = FRONTMATTER_RE.match(raw_text)
    if not m:
        raise SkillIndexError(
            "SKILL.md has no YAML frontmatter block (expected a '---' "
            "delimited block at the top of the file with name/description/"
            "metadata.version)."
        )
    block = m.group(1)
    name_m = NAME_RE.search(block)
    desc_m = DESCRIPTION_RE.search(block)
    version_m = VERSION_RE.search(block)
    if not (name_m and desc_m and version_m):
        raise SkillIndexError(
            "SKILL.md frontmatter is missing one of name / description / "
            "metadata.version."
        )
    return SkillMetadata(
        name=name_m.group(1).strip(),
        description=desc_m.group(1).strip(),
        version=version_m.group(1).strip(),
    )


def parse_routing_table(skill_md_text: str) -> list[RoutingRow]:
    """Parses every data row of SKILL.md §4's routing table. Header/
    separator rows are skipped. A row's trailing text after the link (e.g.
    "— infrastructure only: ...") is preserved verbatim in `note` rather than
    discarded, since it can carry real behavioral meaning (the opensearch-
    fundamentals.md row's caveat is exactly this kind of content, and a
    future row could use the same pattern for something else)."""
    lines = skill_md_text.splitlines()

    # Isolate the §4 section the same way SkillIndex.section() would, so this
    # function is usable standalone (e.g. by scripts/tests) without
    # constructing a full SkillIndex.
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("## 4."):
            start = i
            break
    if start is None:
        return []
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    section_lines = lines[start:end]

    rows: list[RoutingRow] = []
    for line in section_lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if stripped.startswith("|---") or stripped.startswith("| ---"):
            continue
        if stripped.lower().startswith("| route when"):
            continue  # header row
        m = ROUTING_ROW_RE.match(stripped)
        if not m:
            continue
        topic = m.group("topic")
        data_source_raw = m.group("data_source")
        link_path = m.group("link_path").strip()
        trailing = m.group("trailing").strip(" |")
        pending = PENDING_MARKER in stripped.lower()
        data_sources = tuple(
            part.strip() for part in data_source_raw.split(",") if part.strip()
        )
        rows.append(
            RoutingRow(
                topic=topic,
                data_sources=data_sources,
                reference_path=link_path,
                note=trailing,
                pending=pending,
            )
        )
    return rows
