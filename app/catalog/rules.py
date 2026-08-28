"""
app/catalog/rules.py

Phase 5: deterministic, rule-based generation of the two catalog fields
Phase 2/4 deliberately left at their explicit "not yet classified"
placeholders -- `keywords` (always `()`) and `priority` (always
`Priority.REVIEW`) -- for every entry, regardless of how that entry
entered the catalog (Markdown-documented vendor metric, or a runtime-only
`discovered_pending_review` entry from reconciler.py).

This module is the frozen architecture's "one source of truth for
category/priority patterns" (Implementation Rule 4): the priority pattern
table (`_PRIORITY_PATTERNS`) and the keyword stopword/tokenization rules
below are defined exactly once, here. No other module should carry its
own copy of a category->priority mapping or a keyword-extraction regex --
generator.py, reconciler.py, and search.py all treat category/priority as
data they read or produce via this module, never patterns they duplicate.

Explicitly NOT this module's job:
  - Assigning `category` for a Markdown-documented metric -- that already
    comes straight from the domain file's own "- **Category:**" bullet
    (generator.py, Phase 2), which is more precise, source-derived data
    that a heuristic here should never override.
  - Deciding whether a metric is `approved`/`approved_unavailable`/etc --
    that is reconciler.py's job (Phase 4). This module only ever touches
    `keywords` and `priority`.
  - Filtering anything out. Per the frozen architecture, category and
    priority are RETRIEVAL/RANKING SIGNALS ONLY, never hard eligibility
    filters -- search.py (Phase 6) must be free to surface a `Priority.
    REVIEW` or `UNCATEGORIZED` entry if it's the best match; this module
    just tries to give search.py better signal to rank with, when it
    confidently can.
"""

from __future__ import annotations

import re
from dataclasses import replace

from app.catalog.schema import UNCATEGORIZED, Catalog, CatalogEntry, Priority

# ---- keyword generation ---------------------------------------------------

# Small, generic English stopwords plus a few words so common across this
# skill package's own Purpose/Category text (e.g. "measures", "total")
# that keeping them as keywords would make every entry match every query
# equally -- pure noise reduction, not domain-specific tuning.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "into", "is", "it", "its", "of", "on", "or", "over", "per", "such",
    "than", "that", "the", "this", "to", "used", "using", "via", "was",
    "were", "with", "without",
    "measures", "measure", "measuring", "total", "current", "value",
    "including", "include", "includes", "other", "different",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_MIN_KEYWORD_LEN = 3
_MAX_KEYWORDS = 12


def _tokenize(text: str) -> list[str]:
    """Lowercases and splits on any run of non-alphanumeric characters
    (so `node_cpu_seconds_total`, "GPU/Memory", and "user, system, idle"
    all split into plain word tokens), dropping stopwords and anything
    shorter than _MIN_KEYWORD_LEN. Purely mechanical -- no semantic
    understanding, no LLM call, matching the frozen requirement that "no
    LLM calls are required for onboarding metadata."
    """
    tokens = _TOKEN_RE.findall(text.lower())
    return [t for t in tokens if len(t) >= _MIN_KEYWORD_LEN and t not in _STOPWORDS]


def generate_keywords(entry: CatalogEntry) -> tuple[str, ...]:
    """Deterministic keyword extraction from the fields the catalog
    already has -- name, category, and help/Purpose text -- in that
    priority order (name-derived tokens first, since a query matching the
    metric name itself is the strongest possible signal; see search.py's
    own weighting, which mirrors this ordering).

    Deduplicates while preserving first-occurrence order, and caps at
    _MAX_KEYWORDS so a long Purpose sentence doesn't dilute the token set
    with low-value words. Never touches `entry.keywords` if it is already
    non-empty (see apply_rules) -- this function is a pure generator, not
    a merge/override policy.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for source in (entry.name, entry.category, entry.help):
        for token in _tokenize(source):
            if token not in seen:
                seen.add(token)
                ordered.append(token)
    return tuple(ordered[:_MAX_KEYWORDS])


# ---- priority classification ----------------------------------------------

# The single canonical category -> priority pattern table (Implementation
# Rule 4: "One source of truth for category/priority patterns. Never
# duplicate regex/pattern lists."). Patterns are matched case-insensitively
# as substrings against `entry.category`, in order; the first match wins.
#
# These are a deliberately small, easily-reviewed starting point -- the
# frozen spec is explicit that "exact scoring weights ... are experimental
# and must be validated rather than guessed" (this applies to priority
# classification the same way it applies to search ranking weights).
# Treat this table as a first draft for the Batch 2 manual-review
# checkpoint ("Start manual inspection of... verify candidates make
# semantic sense"), not a finished taxonomy.
_PRIORITY_PATTERNS: tuple[tuple[str, str], ...] = (
    # (substring to match in category, lowercased) -> priority
    ("reliability", Priority.HIGH.value),
    ("ecc", Priority.HIGH.value),
    ("nvlink health", Priority.HIGH.value),
    ("temperature", Priority.HIGH.value),
    ("power", Priority.HIGH.value),
    ("utilization", Priority.MEDIUM.value),
    ("cpu scheduling activity", Priority.MEDIUM.value),
    ("memory", Priority.MEDIUM.value),
    ("swap", Priority.MEDIUM.value),
    ("filesystem", Priority.MEDIUM.value),
    ("system load", Priority.MEDIUM.value),
    ("clocks", Priority.MEDIUM.value),
    ("pcie", Priority.MEDIUM.value),
    ("nvlink", Priority.MEDIUM.value),
    ("tensor", Priority.MEDIUM.value),
    ("compute", Priority.MEDIUM.value),
)


def classify_priority(entry: CatalogEntry) -> str:
    """Looks up `entry.category` (case-insensitively, substring match)
    against `_PRIORITY_PATTERNS`. Returns `Priority.REVIEW.value` -- the
    schema's own explicit "not yet classified" value -- for any category
    that doesn't match a known pattern, including `UNCATEGORIZED` itself,
    rather than guessing a default High/Medium that would misrepresent an
    un-reviewed metric as already triaged.
    """
    category_lower = entry.category.lower()
    if entry.category == UNCATEGORIZED:
        return Priority.REVIEW.value
    for substring, priority in _PRIORITY_PATTERNS:
        if substring in category_lower:
            return priority
    return Priority.REVIEW.value


# ---- applying rules across a catalog ---------------------------------------


def apply_rules(catalog: Catalog, *, overwrite_existing: bool = False) -> Catalog:
    """Returns a new Catalog with `keywords` and `priority` filled in for
    every entry, via generate_keywords()/classify_priority() above.

    By default (`overwrite_existing=False`), an entry that already has a
    non-empty `keywords` tuple or a `priority` other than `Priority.
    REVIEW` is left untouched -- this makes apply_rules() safe to re-run
    after a human has manually curated an entry's keywords/priority
    (e.g. during the discovered_pending_review review step) without that
    manual work being silently clobbered on the next Phase 5 pass. Pass
    `overwrite_existing=True` only for a deliberate full re-classification
    pass (e.g. after `_PRIORITY_PATTERNS` itself changes).
    """
    updated: list[CatalogEntry] = []
    for entry in catalog.metrics:
        new_keywords = entry.keywords
        if overwrite_existing or not entry.keywords:
            new_keywords = generate_keywords(entry)

        new_priority = entry.priority
        if overwrite_existing or entry.priority == Priority.REVIEW.value:
            new_priority = classify_priority(entry)

        if new_keywords != entry.keywords or new_priority != entry.priority:
            updated.append(replace(entry, keywords=new_keywords, priority=new_priority))
        else:
            updated.append(entry)

    return Catalog(
        catalog_version=catalog.catalog_version,
        generated_at=catalog.generated_at,
        metrics=tuple(updated),
    )
