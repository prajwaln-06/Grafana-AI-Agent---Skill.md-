#!/usr/bin/env python3
r"""
check_metric_directory.py

Maintainer-run consistency checker for the observability-query-builder skill.
Not invoked by Claude during a conversation — this is a development-time tool.

Verifies two invariants stated in SKILL.md, exporter-overview-template.md, and
domain-reference-template.md but never previously checked by anything other than
careful authoring (Phase 0 finding):

    1. Every catalog metric has a corresponding `### `metric_name`` definition
       in its referenced domain file (the catalog is now authoritative).
  2. Every reference file linked from SKILL.md's routing table (§4) exists on
     disk, unless explicitly marked "pending addition" in that table.

Usage:
    python3 scripts/check_metric_directory.py [skill_root]

Exits non-zero if any inconsistency is found. Prints a report either way.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

METRIC_DIR_ROW_RE = re.compile(
    r"^\|.*\|\s*`([^`]+)`\s*\|\s*`?([A-Za-z0-9_./-]+\.md)`?\s*\|\s*$"
)
DOMAIN_METRIC_HEADER_RE = re.compile(r"^###\s+`([^`]+)`")
SKILL_ROUTING_LINK_RE = re.compile(r"\[references/([^\]]+)\]\(references/([^)]+)\)")
PENDING_MARKER = "pending addition"


def find_overview_files(references_dir: Path) -> list[Path]:
    return sorted(references_dir.glob("*/overview.md"))


def parse_metric_directory(overview_path: Path) -> dict[str, Path]:
    """Returns {metric_name: resolved domain file path} from the Metric Directory table."""
    metrics: dict[str, Path] = {}
    in_table = False
    for line in overview_path.read_text().splitlines():
        if line.strip().startswith("| Domain | Intent"):
            in_table = True
            continue
        if in_table:
            if not line.strip().startswith("|"):
                in_table = False
                continue
            if line.strip().startswith("|---"):
                continue
            m = METRIC_DIR_ROW_RE.match(line)
            if m:
                metric_name, domain_file = m.group(1), m.group(2)
                metrics[metric_name] = overview_path.parent / domain_file
    return metrics


def parse_domain_metrics(domain_path: Path) -> set[str]:
    if not domain_path.exists():
        return set()
    metrics = set()
    for line in domain_path.read_text().splitlines():
        m = DOMAIN_METRIC_HEADER_RE.match(line)
        if m:
            metrics.add(m.group(1))
    return metrics


def check_metric_directory_consistency(skill_root: Path) -> list[str]:
    problems: list[str] = []
    references_dir = skill_root / "references"
    for overview_path in find_overview_files(references_dir):
        directory_metrics = parse_metric_directory(overview_path)
        by_file: dict[Path, set[str]] = {}
        for metric, domain_file in directory_metrics.items():
            by_file.setdefault(domain_file, set()).add(metric)

        for domain_file, expected_metrics in by_file.items():
            if not domain_file.exists():
                problems.append(
                    f"{overview_path}: Metric Directory points at "
                    f"{domain_file.relative_to(skill_root)}, which does not exist "
                    f"(may be pending addition — verify against "
                    f"MIGRATION-REPORT.md)."
                )
                continue
            actual_metrics = parse_domain_metrics(domain_file)
            missing_in_domain = expected_metrics - actual_metrics
            missing_in_directory = actual_metrics - expected_metrics
            for metric in sorted(missing_in_domain):
                problems.append(
                    f"{overview_path}: lists `{metric}` in the Metric Directory "
                    f"but {domain_file.relative_to(skill_root)} has no matching "
                    f"### `{metric}` definition."
                )
            for metric in sorted(missing_in_directory):
                problems.append(
                    f"{domain_file.relative_to(skill_root)}: defines `{metric}` "
                    f"but {overview_path.relative_to(skill_root)}'s Metric "
                    f"Directory has no row for it."
                )
    return problems


def check_catalog_consistency(skill_root: Path) -> list[str]:
    """Verify the catalog is wired to real metric-specific Markdown.

    Legacy overview tables, when present in an older package, are still
    checked by check_metric_directory_consistency above. Current overview
    files intentionally have no metric table.
    """
    catalog_path = skill_root.parent / "app" / "catalog" / "catalog.json"
    if not catalog_path.exists():
        return [f"{catalog_path} not found."]
    try:
        import json

        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{catalog_path}: cannot read catalog: {exc}"]

    problems: list[str] = []
    names: set[str] = set()
    for entry in data.get("metrics", []):
        name = entry.get("name")
        reference = entry.get("reference_path")
        if not name or name in names:
            problems.append(f"{catalog_path}: duplicate or empty metric name {name!r}.")
            continue
        names.add(name)
        if not reference:
            problems.append(f"{catalog_path}: metric {name!r} has no reference_path.")
            continue
        domain_path = skill_root / reference
        if not domain_path.exists():
            problems.append(f"{catalog_path}: {name!r} points to missing {reference}.")
            continue
        if not re.search(rf"^###\s+`{re.escape(name)}`\s*$", domain_path.read_text(encoding="utf-8"), re.MULTILINE):
            problems.append(f"{domain_path}: missing catalog metric definition {name!r}.")
    return problems


def check_routing_links_resolve(skill_root: Path) -> list[str]:
    problems: list[str] = []
    skill_md = skill_root / "SKILL.md"
    if not skill_md.exists():
        return [f"{skill_md} not found."]
    for line in skill_md.read_text().splitlines():
        if "§4" in line or "routing table" in line.lower():
            continue
        for m in SKILL_ROUTING_LINK_RE.finditer(line):
            rel_path = m.group(2)
            target = skill_root / "references" / rel_path
            if not target.exists() and PENDING_MARKER not in line:
                problems.append(
                    f"SKILL.md links to references/{rel_path}, which does not "
                    f"exist and is not marked '{PENDING_MARKER}' on that line."
                )
    return problems


def main() -> int:
    skill_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    problems = []
    problems += check_metric_directory_consistency(skill_root)
    problems += check_catalog_consistency(skill_root)
    problems += check_routing_links_resolve(skill_root)

    if not problems:
        print("OK: Metric Directory / domain-file consistency and routing links verified.")
        return 0

    print(f"Found {len(problems)} issue(s):\n")
    for p in problems:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
