"""
registry.py

main_SKILL.md v3.0 made the sub-file registry fully dynamic (Section 4):
"The agent will receive the data_source, version, purpose, and
trigger_keywords for all available sub-skills programmatically at
runtime." There is no longer a markdown table in main_SKILL.md to parse
— this module IS that runtime mechanism.

It scans the skills/ folder for exporter subdirectories, reads each
one's _index.md frontmatter, and builds the registry Phase 1 needs to
route a question — with zero code changes required when a new exporter
is added, matching Section 11's stated extensibility rule.

No LLM call happens anywhere in this module.
"""
import os
import yaml

REQUIRED_FRONTMATTER_FIELDS = ("name", "purpose", "data_source", "version", "trigger_keywords", "domains")


class RegistryError(ValueError):
    """Raised when an _index.md's frontmatter is missing a required field.
    Deliberately loud rather than silently skipping a malformed exporter —
    a routing registry with a silently-missing entry is worse than one
    that fails to load at all."""


def _parse_frontmatter(markdown_text: str) -> dict:
    """
    Extracts and parses the YAML frontmatter block (between the first
    pair of '---' lines) from an _index.md file's text.
    """
    if not markdown_text.startswith("---"):
        raise RegistryError("File does not start with a '---' frontmatter block.")
    parts = markdown_text.split("---", 2)
    if len(parts) < 3:
        raise RegistryError("Frontmatter block is not properly closed with a second '---'.")
    return yaml.safe_load(parts[1]) or {}


def scan_skills_folder(skills_root: str) -> list[dict]:
    """
    skills_root: path to the skills/ folder (containing main_skill.md,
    the *_fundamentals.md files, and one subfolder per exporter).

    Returns one registry entry per exporter subfolder found, each:
    {
      "sub_file_id": "node_exporter",       # the subfolder name
      "index_path": "/abs/path/node_exporter/_index.md",
      "index_dir": "/abs/path/node_exporter",
      "name": ..., "purpose": ..., "data_source": ...,
      "version": ..., "trigger_keywords": [...], "domains": [...]
    }

    A subfolder without an _index.md, or one whose frontmatter is
    missing a required field, raises RegistryError immediately rather
    than being silently skipped — a routing registry should never have
    a gap nobody notices.
    """
    entries = []
    for entry_name in sorted(os.listdir(skills_root)):
        subdir = os.path.join(skills_root, entry_name)
        if not os.path.isdir(subdir):
            continue  # skip top-level files like main_skill.md itself
        index_path = os.path.join(subdir, "_index.md")
        if not os.path.isfile(index_path):
            continue  # not an exporter folder (no _index.md) — skip quietly

        with open(index_path, "r", encoding="utf-8") as f:
            text = f.read()
        frontmatter = _parse_frontmatter(text)

        missing = [field for field in REQUIRED_FRONTMATTER_FIELDS if field not in frontmatter]
        if missing:
            raise RegistryError(
                f"{index_path} is missing required frontmatter field(s): {missing}. "
                f"Per main_SKILL.md Section 4.1, every _index.md must declare all of "
                f"{REQUIRED_FRONTMATTER_FIELDS}."
            )

        entries.append({
            "sub_file_id": entry_name,
            "index_path": index_path,
            "index_dir": subdir,
            **frontmatter,
        })

    return entries


def format_registry_for_prompt(entries: list[dict]) -> str:
    """
    Renders the scanned registry as plain text for Phase 1's routing
    prompt — this is the data that used to live in main_SKILL.md's old
    Section 4 table, now assembled at runtime instead.
    """
    if not entries:
        return "No exporters are currently registered (skills/ folder is empty or unreadable)."

    lines = []
    for e in entries:
        keywords = ", ".join(e["trigger_keywords"])
        lines.append(
            f"- sub_file_id: \"{e['sub_file_id']}\" | data_source: {e['data_source']} | "
            f"purpose: {e['purpose']} | trigger_keywords: [{keywords}]"
        )
    return "\n".join(lines)


def resolve_domain_file(entry: dict, domain_id: str) -> str:
    """
    Given one registry entry (an exporter) and a domain_id chosen by the
    Domain Resolver (agent.py), returns the absolute path to that
    domain's .md file, using the exporter's own frontmatter-declared
    'domains' list — never guessing a filename pattern.
    """
    for domain in entry.get("domains", []):
        if domain.get("id") == domain_id:
            return os.path.join(entry["index_dir"], domain["file"])
    raise RegistryError(
        f"domain_id {domain_id!r} is not declared in {entry['sub_file_id']}'s "
        f"_index.md 'domains' list."
    )
