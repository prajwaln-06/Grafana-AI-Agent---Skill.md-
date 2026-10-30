"""
registry.py

The OpenSearch index registry: a small, human-reviewed configuration file
(index_registry.json) that says which index patterns this system may ever
query, which skill reference describes each, which field plays which
semantic role, and which fields may be queried, grouped on or returned.

It is *configuration*, not discovery. It answers "which patterns and fields
may be used at all" (permission). What actually exists on the cluster right
now, and what each field can do, is the runtime field catalog's job
(field_catalog.py). Discovery widens knowledge, never permission: a field
that exists in the live mapping but is absent from `queryable_fields` here
is not usable by a generated query.

The Generator selects a `pattern_id`; Python maps it to the `index_pattern`
string through this registry. The model never writes an index expression,
and `resolve_index()` accepts only the exact registered strings.

load_registry() validates every invariant below at load time and raises
RegistryError on the first violation, so a bad edit fails at startup
instead of silently widening what may be queried.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent / "index_registry.json"

SUPPORTED_REGISTRY_VERSION = "1"

# Fixed caveat vocabulary (skills/references/opensearch-logs/overview.md,
# "Shared Data Caveats"). Anything else is a typo, not a new caveat.
KNOWN_CAVEAT_CODES = frozenset({
    "duplicate_reemission",
    "event_time_unreliable",
    "heartbeat_is_document_kind",
    "ingest_lag_variable",
    "schema_varies_by_document_kind",
    "severity_vocabulary_per_index",
    "text_not_aggregatable",
})

_PATTERN_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# A concrete name or a prefix wildcard: letters, digits, '_', '.', '-' and an
# optional single trailing '*'. Deliberately cannot express a bare '*', '_all',
# a comma list, a cluster prefix ('remote:idx') or any other wildcard shape.
_INDEX_PATTERN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*\*?$")
_DURATION_RE = re.compile(r"^(?P<n>[1-9][0-9]*)(?P<unit>[mhdw])$")
_DURATION_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


class RegistryError(Exception):
    """The registry file is structurally invalid or violates an invariant.
    Distinct from a runtime discovery problem: this means the checked-in
    configuration is wrong and the process should not start with it."""


def parse_duration_seconds(value: str) -> int:
    """'15m' / '24h' / '7d' / '2w' -> seconds. Raises ValueError otherwise."""
    m = _DURATION_RE.match(value or "")
    if not m:
        raise ValueError(f"{value!r} is not a duration like '30m', '24h', '7d' or '2w'")
    return int(m.group("n")) * _DURATION_SECONDS[m.group("unit")]


@dataclass(frozen=True)
class PatternEntry:
    pattern_id: str
    index_pattern: str
    domain_reference: str
    enabled: bool
    time_field: str
    max_window: str
    roles: dict[str, tuple[str, ...]]
    queryable_fields: frozenset[str]
    groupable_fields: frozenset[str]
    never_group: frozenset[str]
    output_projection: tuple[str, ...]
    display_time_fields: tuple[str, ...] = ()
    known_caveats: tuple[str, ...] = ()
    description: str = ""
    sensitivity: dict = field(default_factory=dict)
    date_rotation: dict = field(default_factory=dict)

    @property
    def max_window_seconds(self) -> int:
        return parse_duration_seconds(self.max_window)

    def role_paths(self, role: str) -> tuple[str, ...]:
        return self.roles.get(role, ())


@dataclass(frozen=True)
class IndexRegistry:
    patterns: tuple[PatternEntry, ...]
    registry_version: str = SUPPORTED_REGISTRY_VERSION

    @property
    def enabled_patterns(self) -> tuple[PatternEntry, ...]:
        return tuple(p for p in self.patterns if p.enabled)

    def get(self, pattern_id: str) -> PatternEntry | None:
        for p in self.patterns:
            if p.pattern_id == pattern_id:
                return p
        return None

    def patterns_for_reference(self, reference_path: str) -> tuple[PatternEntry, ...]:
        """Enabled patterns whose domain file is `reference_path`. This is the
        reference -> pattern_id step of the Router's output."""
        return tuple(p for p in self.enabled_patterns if p.domain_reference == reference_path)

    def index_expression(self, pattern_ids: list[str] | tuple[str, ...]) -> str:
        """The index string to send for the given pattern_ids: the registered
        index patterns, comma-joined in the order given. Raises KeyError for an
        unknown or disabled pattern_id, so a bad id can never become a query."""
        if not pattern_ids:
            raise KeyError("no pattern_id given")
        parts: list[str] = []
        for pid in pattern_ids:
            entry = self.get(pid)
            if entry is None or not entry.enabled:
                raise KeyError(f"unknown or disabled pattern_id {pid!r}")
            if entry.index_pattern not in parts:
                parts.append(entry.index_pattern)
        return ",".join(parts)

    def resolve_index(self, index: str) -> tuple[PatternEntry, ...] | None:
        """Exact-equality allow-list check. `index` is a registered
        `index_pattern`, or a comma-join of several. Returns the matching
        entries, or None if ANY part is not exactly an enabled registered
        pattern (a narrower or wider wildcard, '*', '_all', a '.'-prefixed
        system index, a 'remote:idx' expression, an empty part, whitespace
        or a duplicate all return None)."""
        if not isinstance(index, str) or not index:
            return None
        found: list[PatternEntry] = []
        for part in index.split(","):
            match = next((p for p in self.enabled_patterns if p.index_pattern == part), None)
            if match is None or match in found:
                return None
            found.append(match)
        return tuple(found)

    def is_allowed_index(self, index: str) -> bool:
        return self.resolve_index(index) is not None


# ---- loading and validation ------------------------------------------------------


def _fail(where: str, message: str) -> None:
    raise RegistryError(f"{where}: {message}")


def _require_str(obj: dict, key: str, where: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        _fail(where, f"'{key}' is required and must be a non-empty string")
    return v


def _str_list(obj: dict, key: str, where: str, *, required: bool) -> list[str]:
    if key not in obj:
        if required:
            _fail(where, f"'{key}' is required")
        return []
    v = obj[key]
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        _fail(where, f"'{key}' must be a list of non-empty strings")
    if len(set(v)) != len(v):
        _fail(where, f"'{key}' contains duplicates")
    return v


def _build_entry(raw: dict, index: int, skills_root: Path | None) -> PatternEntry:
    if not isinstance(raw, dict):
        _fail(f"patterns[{index}]", "must be an object")
    pattern_id = _require_str(raw, "pattern_id", f"patterns[{index}]")
    where = f"pattern {pattern_id!r}"
    if not _PATTERN_ID_RE.match(pattern_id):
        _fail(where, "pattern_id must match ^[a-z][a-z0-9_]*$")

    index_pattern = _require_str(raw, "index_pattern", where)
    if not _INDEX_PATTERN_RE.match(index_pattern) or index_pattern.lower() == "_all" \
            or index_pattern.startswith("."):
        _fail(where, f"index_pattern {index_pattern!r} is not an allowed index pattern "
                     f"(letters/digits/_ . - with at most one trailing '*'; no bare '*', "
                     f"'_all', system ('.') index, comma list or cluster prefix)")

    domain_reference = _require_str(raw, "domain_reference", where)
    if skills_root is not None and not (skills_root / domain_reference).is_file():
        _fail(where, f"domain_reference {domain_reference!r} does not exist under {skills_root}")

    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        _fail(where, "'enabled' is required and must be true or false")

    time_field = _require_str(raw, "time_field", where)
    max_window = _require_str(raw, "max_window", where)
    try:
        parse_duration_seconds(max_window)
    except ValueError as e:
        _fail(where, f"max_window: {e}")

    queryable = _str_list(raw, "queryable_fields", where, required=True)
    groupable = _str_list(raw, "groupable_fields", where, required=True)
    never_group = _str_list(raw, "never_group", where, required=False)
    projection = _str_list(raw, "output_projection", where, required=True)
    display_time = _str_list(raw, "display_time_fields", where, required=False)
    caveats = _str_list(raw, "known_caveats", where, required=False)

    if time_field not in queryable:
        _fail(where, f"time_field {time_field!r} must be listed in queryable_fields")
    if time_field in display_time:
        _fail(where, f"time_field {time_field!r} must not also be a display-only time field")
    overlap = set(display_time) & set(queryable)
    if overlap:
        _fail(where, f"display_time_fields are display-only and must not be queryable: {sorted(overlap)}")
    if not set(groupable) <= set(queryable):
        _fail(where, f"groupable_fields not in queryable_fields: {sorted(set(groupable) - set(queryable))}")
    both = set(groupable) & set(never_group)
    if both:
        _fail(where, f"fields are both groupable and never_group: {sorted(both)}")
    allowed_out = set(queryable) | set(display_time)
    if not set(projection) <= allowed_out:
        _fail(where, f"output_projection fields neither queryable nor display-time: "
                     f"{sorted(set(projection) - allowed_out)}")
    unknown_caveats = set(caveats) - KNOWN_CAVEAT_CODES
    if unknown_caveats:
        _fail(where, f"unknown known_caveats codes: {sorted(unknown_caveats)}")

    roles_raw = raw.get("roles", {})
    if not isinstance(roles_raw, dict) or not roles_raw:
        _fail(where, "'roles' is required and must be a non-empty object")
    roles: dict[str, tuple[str, ...]] = {}
    for role, paths in roles_raw.items():
        if not _ROLE_NAME_RE.match(role):
            _fail(where, f"role name {role!r} must match ^[a-z][a-z0-9_]*$")
        if not isinstance(paths, list) or not paths or not all(isinstance(x, str) and x for x in paths):
            _fail(where, f"role {role!r} must be a non-empty list of field paths")
        missing = set(paths) - set(queryable)
        if missing:
            _fail(where, f"role {role!r} binds fields not in queryable_fields: {sorted(missing)}")
        roles[role] = tuple(paths)

    sensitivity = raw.get("sensitivity", {})
    date_rotation = raw.get("date_rotation", {})
    if not isinstance(sensitivity, dict) or not isinstance(date_rotation, dict):
        _fail(where, "'sensitivity' and 'date_rotation' must be objects when present")

    return PatternEntry(
        pattern_id=pattern_id,
        index_pattern=index_pattern,
        domain_reference=domain_reference,
        enabled=enabled,
        time_field=time_field,
        max_window=max_window,
        roles=roles,
        queryable_fields=frozenset(queryable),
        groupable_fields=frozenset(groupable),
        never_group=frozenset(never_group),
        output_projection=tuple(projection),
        display_time_fields=tuple(display_time),
        known_caveats=tuple(caveats),
        description=str(raw.get("description", "")),
        sensitivity=dict(sensitivity),
        date_rotation=dict(date_rotation),
    )


def load_registry(path: Path | str = DEFAULT_REGISTRY_PATH,
                  skills_root: Path | None = None) -> IndexRegistry:
    """Load and validate the registry. With `skills_root`, each pattern's
    `domain_reference` must exist on disk. Raises RegistryError on any
    problem, including an unreadable or non-JSON file."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise RegistryError(f"cannot read registry {path}: {e}") from e
    except ValueError as e:
        raise RegistryError(f"registry {path} is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        _fail(str(path), "top level must be an object")
    version = data.get("registry_version")
    if version != SUPPORTED_REGISTRY_VERSION:
        _fail(str(path), f"registry_version must be {SUPPORTED_REGISTRY_VERSION!r}, got {version!r}")
    raw_patterns = data.get("patterns")
    if not isinstance(raw_patterns, list) or not raw_patterns:
        _fail(str(path), "'patterns' is required and must be a non-empty list")

    entries = [_build_entry(raw, i, skills_root) for i, raw in enumerate(raw_patterns)]

    ids = [e.pattern_id for e in entries]
    dup_ids = sorted({i for i in ids if ids.count(i) > 1})
    if dup_ids:
        _fail(str(path), f"duplicate pattern_id: {dup_ids}")
    idx = [e.index_pattern for e in entries]
    dup_idx = sorted({i for i in idx if idx.count(i) > 1})
    if dup_idx:
        _fail(str(path), f"duplicate index_pattern: {dup_idx}")
    refs = [e.domain_reference for e in entries]
    dup_refs = sorted({r for r in refs if refs.count(r) > 1})
    if dup_refs:
        _fail(str(path), f"each domain_reference may belong to one pattern: {dup_refs}")

    return IndexRegistry(patterns=tuple(entries), registry_version=version)
