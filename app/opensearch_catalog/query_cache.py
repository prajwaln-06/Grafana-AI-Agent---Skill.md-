"""
query_cache.py

The OpenSearch RESULT cache: a short-lived, process-wide store of COMPLETED, normalised,
redacted execution results, so a repeated (or reworded) question can be answered without the
Router/Generator LLM calls, validation, or any OpenSearch search.

It is NOT the field-catalog cache (field_catalog.FieldCatalogCache). That one caches live
index *mappings* (what fields exist). This one caches *answers*. They share nothing except that
reusing an answer re-reads the field catalog to prove the answer is still permitted.

Safety model (false hits are worse than misses; every doubt is a miss)
----------------------------------------------------------------------
1. WHAT is stored: only a result that is complete and successful -- every part an `ok`
   OpenSearch entry whose execution succeeded (or legitimately matched nothing), is not partial
   (no timeout / shard failure), and, for an analysis, finished (`completed`, not truncated).
   Errors, rejections, partial runs, Prometheus results, alert proposals and any multi-entry
   response with even one unusable entry are never stored. Nothing is persisted to disk.
2. WHEN a question may reuse an entry (two levels):
   Level 1  identical canonical wording (case-preserving for literals, tense/plural folded with
            text_forms). Needs no understanding of the question.
   Level 2  reworded: both questions are parsed into a QuestionSignature -- operation, entity
            scope, concept set, literals, time window and every leftover word. They must be
            EQUAL in every facet (`equivalent()`). Anything the parser does not fully
            understand (compound questions, calendar times, unknown time words, several
            operations) is never matched at Level 2. A synonym is accepted only from a fixed,
            small lexicon or from an operator-declared per-family alias.
   This is deterministic rule-based matching, NOT embedding/similarity matching: there is no
   score and no threshold, because facet equality is the whole contract. (A statistical
   similarity could only ever add a requirement on top of it; none is used today.)
3. Level 2 is offered only for entries whose own question was cross-checked against the query
   that really ran: the analysis type matches the question's concepts, the window matches the
   executed window, every literal appears in the canonical query, and every entity noun is
   grounded in the pattern's registry roles. Otherwise the entry answers its exact wording only.
4. Reuse re-runs the real guard: the registry, the live field view, configuration and skill text
   must hash to what they were when the answer was produced, and the stored canonical query must
   still pass guard.check_entry unchanged. A cache hit therefore can never replay an answer the
   current guard would refuse.
5. FRESHNESS: a fixed expiry (never extended by reads). An entry whose window is relative
   ("now-3m") lives at most 10% of its narrowest span, so a reused answer describes
   effectively the same interval.
6. ISOLATION: entries are keyed by scope (app/user); one scope never sees another's. Stored
   results are JSON text; every hit is a fresh deserialisation, so callers cannot mutate the cache.
7. Any internal failure (signature building, matching, hashing, serialisation, lock) is a miss / not-stored, never a
   failed request.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping

from app.opensearch_catalog import guard
from app.opensearch_catalog.context import Limits, OpenSearchContext, PatternContext
from app.opensearch_catalog.field_catalog import PatternFieldView, build_field_view
from app.opensearch_catalog.registry import IndexRegistry, parse_short_duration_seconds
from app.opensearch_catalog.text_forms import word_forms

logger = logging.getLogger(__name__)

MAX_ENTRY_BYTES = 1_000_000
MAX_QUESTION_CHARS = 2000
# An entry with a relative window never outlives this fraction of its narrowest span ...
TTL_SPAN_FRACTION = 0.10
# ... but a very narrow span does not shrink the lifetime below this (or the configured ttl, if lower).
MIN_EFFECTIVE_TTL_SECONDS = 5.0

# analysis type <-> the concept a question must carry for Level-2 reuse (others: exact wording only)
_ANALYSIS_CONCEPT = {"absence": "ABSENT", "recurrence": "RECURRING"}


# =====================================================================================================
# 1. question -> signature
# =====================================================================================================

# Auxiliaries / articles: dropped before phrase matching so "have not reported", "are not
# reporting" and "did not report" are the same phrase.
_AUX = frozenset({"is", "are", "was", "were", "be", "been", "being", "am", "have", "has", "had", "having",
                  "do", "does", "did", "the", "a", "an", "their", "its", "my", "our", "they", "them", "that",
                  "of", "there", "to"})
# Fillers dropped after phrase matching.
_FILLER = frozenset({"me", "us", "you", "i", "please", "can", "could", "would", "will", "should", "in", "on", "at",
                     "over", "within", "for", "across", "during", "from", "with"})
_IRREGULAR = {"went": "go", "gone": "go", "goes": "go", "sent": "send", "ran": "run"}


@lru_cache(maxsize=4096)
def _stem(word: str) -> str:
    """Tense/plural-folded lowercase form, reusing text_forms. word_forms() picks the first of a
    sorted form-set and the set depends on which form it is given ("warnings" -> "warning",
    "warning" -> "warn"), so iterate to a fixed point and keep the shortest."""
    w = word.lower()
    if w in _IRREGULAR:
        return _IRREGULAR[w]
    if w in _AUX or w in _FILLER:        # stop words keep their surface form ("does" is not "doe")
        return w
    seen = [w]
    cur = w
    for _ in range(4):
        nxt = word_forms(cur)[0].lower()
        if nxt == cur:
            break
        cur = nxt
        seen.append(cur)
    return min(seen, key=lambda s: (len(s), s))


# Dropped from the Level-1 key only (articles/politeness).
_L1_DROP = frozenset({"the", "a", "an", "please"})

_LIST_OPS = {"which", "what", "who", "show", "list", "display", "give", "find", "get", "identify", "name", "tell",
             "see", "view"}
_EXPLAIN_OPS = {"why", "cause", "reason", "explain", "diagnose"}
_WHEN_OPS = {"when"}

_ENTITY_WORDS = {"switch", "node", "device", "port", "host", "rack", "chassis", "controller", "server", "ilo", "bmc",
                 "link", "asic", "location", "service", "cabinet", "blade", "fabric", "cluster"}

_COMPOUND_WORDS = {"and", "or", "also", "then", "but", "plus", "versus", "vs", "compare", "while", "because",
                   "whereas", "additionally", "either", "both", "neither", "nor"}

_TIME_WORDS = {"last", "past", "previous", "ago", "today", "yesterday", "tonight", "tomorrow", "since", "until",
               "till", "between", "before", "after", "recent", "recently", "earlier", "latest", "morning",
               "afternoon", "evening", "night", "week", "month", "year", "hour", "minute", "second", "day", "daily",
               "hourly", "weekly", "monthly", "yearly", "midnight", "noon", "next"}


def _phrase(*words: str) -> tuple[str, ...]:
    return tuple(_stem(w) for w in words if w not in _AUX)


# phrase (stems, auxiliaries removed) -> concepts
_SIGNAL_ABSENT = ("ABSENT", "SIGNAL")
_PHRASES: dict[tuple[str, ...], tuple[str, ...]] = {
    # stopped / not sending or reporting  -> absent signal
    _phrase("stopped", "sending"): _SIGNAL_ABSENT,
    _phrase("stopped", "reporting"): _SIGNAL_ABSENT,
    _phrase("stopped", "emitting"): _SIGNAL_ABSENT,
    _phrase("stopped", "transmitting"): _SIGNAL_ABSENT,
    _phrase("no", "longer", "sending"): _SIGNAL_ABSENT,
    _phrase("no", "longer", "reporting"): _SIGNAL_ABSENT,
    _phrase("no", "longer", "emitting"): _SIGNAL_ABSENT,
    _phrase("not", "sending"): _SIGNAL_ABSENT,
    _phrase("not", "reporting"): _SIGNAL_ABSENT,
    _phrase("not", "emitting"): _SIGNAL_ABSENT,
    _phrase("failed", "to", "report"): _SIGNAL_ABSENT,
    _phrase("failed", "to", "send"): _SIGNAL_ABSENT,
    _phrase("gone", "silent"): _SIGNAL_ABSENT,
    _phrase("gone", "quiet"): _SIGNAL_ABSENT,
    _phrase("silent"): _SIGNAL_ABSENT,
    _phrase("quiet"): _SIGNAL_ABSENT,
    _phrase("missed"): ("ABSENT",),
    _phrase("heartbeat", "signal"): ("SIGNAL",),
    _phrase("heartbeat"): ("SIGNAL",),
    _phrase("down"): ("DOWN",),
    _phrase("recurring"): ("RECURRING",),
    _phrase("repeated"): ("RECURRING",),
    _phrase("repeatedly"): ("RECURRING",),
    _phrase("recurrence"): ("RECURRING",),
    _phrase("recurrent"): ("RECURRING",),
    _phrase("persistent"): ("RECURRING",),
    _phrase("persistently"): ("RECURRING",),
    _phrase("right", "now"): ("NOW",),
    _phrase("now"): ("NOW",),
    _phrase("currently"): ("NOW",),
    _phrase("current"): ("NOW",),
    _phrase("at", "the", "moment"): ("NOW",),
    _phrase("at", "present"): ("NOW",),
    # operations
    _phrase("how", "many"): ("OP:count",),
    _phrase("count"): ("OP:count",),
}
_TIME_STEMS = frozenset(_stem(w) for w in _TIME_WORDS)
_COMPOUND_STEMS = frozenset(_stem(w) for w in _COMPOUND_WORDS)
_ENTITY_STEMS = frozenset(_stem(w) for w in _ENTITY_WORDS)
_LIST_STEMS = frozenset(_stem(w) for w in _LIST_OPS)
_EXPLAIN_STEMS = frozenset(_stem(w) for w in _EXPLAIN_OPS)
_WHEN_STEMS = frozenset(_stem(w) for w in _WHEN_OPS)
_AUX_STEMS = frozenset(_AUX)
_FILLER_STEMS = frozenset(_FILLER)
_MAX_PHRASE = max(len(k) for k in _PHRASES)

_UNIT_SECONDS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400,
                 "week": 604800}
_QUOTED_RE = re.compile(r'"([^"]+)"|“([^”]+)”|`([^`]+)`|(?<!\w)\'([^\']+)\'(?!\w)')
_ROLLING_RE = re.compile(r"\b(?:the\s+)?(?:last|past|previous)\s+(?:(\d+)\s*)?"
                         r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)\b", re.I)
_CLOCKISH_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_.\-:/@]*[A-Za-z0-9_])?")
_PLACEHOLDER_RE = re.compile(r"^zz([qw])(\d+)zz$")


def _is_literal(tok: str) -> bool:
    """Tokens whose exact spelling matters to a filter: digits, punctuation inside, or case that
    carries meaning (ALLCAPS, mixedCase). Their case is preserved; ordinary words are not."""
    if any(c.isdigit() for c in tok) or any(c in tok for c in "_.:/@-"):
        return True
    return len(tok) >= 2 and (tok.isupper() or any(c.isupper() for c in tok[1:]))


@dataclass(frozen=True)
class QuestionSignature:
    canonical_text: str                 # Level-1 key
    operation: str | None               # list | count | explain | when | None
    entities: frozenset[str]
    concepts: frozenset[str]
    literals: frozenset[str]
    window_seconds: int | None
    residual: frozenset[str]            # every content word the lexicon did not explain
    understood: bool                    # False: only exact (Level-1) wording may match
    reasons: tuple[str, ...] = ()       # why not understood


def _normalise_alias_items(aliases: Mapping[str, str] | tuple) -> tuple[tuple[str, str], ...]:
    items = aliases.items() if isinstance(aliases, Mapping) else aliases
    return tuple(sorted((_stem(str(k).strip()), str(v)) for k, v in items))


def build_signature(question: str, aliases: Mapping[str, str] | tuple = ()) -> QuestionSignature:
    """Deterministic, dependency-free. `aliases` maps a (stemmed) word to replacement words, applied
    before phrase matching; it is how an operator declares "in this index family, 'node' means
    'switch'". Never raises."""
    return _build_signature(question if isinstance(question, str) else "", _normalise_alias_items(aliases))


@lru_cache(maxsize=2048)
def _build_signature(question: str, alias_items: tuple[tuple[str, str], ...]) -> QuestionSignature:
    reasons: list[str] = []
    text = unicodedata.normalize("NFKC", question).strip()
    if len(text) > MAX_QUESTION_CHARS:
        text = text[:MAX_QUESTION_CHARS]
        reasons.append("question too long")

    quotes: list[str] = []

    def _quote(m: re.Match) -> str:
        quotes.append(" ".join(next(g for g in m.groups() if g is not None).split()))
        return f" zzq{len(quotes) - 1}zz "

    text = _QUOTED_RE.sub(_quote, text)

    windows: list[int] = []

    def _window(m: re.Match) -> str:
        n = int(m.group(1)) if m.group(1) else 1
        unit = m.group(2).lower().rstrip("s")
        unit = {"sec": "second", "min": "minute", "hr": "hour"}.get(unit, unit)
        windows.append(n * _UNIT_SECONDS[unit])
        return f" zzw{len(windows) - 1}zz "

    text = _ROLLING_RE.sub(_window, text)
    if _CLOCKISH_RE.search(text):
        reasons.append("calendar/clock time")
    if len(windows) > 1:
        reasons.append("several time windows")

    text = re.sub(r"\bwon['’]t\b", "will not", text, flags=re.I)
    text = re.sub(r"\bcan['’]t\b", "can not", text, flags=re.I)
    text = re.sub(r"\b(\w+)n['’]t\b", r"\1 not", text, flags=re.I)
    text = re.sub(r"['’]s\b", "", text)
    text = text.replace("'", " ").replace("’", " ")

    # items: ("w", stem) | ("l", literal) | ("q", literal) | ("t", seconds)
    raw_items: list[tuple[str, Any]] = []
    for tok in _TOKEN_RE.findall(text):
        pm = _PLACEHOLDER_RE.match(tok)
        if pm and pm.group(1) == "q" and int(pm.group(2)) < len(quotes):
            raw_items.append(("q", "q:" + quotes[int(pm.group(2))]))
        elif pm and pm.group(1) == "w" and int(pm.group(2)) < len(windows):
            raw_items.append(("t", windows[int(pm.group(2))]))
        elif _is_literal(tok):
            raw_items.append(("l", tok))
        else:
            raw_items.append(("w", _stem(tok)))

    # Level-1 key: everything, in order, minus articles/politeness.
    parts: list[str] = []
    for kind, val in raw_items:
        if kind == "w":
            if val not in _L1_DROP:
                parts.append(val)
        elif kind == "t":
            parts.append(f"w:{val}")
        else:
            parts.append(val)
    canonical_text = " ".join(parts)

    # aliases (operator-declared, per index family), then auxiliaries out, then phrases.
    alias_map = dict(alias_items)
    items: list[tuple[str, Any]] = []
    for kind, val in raw_items:
        if kind == "w" and val in alias_map:
            for w in _TOKEN_RE.findall(alias_map[val]):
                items.append(("w", _stem(w)))
        else:
            items.append((kind, val))
    items = [(k, v) for k, v in items if not (k == "w" and v in _AUX_STEMS)]

    operation_set: set[str] = set()
    concepts: set[str] = set()
    entities: set[str] = set()
    literals: set[str] = set()
    residual: set[str] = set()
    window_seconds = windows[0] if len(windows) == 1 else None

    i = 0
    while i < len(items):
        kind, val = items[i]
        if kind == "w":
            matched = False
            for n in range(min(_MAX_PHRASE, len(items) - i), 0, -1):
                chunk = items[i:i + n]
                if all(k == "w" for k, _ in chunk):
                    hit = _PHRASES.get(tuple(v for _, v in chunk))
                    if hit is not None:
                        for c in hit:
                            if c.startswith("OP:"):
                                operation_set.add(c[3:])
                            else:
                                concepts.add(c)
                        i += n
                        matched = True
                        break
            if matched:
                continue
            if val in _LIST_STEMS:
                operation_set.add("list")
            elif val in _EXPLAIN_STEMS:
                operation_set.add("explain")
            elif val in _WHEN_STEMS:
                operation_set.add("when")
            elif val in _FILLER_STEMS:
                pass
            elif val in _COMPOUND_STEMS:
                reasons.append("compound question")
            elif val in _TIME_STEMS:
                reasons.append("unparsed time expression")
                residual.add(val)
            elif val in _ENTITY_STEMS:
                entities.add(val)
            else:
                residual.add(val)
        elif kind in ("l", "q"):
            literals.add(val)
        i += 1

    if len(operation_set) > 1:
        reasons.append("several operations")
    operation = next(iter(operation_set)) if len(operation_set) == 1 else None
    return QuestionSignature(
        canonical_text=canonical_text, operation=operation, entities=frozenset(entities),
        concepts=frozenset(concepts), literals=frozenset(literals), window_seconds=window_seconds,
        residual=frozenset(residual), understood=not reasons, reasons=tuple(dict.fromkeys(reasons)))


def _has_content(sig: QuestionSignature) -> bool:
    return bool(sig.operation or sig.entities or sig.concepts or sig.literals or sig.residual
                or sig.window_seconds is not None)


def equivalent(a: QuestionSignature, b: QuestionSignature) -> bool:
    """The Level-2 equivalence contract: both fully understood, carrying some substantive content,
    and EQUAL in every facet. A single unexplained word that differs makes them different. A
    question with no content at all (only fillers/articles) matches nothing but its exact wording."""
    return (a.understood and b.understood and _has_content(a) and _has_content(b) and a.operation == b.operation and a.entities == b.entities
            and a.concepts == b.concepts and a.literals == b.literals and a.window_seconds == b.window_seconds
            and a.residual == b.residual)


# =====================================================================================================
# 2. fingerprints (what an entry's validity depends on)
# =====================================================================================================


def _json_default(o: Any) -> Any:
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    raise TypeError(f"not serialisable: {type(o).__name__}")


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=_json_default, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def fingerprint_pattern(pattern) -> str:
    """The registry entry for one index family: its permissions, roles, analyses, severity rules."""
    return _digest(dataclasses.asdict(pattern))


def fingerprint_view(view: PatternFieldView) -> str:
    """Which fields the live mapping confirms and what each may do. Excludes only the
    fresh/stale label and free-text warnings."""
    return _digest({
        "time_field": view.time_field, "time_confirmed": view.time_field_confirmed,
        "variation": view.mapping_variation,
        "allowed": {p: {"family": i.family, "ops": i.ops, "kw": i.keyword_subfield, "ia": i.keyword_ignore_above,
                        "conflict": i.conflict, "nested": i.nested, "present": i.present_in}
                    for p, i in view.allowed_fields.items()},
        "roles": view.roles_confirmed, "group_by": view.group_by_fields,
    })


def fingerprint_config(settings) -> str:
    """Settings that change what a stored answer would look like or how a question is routed."""
    return _digest({
        "limits": dataclasses.asdict(Limits.from_settings(settings)),
        "redact_usernames": settings.opensearch_redact_usernames, "redact_ip": settings.opensearch_redact_ip_mode,
        "max_series": settings.max_series_per_result, "max_hits": settings.max_hits_per_result,
        "url": settings.opensearch_url, "user": settings.opensearch_auth_username,
        "alerts": settings.alert_rule_creation_enabled, "dependent": settings.dependent_query_resolution_enabled,
        "model": settings.gemini_model,
    })


_ref_text_cache: dict[tuple[str, int, int], str] = {}
_ref_lock = threading.Lock()


def _read_reference(path: Path) -> str:
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        with _ref_lock:
            if key in _ref_text_cache:
                return _ref_text_cache[key]
        text = path.read_text(encoding="utf-8")
        with _ref_lock:
            if len(_ref_text_cache) > 64:
                _ref_text_cache.clear()
            _ref_text_cache[key] = text
        return text
    except OSError:
        return ""


def fingerprint_skill(skill_index, settings, pattern) -> str:
    """SKILL.md plus the family's domain reference: they decide how a question becomes a query."""
    raw = getattr(skill_index, "raw_text", "") or ""
    ref = _read_reference(Path(settings.skills_root) / pattern.domain_reference)
    return _digest({"skill": raw, "reference": ref})


@dataclass(frozen=True)
class PatternPrint:
    pattern_id: str
    registry: str
    view: str
    skill: str


@dataclass(frozen=True)
class Fingerprints:
    config: str
    patterns: tuple[PatternPrint, ...]


def compute_fingerprints(pattern_ids, *, registry: IndexRegistry, field_cache, settings, skill_index
                         ) -> tuple[Fingerprints | None, str]:
    """(fingerprints, "") or (None, reason). Fails when a pattern is gone/disabled or its live
    field view is not usable: such an answer is neither stored nor reused."""
    prints = []
    for pid in sorted(set(pattern_ids)):
        pattern = registry.get(pid)
        if pattern is None or not pattern.enabled:
            return None, f"pattern {pid!r} is not an enabled registry pattern"
        view = build_field_view(pattern, field_cache.get(pattern))
        if not PatternContext(entry=pattern, view=view).usable:
            return None, f"live field discovery is not usable for {pid!r}"
        prints.append(PatternPrint(pid, fingerprint_pattern(pattern), fingerprint_view(view),
                                   fingerprint_skill(skill_index, settings, pattern)))
    return Fingerprints(config=fingerprint_config(settings), patterns=tuple(prints)), ""


# =====================================================================================================
# 3. what may be stored
# =====================================================================================================


@dataclass(frozen=True)
class PartFacts:
    pattern_id: str
    analysis_type: str | None
    analysis_json: str            # canonical analysis parameters ("" = none)
    query_json: str               # canonical DSL
    window_from: str
    window_to: str
    window_seconds: float
    window_relative: bool


@dataclass(frozen=True)
class Assessment:
    cacheable: bool
    reason: str = ""
    parts: tuple[PartFacts, ...] = ()
    semantic_ok: bool = False
    semantic_reason: str = ""
    max_ttl: float | None = None
    mode: str = "single"


def _blocks(executed: dict) -> tuple[str, list[dict]]:
    if executed.get("mode") == "multi":
        res = executed.get("results")
        return "multi", res if isinstance(res, list) else []
    return "single", [executed]


# ---- is a finished analysis COMPLETE? ------------------------------------------------------------------
#
# analysis.py reports its caveats as `warnings` of the form "<code>: <message>". Some analyses
# also set a summary flag (absence: `complete`, text_ratio_shortfall: `scan_complete`), but
# context_window and recurrence do NOT: they signal a cut-short answer only through a warning
# (`anchor_<i>_truncated`, `ranking_limited`). So warnings are classified by code:
#
#   INCOMPLETE  -- data is missing from the answer: never cached.
#   INFORMATIONAL -- the answer is complete and the caveat only helps reading it: cacheable.
#   anything else -- an unclassified code (e.g. a warning added to analysis.py later) is treated
#                    as incomplete (fail closed) until someone classifies it here;
#                    test_every_warning_code_in_analysis_py_is_classified enforces that.
INCOMPLETE_WARNING_CODES = frozenset({
    "incomplete_entity_list",   # absence: more entities exist than the analysis limit
    "ranking_limited",          # recurrence: ranking applied to a doc_count-truncated entity set
    "scan_limited",             # text_ratio_shortfall: more matching rows than were scanned
})
INCOMPLETE_WARNING_PATTERNS = (
    re.compile(r"anchor_\d+_truncated"),   # context_window: rows around an event cut at the row limit
    re.compile(r"anchor_\d+_skipped"),     # context_window: an event found but not expanded
)
INFORMATIONAL_WARNING_CODES = frozenset({
    "no_recent_reports",        # absence: nothing reported recently (full lists, possible ingestion outage)
    "no_anchor_event",          # context_window: no event matched, so there is genuinely nothing to show
    "negative_lag",             # ingest_lag: clock skew in the measured rows
    "no_lag_measurable",        # ingest_lag: no returned row had both times; the answer is "none measurable"
})


def _warning_code(warning: Any) -> str | None:
    if not isinstance(warning, str):
        return None
    code = warning.split(":", 1)[0].strip()
    return code or None


def classify_warning(warning: Any) -> str:
    """"incomplete" | "informational" | "unknown" (unknown is treated as incomplete)."""
    code = _warning_code(warning)
    if code is None:
        return "unknown"
    if code in INCOMPLETE_WARNING_CODES or any(p.fullmatch(code) for p in INCOMPLETE_WARNING_PATTERNS):
        return "incomplete"
    if code in INFORMATIONAL_WARNING_CODES:
        return "informational"
    return "unknown"


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _context_window_problem(summary: dict) -> str:
    """"" only if the summary PROVES that every event the anchor query matched was expanded.

    The proof needs the anchor query's total (`anchors_matched`, same shape as `documents_matched`):
      * relation "eq"  -> exact; complete only when value == anchors_found == anchors_expanded;
      * relation "gte" -> a lower bound; never proof, even when its number equals the count returned;
      * "unknown" / missing / malformed / contradictory -> no proof.
    (An exact total above the configured anchor limit is simply value > anchors_found.)"""
    found, expanded, anchors = summary.get("anchors_found"), summary.get("anchors_expanded"), summary.get("anchors")
    if not (_is_count(found) and _is_count(expanded) and isinstance(anchors, list)):
        return "analysis is incomplete (the event counts are missing or malformed)"
    if expanded > found or len(anchors) != expanded:
        return "analysis is incomplete (the event counts contradict each other)"
    if expanded < found:
        return "analysis is incomplete (an event was not expanded)"
    matched = summary.get("anchors_matched")
    if not isinstance(matched, dict):
        return "analysis is incomplete (the number of matching events is not reported)"
    value, relation = matched.get("value"), matched.get("relation")
    if relation == "unknown" or value is None:
        return "analysis is incomplete (OpenSearch did not report how many events matched)"
    if relation not in ("eq", "gte") or not _is_count(value):
        return "analysis is incomplete (the number of matching events is malformed)"
    if relation == "gte":
        return "analysis is incomplete (the number of matching events is only a lower bound)"
    if value > found:
        return (f"analysis is incomplete ({value} events matched but only {found} were expanded; "
                f"the anchor query returns at most its size / the anchor limit)")
    if value < found:
        return "analysis is incomplete (the number of matching events contradicts the events returned)"
    return ""


def _analysis_incomplete_reason(summary: dict, execution: dict, atype: str | None = None) -> str:
    """"" if the finished analysis is complete, else why it is not. Every check is independent: a
    result is cacheable only if none of them fires. `atype` is the analysis the entry asked for."""
    if summary.get("completed") is not True:
        return "analysis did not complete"
    # explicit summary flags (absence, text_ratio_shortfall)
    if summary.get("complete") is False or summary.get("scan_complete") is False:
        return "analysis result is truncated"
    # warnings: must be present as a list, and every one must be known and informational
    warnings = summary.get("warnings")
    if not isinstance(warnings, list):
        return "analysis warnings are missing, so completeness is unknown"
    for w in warnings:
        kind = classify_warning(w)
        if kind == "incomplete":
            return f"analysis is incomplete ({_warning_code(w)})"
        if kind == "unknown":
            return f"analysis has an unclassified warning ({_warning_code(w) or 'unreadable'}); treated as incomplete"
    # context_window: per-event flags, and every event found must have been expanded
    anchors = summary.get("anchors")
    if isinstance(anchors, list) and any(isinstance(a, dict) and a.get("truncated") is True for a in anchors):
        return "analysis is incomplete (an event's surrounding rows were truncated)"
    found, expanded = summary.get("anchors_found"), summary.get("anchors_expanded")
    if isinstance(found, int) and isinstance(expanded, int) and expanded < found:
        return "analysis is incomplete (an event was not expanded)"
    # context_window: the anchor query's own total must prove that every matching event was expanded
    if atype == "context_window" or summary.get("type") == "context_window":
        why = _context_window_problem(summary)
        if why:
            return why
    # normalisation had to cut rows/buckets/series (also covers recurrence's `sum_other_doc_count`)
    if execution.get("truncated") is True:
        return "analysis result was truncated by the result limits"
    return ""


def _part_problem(entry: Any) -> tuple[PartFacts | None, str]:
    if not isinstance(entry, dict):
        return None, "entry is not an object"
    if entry.get("status") != "ok":
        return None, f"entry status is {entry.get('status')!r}, not 'ok'"
    if (entry.get("data_source") or "").strip().lower() != "opensearch":
        return None, "not an OpenSearch result"
    pid, query = entry.get("pattern_id"), entry.get("query")
    if not isinstance(pid, str) or not pid or not isinstance(query, dict):
        return None, "entry has no pattern_id / DSL object"
    ex = entry.get("execution")
    if not isinstance(ex, dict):
        return None, "entry was not executed"
    if ex.get("execution_status") not in ("success", "empty_result") or ex.get("error"):
        return None, f"execution_status is {ex.get('execution_status')!r}"
    md = ex.get("metadata")
    if not isinstance(md, dict) or md.get("partial") is not False or md.get("timed_out") is True:
        return None, "result is partial or its completeness is unknown"
    analysis = entry.get("analysis")
    atype = None
    if analysis is not None:
        summary = ex.get("analysis")
        if not isinstance(analysis, dict) or not isinstance(summary, dict):
            return None, "analysis did not complete"
        atype = analysis.get("type") if isinstance(analysis.get("type"), str) else None
        why = _analysis_incomplete_reason(summary, ex, atype)
        if why:
            return None, why
    win = ex.get("resolved_time_range")
    if not isinstance(win, dict) or not isinstance(win.get("seconds"), (int, float)) \
            or not isinstance(win.get("from_expression"), str) or not isinstance(win.get("to_expression"), str):
        return None, "no resolved query window"
    relative = "now" in win["from_expression"] or "now" in win["to_expression"]
    try:
        qj = json.dumps(query, sort_keys=True, ensure_ascii=False, allow_nan=False)
        aj = json.dumps(analysis, sort_keys=True, ensure_ascii=False, allow_nan=False) if analysis is not None else ""
    except (TypeError, ValueError):
        return None, "query is not JSON-serialisable"
    return PartFacts(pid, atype, aj, qj, win["from_expression"], win["to_expression"], float(win["seconds"]),
                     relative), ""


def _narrowest_span(part: PartFacts) -> float | None:
    """The shortest interval, relative to 'now', that this answer describes (None for an absolute
    window, whose spans do not move with the clock)."""
    if not part.window_relative:
        return None
    spans: list[float] = [part.window_seconds]
    if part.analysis_json:
        try:
            params = json.loads(part.analysis_json)
            for key in ("recent_window", "interval"):
                if isinstance(params.get(key), str):
                    spans.append(float(parse_short_duration_seconds(params[key])))
        except (ValueError, TypeError):
            pass
    return min(spans) if spans else None


def alias_items_for(aliases: Mapping[str, Mapping[str, str]], pattern_ids) -> tuple[tuple[str, str], ...]:
    """Aliases apply only when an entry belongs to exactly one family."""
    pids = set(pattern_ids)
    if len(pids) != 1:
        return ()
    return _normalise_alias_items(aliases.get(next(iter(pids)), {}))


def _semantic_consistency(question: str, parts: tuple[PartFacts, ...], registry: IndexRegistry,
                          aliases: Mapping[str, Mapping[str, str]]) -> tuple[bool, str]:
    """May this entry be matched by DIFFERENTLY WORDED questions? Only if its own question agrees
    with the query that really ran, in everything that can be checked mechanically."""
    if len(parts) != 1:
        return False, "multi-part result: exact wording only"
    part = parts[0]
    sig = build_signature(question, alias_items_for(aliases, [part.pattern_id]))
    if not sig.understood:
        return False, "question not fully understood: " + ", ".join(sig.reasons)
    for atype, concept in _ANALYSIS_CONCEPT.items():
        if (concept in sig.concepts) != (part.analysis_type == atype):
            return False, f"question and analysis disagree about {atype!r}"
    if part.analysis_type is not None and part.analysis_type not in _ANALYSIS_CONCEPT:
        return False, f"analysis {part.analysis_type!r}: exact wording only"
    if sig.window_seconds is not None and abs(part.window_seconds - sig.window_seconds) > 1:
        return False, "the question's time window is not the executed window"
    for lit in sig.literals:
        needle = lit[2:] if lit.startswith("q:") else lit
        if needle not in part.query_json:
            return False, f"literal {needle!r} is not in the executed query"
    pattern = registry.get(part.pattern_id)
    if pattern is None:
        return False, "pattern not in registry"
    names = [r.lower() for r in pattern.roles] + [f.lower() for f in pattern.queryable_fields]
    for ent in sig.entities:
        if not any(ent in n for n in names):
            return False, f"entity {ent!r} is not grounded in {part.pattern_id!r}'s registry roles"
    return True, ""


def assess_executed(question: str, executed: Any, *, registry: IndexRegistry,
                    aliases: Mapping[str, Mapping[str, str]] | None = None) -> Assessment:
    """May this executed result be stored, for exact wording and (separately) for reworded questions?"""
    aliases = aliases or {}
    if not isinstance(question, str) or not question.strip():
        return Assessment(False, "empty question")
    if not isinstance(executed, dict):
        return Assessment(False, "result is not an object")
    mode, blocks = _blocks(executed)
    if not blocks:
        return Assessment(False, "no results")
    parts: list[PartFacts] = []
    for i, b in enumerate(blocks):
        part, why = _part_problem(b)
        if part is None:
            # one unusable entry must not let the rest through: all-or-nothing, never a partial store
            return Assessment(False, f"entry {i}: {why}", mode=mode)
        parts.append(part)
    parts_t = tuple(parts)
    spans = [s for s in (_narrowest_span(p) for p in parts_t) if s is not None]
    max_ttl = max(MIN_EFFECTIVE_TTL_SECONDS, TTL_SPAN_FRACTION * min(spans)) if spans else None
    ok, why = _semantic_consistency(question, parts_t, registry, aliases)
    return Assessment(True, "", parts_t, ok, why, max_ttl, mode)


# =====================================================================================================
# 4. the cache
# =====================================================================================================


@dataclass(frozen=True)
class CacheEntry:
    entry_id: int
    scope: str
    question: str
    canonical_text: str
    semantic_ok: bool
    mode: str
    parts: tuple[PartFacts, ...]
    result_json: str
    fingerprints: Fingerprints
    created_wall: float
    created_at: float            # monotonic
    expires_at: float            # monotonic; fixed at creation, never extended
    ttl_seconds: float


@dataclass(frozen=True)
class CacheHit:
    result: dict
    match: str                   # "exact" (same wording) | "semantic" (reworded, signature-equivalent)
    entry_id: int


Revalidator = Callable[[CacheEntry], "str | None"]


class ResultCache:
    def __init__(self, *, ttl_seconds: float = 60.0, max_entries: int = 200, semantic_enabled: bool = True,
                 aliases: Mapping[str, Mapping[str, str]] | None = None,
                 clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time):
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self.semantic_enabled = bool(semantic_enabled)      # Level 2 (reworded questions) on/off
        self.aliases = {k: dict(v) for k, v in (aliases or {}).items()}
        self._clock, self._wall = clock, wall_clock
        self._lock = threading.RLock()
        self._entries: dict[int, CacheEntry] = {}
        self._by_key: dict[tuple[str, str], int] = {}
        self._next_id = 1
        self._stats: Counter = Counter()

    # ---- bookkeeping ----

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._by_key.clear()

    def snapshot(self) -> dict:
        """Counts only: never a question, a result or a scope."""
        with self._lock:
            return {"enabled": True, "entries": len(self._entries), "max_entries": self.max_entries,
                    "ttl_seconds": self.ttl_seconds, "semantic_enabled": self.semantic_enabled,
                    **dict(self._stats)}

    def _evict_locked(self, entry_id: int) -> None:
        e = self._entries.pop(entry_id, None)
        if e is not None and self._by_key.get((e.scope, e.canonical_text)) == entry_id:
            del self._by_key[(e.scope, e.canonical_text)]

    def _purge_expired_locked(self, now: float) -> None:
        for eid in [i for i, e in self._entries.items() if e.expires_at <= now]:
            self._evict_locked(eid)
            self._stats["expired"] += 1

    # ---- store ----

    def put(self, question: str, scope: str, executed: dict, assessment: Assessment,
            fingerprints: Fingerprints) -> tuple[bool, str]:
        """(stored, reason). Never raises. First writer wins until expiry: a duplicate never replaces
        or extends a live entry, so concurrent identical misses cannot churn or contaminate."""
        try:
            if not assessment.cacheable:
                self._stats["not_stored"] += 1
                return False, assessment.reason
            if not isinstance(scope, str) or not scope:
                return False, "no scope"
            try:
                result_json = json.dumps(executed, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            except (TypeError, ValueError):
                self._stats["not_stored"] += 1
                return False, "result is not JSON-serialisable"
            if len(result_json.encode("utf-8")) > MAX_ENTRY_BYTES:
                self._stats["not_stored"] += 1
                return False, "result too large to cache"
            ttl = self.ttl_seconds if assessment.max_ttl is None else min(self.ttl_seconds, assessment.max_ttl)
            canonical_text = build_signature(question).canonical_text
            now = self._clock()
            with self._lock:
                self._purge_expired_locked(now)
                if (scope, canonical_text) in self._by_key:
                    self._stats["duplicate_not_stored"] += 1
                    return False, "an unexpired entry for this question already exists"
                while len(self._entries) >= self.max_entries:
                    oldest = min(self._entries.values(), key=lambda e: e.created_at)
                    self._evict_locked(oldest.entry_id)
                    self._stats["evicted_capacity"] += 1
                eid = self._next_id
                self._next_id += 1
                self._entries[eid] = CacheEntry(
                    entry_id=eid, scope=scope, question=question, canonical_text=canonical_text,
                    semantic_ok=assessment.semantic_ok, mode=assessment.mode, parts=assessment.parts,
                    result_json=result_json, fingerprints=fingerprints, created_wall=self._wall(),
                    created_at=now, expires_at=now + ttl, ttl_seconds=ttl)
                self._by_key[(scope, canonical_text)] = eid
                self._stats["stored"] += 1
            return True, ""
        except Exception:  # noqa: BLE001 -- caching must never fail a request
            logger.exception("result cache store failed")
            self._stats["errors"] += 1
            return False, "internal error"

    # ---- lookup ----

    def lookup(self, question: str, scope: str, revalidate: Revalidator) -> CacheHit | None:
        """A hit, or None. Never raises."""
        try:
            hit = self._lookup(question, scope, revalidate)
        except Exception:  # noqa: BLE001
            logger.exception("result cache lookup failed")
            self._stats["errors"] += 1
            return None
        self._stats["hits_" + hit.match if hit else "misses"] += 1
        return hit

    def _aliases_for(self, entry: CacheEntry) -> tuple[tuple[str, str], ...]:
        return alias_items_for(self.aliases, [p.pattern_id for p in entry.parts])

    def _lookup(self, question: str, scope: str, revalidate: Revalidator) -> CacheHit | None:
        if not isinstance(question, str) or not question.strip() or not isinstance(scope, str) or not scope:
            return None
        sig = build_signature(question)
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            candidates = [e for e in self._entries.values() if e.scope == scope]

        exact = sorted((e for e in candidates if e.canonical_text == sig.canonical_text),
                       key=lambda e: -e.created_at)
        for e in exact:
            if self._accept(e, revalidate):
                return self._hit(e, "exact")

        if not (self.semantic_enabled and sig.understood):
            return None
        tried = {e.entry_id for e in exact}
        matches: list[CacheEntry] = []
        for e in candidates:
            if e.entry_id in tried or not e.semantic_ok:
                continue
            try:
                aliases = self._aliases_for(e)
                equal = equivalent(build_signature(question, aliases), build_signature(e.question, aliases))
            except Exception:  # noqa: BLE001 -- a matching failure makes this candidate a miss, never an error
                logger.exception("question matching failed; treating as a cache miss")
                self._stats["match_errors"] += 1
                continue
            if equal:
                matches.append(e)
        for e in sorted(matches, key=lambda e: -e.created_at):        # newest equivalent entry first
            if self._accept(e, revalidate):
                return self._hit(e, "semantic")
        return None

    def _accept(self, entry: CacheEntry, revalidate: Revalidator) -> bool:
        if entry.expires_at <= self._clock():
            return False
        try:
            problem = revalidate(entry)       # outside the lock: it may read the field catalog
        except Exception as exc:  # noqa: BLE001
            problem = f"revalidation error: {type(exc).__name__}"
        if problem:
            logger.info("cached result %s not reused: %s", entry.entry_id, problem)
            with self._lock:
                self._evict_locked(entry.entry_id)
                self._stats["revalidation_failed"] += 1
            return False
        return entry.expires_at > self._clock()

    def _hit(self, entry: CacheEntry, match: str) -> CacheHit:
        result = json.loads(entry.result_json)            # a fresh object every time
        now = self._clock()
        info = {"hit": True, "match": match,
                "age_seconds": round(now - entry.created_at, 3),
                "expires_in_seconds": round(max(0.0, entry.expires_at - now), 3),
                "ttl_seconds": entry.ttl_seconds,
                "cached_at": datetime.fromtimestamp(entry.created_wall, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z")}
        _, blocks = _blocks(result)
        for b in blocks:
            ex = b.get("execution") if isinstance(b, dict) else None
            if isinstance(ex, dict):
                if not isinstance(ex.get("metadata"), dict):
                    ex["metadata"] = {}
                ex["metadata"]["result_cache"] = dict(info)
        return CacheHit(result=result, match=match, entry_id=entry.entry_id)


# =====================================================================================================
# 5. proof that a stored answer is still permitted
# =====================================================================================================


def revalidate_entry(entry: CacheEntry, *, settings, registry: IndexRegistry, field_cache, skill_index,
                     now: datetime | None = None) -> str | None:
    """None if the stored answer may still be served, else why not. Cheap (no LLM, no search): the
    cached field catalog is re-read, fingerprints are compared and the stored canonical query is run
    through the real guard again."""
    if fingerprint_config(settings) != entry.fingerprints.config:
        return "configuration changed"
    prints = {p.pattern_id: p for p in entry.fingerprints.patterns}
    contexts: dict[str, PatternContext] = {}
    for pid, stored in prints.items():
        pattern = registry.get(pid)
        if pattern is None or not pattern.enabled:
            return f"pattern {pid!r} is no longer an enabled registry pattern"
        if fingerprint_pattern(pattern) != stored.registry:
            return f"registry entry for {pid!r} changed"
        if fingerprint_skill(skill_index, settings, pattern) != stored.skill:
            return f"skill text for {pid!r} changed"
        view = build_field_view(pattern, field_cache.get(pattern))
        pc = PatternContext(entry=pattern, view=view)
        if not pc.usable:
            return f"live field discovery is not usable for {pid!r}"
        if fingerprint_view(view) != stored.view:
            return f"live field view for {pid!r} changed"
        contexts[pid] = pc
    ctx = OpenSearchContext(patterns=contexts, limits=Limits.from_settings(settings))
    for part in entry.parts:
        query = json.loads(part.query_json)
        analysis = json.loads(part.analysis_json) if part.analysis_json else None
        probe: dict = {"status": "ok", "data_source": "opensearch", "pattern_id": part.pattern_id, "query": query}
        if analysis is not None:
            probe["analysis"] = analysis
        check = guard.check_entry(probe, ctx, now=now)
        if not check.ok:
            return f"guard now rejects the stored query ({check.code})"
        if check.query != query or check.analysis != analysis:
            return "stored query is no longer the guard's canonical form"
    return None
