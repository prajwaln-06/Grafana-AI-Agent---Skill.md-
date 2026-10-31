"""
context.py

Builds the per-request OpenSearch context the pipeline hands to the Generator
(as a prompt block) and to the validator/guard (as structured data):

    matched references --registry--> pattern_ids --field catalog--> field views

A reference that maps to no registered pattern (the shared overview, the DSL
fundamentals) contributes nothing; a request with OpenSearch matched but no
registered pattern, an unreadable registry, or a family whose live field
discovery is unavailable produces an EXPLICIT unusable context -- the model is
told so and the guard rejects any query against it. Nothing here guesses.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.opensearch_catalog.field_catalog import (
    OP_EXISTS, OP_GROUP_BY, OP_MATCH, OP_RANGE, OP_SORT, OP_TERM,
    STATUS_UNAVAILABLE, FieldCatalogCache, PatternFieldView, build_field_catalog,
    build_field_view,
)
from app.opensearch_catalog.registry import IndexRegistry, PatternEntry, RegistryError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limits:
    default_hits_size: int = 20
    max_hits_size: int = 100
    default_terms_size: int = 10
    max_terms_size: int = 50
    max_histogram_buckets: int = 200
    max_total_buckets: int = 5000
    max_agg_depth: int = 3
    max_aggs: int = 8
    max_filters_buckets: int = 5
    max_bool_depth: int = 5
    max_clauses: int = 30
    max_terms_values: int = 100
    max_text_chars: int = 100
    max_term_chars: int = 256
    max_query_chars: int = 20000
    max_track_total_hits: int = 1_000_000
    search_timeout: str = "10s"

    @classmethod
    def from_settings(cls, settings) -> "Limits":
        return cls(
            default_hits_size=settings.opensearch_default_hits_size,
            max_hits_size=settings.opensearch_max_hits_size,
            default_terms_size=settings.opensearch_default_terms_size,
            max_terms_size=settings.opensearch_max_terms_size,
            max_histogram_buckets=settings.opensearch_max_histogram_buckets,
            search_timeout=settings.opensearch_search_timeout,
        )

    @property
    def search_timeout_seconds(self) -> float:
        t = self.search_timeout
        return int(t[:-2]) / 1000 if t.endswith("ms") else float(int(t[:-1]))


@dataclass(frozen=True)
class PatternContext:
    entry: PatternEntry
    view: PatternFieldView

    @property
    def usable(self) -> bool:
        return self.view.usable


@dataclass(frozen=True)
class OpenSearchContext:
    patterns: dict[str, PatternContext] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    error: str | None = None             # registry/context could not be built at all
    note: str | None = None              # e.g. "no registered index family was selected"

    @property
    def usable(self) -> bool:
        return self.error is None and any(p.usable for p in self.patterns.values())

    @property
    def prompt_block(self) -> str:
        return format_prompt_block(self)


def build_context(matched: list[dict], registry: IndexRegistry, cache: FieldCatalogCache,
                  limits: Limits) -> OpenSearchContext:
    entries: dict[str, PatternEntry] = {}
    for m in matched:
        if (m.get("data_source") or "").strip().lower() != "opensearch":
            continue
        for p in registry.patterns_for_reference(m["reference_path"]):
            entries.setdefault(p.pattern_id, p)
    if not entries:
        return OpenSearchContext(limits=limits,
                                 note="No registered OpenSearch index family was selected for this request.")
    patterns = {}
    for pid, entry in entries.items():
        patterns[pid] = PatternContext(entry=entry, view=build_field_view(entry, cache.get(entry)))
    return OpenSearchContext(patterns=patterns, limits=limits)


def error_context(message: str, limits: Limits | None = None) -> OpenSearchContext:
    return OpenSearchContext(limits=limits or Limits(), error=message)


# ---- prompt block --------------------------------------------------------------------------


def _ops_summary(info, *, groupable: bool = True, sortable: bool = True, exact: bool = True) -> str:
    """Operations advertised to the Generator. The live catalog says what the
    mapping CAN do; the registry decides what is PERMITTED, so group-by and
    sort are only advertised where the guard will accept them."""
    parts = []
    if OP_TERM in info.ops and exact:
        parts.append("exact(term/terms/prefix)")
    if OP_MATCH in info.ops:
        parts.append("text-search(match/match_phrase)")
    if OP_RANGE in info.ops:
        parts.append("range")
    if OP_GROUP_BY in info.ops and groupable:
        parts.append("group-by")
    if OP_SORT in info.ops and sortable:
        parts.append("sort")
    if not parts and OP_EXISTS in info.ops:
        parts.append("exists-only")
    return ", ".join(parts)


def format_prompt_block(ctx: OpenSearchContext) -> str:
    L = ["OpenSearch query context (authoritative for this request; produced by the system, not the skill files):"]
    if ctx.error:
        L.append(f"- UNAVAILABLE: {ctx.error}. Do NOT construct an OpenSearch query. Return status "
                 f"\"unmapped\" with an explanation that OpenSearch context is currently unavailable.")
        return "\n".join(L)
    if not ctx.patterns:
        L.append(f"- {ctx.note or 'No index family selected.'} Do NOT construct an OpenSearch query; if the question "
                 f"needs log data from a source outside the registered families, return \"unmapped\".")
        return "\n".join(L)

    lim = ctx.limits
    L += [
        "RULES (enforced deterministically after you answer; a violation rejects the whole query):",
        "- Choose index families ONLY by `pattern_id` from the list below. Put it in the result as "
        "`\"pattern_id\": \"<id>\"`. Do NOT write an `index` value; the system sets it. One pattern_id per result "
        "(use mode \"multi\" for several families).",
        "- Use ONLY the field paths listed under each pattern, and each only for the operations shown. For a text "
        "field the exact-match path is its confirmed keyword sub-field, shown as `exact=`; never assume `.keyword`.",
        "- The query must be `{\"bool\": {\"filter\": [ <time range>, ... ], ...}}` with exactly ONE `range` on the "
        "pattern's time field, as a direct child of the top-level `filter` or `must`, with BOTH a lower and an upper "
        "bound (date math like now-24h/now, or ISO timestamps). Never use event-time/display-only fields to filter.",
        "- Use the window the user asked for. NEVER shrink or widen it to fit a limit; a window over the maximum is "
        "rejected and the user is asked to narrow it.",
        "- Allowed clauses: bool(must/filter/should/must_not/minimum_should_match), term, terms, match, match_phrase "
        "(text role only, max %d chars), prefix (exact fields), range, exists. Forbidden: script, query_string, "
        "wildcard, regexp, fuzzy, nested/join queries, runtime/script fields, paging (`from`, search_after)." % lim.max_text_chars,
        "- Row queries: omit `aggs`; `size` <= %d (default %d); rows come back newest first." % (lim.max_hits_size, lim.default_hits_size),
        "- Aggregation queries: `size` 0 and exactly ONE top-level aggregation (terms, date_histogram, filters, "
        "cardinality, min, max, avg, sum, value_count), at most %d levels deep. `terms` size <= %d (default %d); group "
        "only by the group-by paths listed. `date_histogram` must use the time field with `fixed_interval`, at most %d "
        "buckets across the window." % (lim.max_agg_depth, lim.max_terms_size, lim.default_terms_size, lim.max_histogram_buckets),
        "- For counts, set `\"track_total_hits\": true` so the total is exact.",
        "- To select abnormal/unhealthy/error documents, use the named condition `{\"named_condition\": "
        "\"abnormal_severity\"}` as a clause; the system expands it with the family's documented definition. Do not "
        "write your own severity value lists or must_not on severity fields.",
        "- Set `measurement_used` to {\"type\": \"raw_metric\", \"name\": \"<short descriptor of what is searched>\", "
        "\"source_metrics\": []} and `reference_used` to the family's reference path.",
    ]
    for pid in sorted(ctx.patterns):
        pc = ctx.patterns[pid]
        e, v = pc.entry, pc.view
        L.append("")
        L.append(f"PATTERN `{pid}` (reference {e.domain_reference}) -- field discovery: {v.discovery_status}"
                 + (f" ({v.reason})" if v.reason else ""))
        if not pc.usable:
            L.append("  UNAVAILABLE: live field discovery did not produce a usable field list. Do NOT construct a "
                     "query for this pattern; return status \"unmapped\" explaining that OpenSearch field discovery "
                     "is currently unavailable for it.")
            for w in v.warnings:
                L.append(f"  warning: {w}")
            continue
        L.append(f"  time field: {e.time_field} (filter/sort/bucket on this only); maximum window: {e.max_window}; "
                 f"event-time/display-only (never filter): {', '.join(e.display_time_fields) or 'none'}")
        L.append(f"  mapping variation across recent indices: {v.mapping_variation}")
        L.append("  fields you may use (path -- type -- operations):")
        for path in sorted(p for p in v.allowed_fields if p in e.queryable_fields):
            info = v.allowed_fields[path]
            ex = f"; exact={info.keyword_subfield}" if info.keyword_subfield else ""
            part = " [present in only some recent indices]" if info.present_in == "some" else ""
            conf = " [type conflict: unusable]" if info.conflict else ""
            is_text = any(path == t or path.startswith(t + ".") for t in e.role_paths("text"))
            ops = _ops_summary(info, groupable=path in v.group_by_fields, sortable=not is_text, exact=not is_text)
            L.append(f"    - {path} -- {info.type} -- {ops}{ex if not is_text else ''}{part}{conf}")
        gb = sorted(set(v.group_by_fields.values()))
        L.append("  group-by / cardinality paths: " + (", ".join(gb) if gb else "none"))
        if v.roles_confirmed:
            L.append("  roles: " + "; ".join(f"{r}={'|'.join(ps)}" for r, ps in sorted(v.roles_confirmed.items())))
        L.append("  never group on free text: " + (", ".join(sorted(e.never_group)) or "n/a"))
        L.append("  rows return only: " + ", ".join(e.output_projection))
        sem = e.severity_semantics
        if sem is not None:
            if sem.mode == "positive_abnormal":
                L.append("  abnormal_severity = ANY of these fields holds a recognised abnormal value (OR); "
                         "empty or unrecognised values are NOT abnormal:")
            else:
                L.append("  abnormal_severity = the field exists and is not a documented normal value:")
            for f in sem.fields:
                L.append(f"    - {f.path}: normal={sorted(f.normal)} abnormal={sorted(f.abnormal)} "
                         f"other_known={sorted(f.other_known)} (source: {f.source}; compare exact case)")
        if e.known_caveats:
            L.append("  caveats an answer must respect: " + ", ".join(e.known_caveats))
        for w in v.warnings:
            L.append(f"  warning: {w}")
    return "\n".join(L)
