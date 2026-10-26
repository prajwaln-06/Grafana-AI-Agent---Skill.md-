"""
app/catalog/rules.py

Phase 5: deterministic, rule-based generation of the two catalog fields
that every catalog-construction path (vendor.py's
build_catalog_from_vendor_records, the legacy generator.py path, and
reconciler.py's runtime-discovered entries) deliberately leaves at their
explicit "not yet classified" placeholders -- `keywords` (always `()`) and
`priority` (always `Priority.REVIEW`).

These rules are NOT persisted into catalog.json. app/pipeline.py::
_get_catalog runs apply_rules() in memory each time it loads the catalog
(together with the Markdown-Category keyword enrichment from
generator.build_markdown_category_index()), so the committed file stays
pure source data. That is why every entry in the committed catalog.json
shows `"keywords": []` and `"priority": "Review"`.

This module is the frozen architecture's "one source of truth for
category/priority patterns" (Implementation Rule 4): the priority pattern
table (`_PRIORITY_PATTERNS`) and the keyword stopword/tokenization rules
below are defined exactly once, here. No other module should carry its
own copy of a category->priority mapping or a keyword-extraction regex --
generator.py, reconciler.py, and search.py all treat category/priority as
data they read or produce via this module, never patterns they duplicate.

Explicitly NOT this module's job:
  - Assigning `category`. Since the vendor-source cutover, an entry's
    category is the vendor document's own Category text, carried verbatim
    by vendor_docx.py / vendor.py. (The Markdown "- **Category:**" bullet
    is used here only as extra keyword vocabulary, via `markdown_category`
    -- never written to CatalogEntry.category.) That is source-derived data
    a heuristic here should never override.
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


# ---- targeted vocabulary augmentation (Batch 1 / Phase 1) -----------------
#
# Per the frozen architecture's §11: "Do NOT automatically extract every
# Markdown `Use when` token into catalog keywords" (that experiment was
# already tried and measured to increase candidate contamination). The
# alternative the spec calls for instead is "targeted vocabulary
# augmentation for documented concepts where current lexical retrieval is
# weak" -- a small, curated, per-metric addition, not a blanket extraction
# rule.
#
# This table is that curated addition, and it is deliberately NOT copied
# verbatim from the architecture handoff's candidate list -- each entry
# below was chosen by actually running
# `python -m scripts.evaluate_catalog_retrieval` against the real 46-
# question evaluation set (see that script's own docstring for where the
# question set comes from) and inspecting exactly why the expected metric
# scored zero against its expected question. Two categories of gap showed
# up, and both are singular/deterministic tokenization issues, not
# semantic ones:
#
#   1. A plural/singular mismatch between a query token and the only
#      matching keyword search.py's tokenizer produces (e.g. a metric only
#      carries "buffers"/"errors" but the real question says "buffer
#      cache usage"/"error trend" -- exact-token overlap, no stemming, so
#      the singular form never matches the plural one).
#   2. A documented alternate name/phrasing for the same concept that
#      never appears anywhere in the metric's own name/category/help text
#      at all (e.g. "double-precision" for FP64, "hardware degradation"
#      for a retired-pages metric, "link stability" for an NVLink
#      recovery-event counter).
#
# Each addition below is annotated with the specific evaluation-set
# question it was added to fix. `DCGM_FI_PROF_DRAM_ACTIVE` ("memory-bound
# workload") was investigated and deliberately NOT given an addition here:
# it already carries "memory" as a keyword and still loses to node_memory_*
# entries because their literal metric NAME contains "memory" (name is
# search.py's highest-weighted field) -- that is field-weight contention
# between two genuinely memory-related metrics, not a missing-vocabulary
# gap, and per the frozen architecture is exactly the kind of "weight-
# precision problem for a future tuning phase" this targeted pass is not
# meant to solve.
_KEYWORD_AUGMENTATIONS: dict[str, tuple[str, ...]] = {
    # "Double-precision workload"
    "DCGM_FI_PROF_PIPE_FP64_ACTIVE": ("double", "precision"),
    # "Single-precision workload"
    "DCGM_FI_PROF_PIPE_FP32_ACTIVE": ("single", "precision"),
    # "Mixed-precision workload"
    "DCGM_FI_PROF_PIPE_FP16_ACTIVE": ("mixed", "precision"),
    # "Buffer cache usage" -- entry only had the plural "buffers".
    "node_memory_Buffers_bytes": ("buffer",),
    # "Error trend" -- entry only had the plural "errors"; an unrelated
    # NVLink counter's singular "error" keyword was outscoring it.
    "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL": ("error",),
    # "Memory degradation" -- category is "Reliability", with no "memory"
    # or "degradation" token anywhere in name/category/help.
    "DCGM_FI_DEV_RETIRED_SBE": ("memory", "degradation"),
    # "Serious hardware degradation" -- same gap, "hardware"/"degradation".
    "DCGM_FI_DEV_RETIRED_DBE": ("hardware", "degradation"),
    # "Link stability" -- "nvlink" tokenizes as one token, so it never
    # matches a query's separate "link" token, and "stability" doesn't
    # appear anywhere in the entry at all.
    "DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL": ("link", "stability"),
}


def generate_keywords(
    entry: CatalogEntry, markdown_category: str | None = None
) -> tuple[str, ...]:
    """Deterministic keyword extraction from the fields the catalog
    already has -- name, category, and help/Purpose text -- in that
    priority order (name-derived tokens first, since a query matching the
    metric name itself is the strongest possible signal; see search.py's
    own weighting, which mirrors this ordering), followed by this
    metric's targeted vocabulary augmentation (`_KEYWORD_AUGMENTATIONS`
    above), if any.

    `markdown_category` (Batch 2, Option C): the metric's Markdown
    "Category" bullet text, if any -- see
    app.catalog.generator.build_markdown_category_index(), which is how a
    caller obtains this. When provided, its tokens are added as a final,
    additive enrichment stage, AFTER base keyword generation and the
    targeted augmentation table above. This is retrieval vocabulary only:
    `markdown_category` is never written to `entry.category` (which
    remains the vendor category -- see module docstring's Option C
    boundary and app/catalog/vendor.py), and passing None (no Markdown,
    e.g. a vendor-only metric with reference_path=None) is a normal,
    no-op case, not an error.

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
    for token in _KEYWORD_AUGMENTATIONS.get(entry.name, ()):
        if token not in seen:
            seen.add(token)
            ordered.append(token)
    if markdown_category:
        for token in _tokenize(markdown_category):
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


def apply_rules(
    catalog: Catalog,
    *,
    overwrite_existing: bool = False,
    markdown_category_index: dict[str, str] | None = None,
) -> Catalog:
    """Returns a new Catalog with `keywords` and `priority` filled in for
    every entry, via generate_keywords()/classify_priority() above.

    `markdown_category_index` (Batch 2, Option C): optional metric name ->
    Markdown Category text map (see
    app.catalog.generator.build_markdown_category_index()), threaded
    through to generate_keywords() as additive retrieval-keyword
    enrichment for entries whose name has an entry in the index. Omitting
    it (the default) reproduces pre-Option-C behavior exactly -- callers
    that don't care about Markdown-category enrichment (e.g. most
    existing tests) do not need to change.

    By default (`overwrite_existing=False`), an entry that already has a
    non-empty `keywords` tuple or a `priority` other than `Priority.
    REVIEW` is left untouched -- this makes apply_rules() safe to re-run
    after a human has manually curated an entry's keywords/priority
    (e.g. during the discovered_pending_review review step) without that
    manual work being silently clobbered on the next Phase 5 pass. Pass
    `overwrite_existing=True` only for a deliberate full re-classification
    pass (e.g. after `_PRIORITY_PATTERNS` itself changes).
    """
    markdown_category_index = markdown_category_index or {}
    updated: list[CatalogEntry] = []
    for entry in catalog.metrics:
        new_keywords = entry.keywords
        if overwrite_existing or not entry.keywords:
            new_keywords = generate_keywords(
                entry, markdown_category_index.get(entry.name)
            )

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
