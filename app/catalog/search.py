"""
app/catalog/search.py

Phase 6: deterministic retrieval/ranking over a Catalog. Per the frozen
architecture (§2.5 Retrieval):

  "Initial retrieval is deterministic using: metric name; keywords; HELP;
   exporter; category; priority. No embeddings/vector database initially.
   Exact scoring weights and Top-N are experimental and must be validated
   rather than guessed."

This module is NOT wired into Router/SkillIndex yet -- that is explicitly
Phase 9/10 work (Batch 3/4). search() is a standalone function a future
integration point can call; nothing here assumes it is being called from
within the request pipeline.

Weights below (`_WEIGHTS`, `DEFAULT_MIN_SCORE`) are a documented starting
point, not a tuned result -- the frozen spec is explicit that these are
"experimental and must be validated rather than guessed", which is why
Batch 2's central review question ("Does catalogization actually retrieve
the right metrics efficiently?") is flagged High review importance and
Batch 3's manual-testing checkpoint calls for inspecting real
question -> candidate results before this is trusted in the routing path.
Treat every constant in this module as provisional until that checkpoint.

Category and priority are used ONLY as small tie-breaking signals here,
never as filters -- per the frozen rule that they are "retrieval/ranking
signals only" and "must never be hard filters. False positives/over-
inclusion are preferable to false negatives." A `rejected` metric is the
one status this module does exclude by default (see `statuses` parameter)
-- that exclusion is a status-based policy decision (a human explicitly
said "not this metric"), not a category/priority filter, and the frozen
model's own definition of `rejected` is "explicitly excluded."
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.catalog.schema import Catalog, CatalogEntry, CatalogStatus, Priority

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Field weights: how much one query-token match against each field is
# worth. Name and keyword matches are weighted highest since they are the
# most deliberate, curated signal about what a metric is "about"; HELP
# text is weighted lowest since it is free prose most likely to contain
# incidental word overlap with an unrelated query.
_WEIGHTS = {
    "name": 5.0,
    "keywords": 3.0,
    "category": 2.0,
    "exporter": 1.0,
    "help": 1.0,
}

# Priority is a tie-breaking multiplier only -- applied AFTER the additive
# field-match score above, and only distinguishes entries that already
# matched the query on at least one field. It must never let a
# High-priority, zero-relevance entry outrank a genuinely relevant one:
# see _score()'s "only apply if base_score > 0" guard.
_PRIORITY_MULTIPLIER = {
    Priority.HIGH.value: 1.15,
    Priority.MEDIUM.value: 1.05,
    Priority.REVIEW.value: 1.0,
}

DEFAULT_TOP_N = 5

# A catalog "miss" (per §2.6, must fall back to full routing, never be
# treated as "unsupported") is any query with zero field-token overlap
# against every candidate entry. Deliberately not set above zero: any
# floor higher than "some real overlap exists" would be an arbitrary
# guess about how much overlap counts as "relevant enough", which is
# exactly the kind of weight the frozen spec says must be validated, not
# guessed, before it's trusted. Provisional -- see module docstring.
DEFAULT_MIN_SCORE = 0.0

DEFAULT_STATUSES = frozenset(
    {
        CatalogStatus.APPROVED.value,
        CatalogStatus.APPROVED_UNAVAILABLE.value,
    }
)
"""Default status allow-list for normal (query-generation-facing)
retrieval: `approved` + `approved_unavailable` only.

`discovered_pending_review` is deliberately excluded from this default.
Per the frozen status model, a runtime-only metric that has not yet been
vendor-approved must never become automatically eligible for query
generation just because it showed up at runtime with a resolvable type --
that would let an unreviewed metric silently start being routed to and
queried the same as a fully-approved one. `discovered_pending_review`
entries remain IN the catalog (reconciler.py never removes them) purely
for review/discovery purposes -- a reviewer explicitly asking to see
pending-review candidates should pass
`statuses={CatalogStatus.DISCOVERED_PENDING_REVIEW.value}` (or a superset
including it) to `search()`; normal Router-facing retrieval must not."""


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


@dataclass(frozen=True)
class SearchResult:
    entry: CatalogEntry
    score: float


def _field_tokens(entry: CatalogEntry) -> dict[str, set[str]]:
    return {
        "name": _tokenize(entry.name),
        "keywords": set(k.lower() for k in entry.keywords),
        "category": _tokenize(entry.category),
        "exporter": _tokenize(entry.exporter),
        "help": _tokenize(entry.help),
    }


def _score(query_tokens: set[str], entry: CatalogEntry) -> float:
    if not query_tokens:
        return 0.0
    fields = _field_tokens(entry)
    base_score = 0.0
    for field_name, weight in _WEIGHTS.items():
        overlap = query_tokens & fields[field_name]
        if overlap:
            base_score += weight * len(overlap)
    if base_score <= 0.0:
        return 0.0
    return base_score * _PRIORITY_MULTIPLIER.get(entry.priority, 1.0)


def search(
    catalog: Catalog,
    query: str,
    top_n: int = DEFAULT_TOP_N,
    min_score: float = DEFAULT_MIN_SCORE,
    statuses: frozenset[str] | set[str] = DEFAULT_STATUSES,
) -> list[SearchResult]:
    """Deterministically ranks `catalog`'s entries against `query`, using
    exact (case-insensitive, tokenized) overlap against name/keywords/
    category/exporter/help -- no embeddings, no LLM call, per the frozen
    §2.5 requirement.

    Returns results sorted by descending score, ties broken by metric name
    (alphabetical) for fully deterministic, reproducible output -- the
    same query against the same catalog always returns the same ordered
    list. Entries whose status is not in `statuses` (by default: every
    status except `rejected`) are never scored at all. Entries scoring at
    or below `min_score` are omitted -- an EMPTY return value here is a
    genuine catalog miss and, per §2.6, the caller must fall back to the
    existing full routing path, never treat it as "this metric doesn't
    exist" or "unsupported."

    category/priority influence ranking only (priority as a small
    multiplicative tie-breaker, category as one of several additive
    token-overlap fields) -- neither can cause an entry to be excluded
    except via the `statuses` allow-list above, which is a status check,
    not a category/priority filter.
    """
    query_tokens = _tokenize(query)
    scored: list[SearchResult] = []
    for entry in catalog.metrics:
        if entry.status not in statuses:
            continue
        score = _score(query_tokens, entry)
        if score > min_score:
            scored.append(SearchResult(entry=entry, score=score))

    scored.sort(key=lambda r: (-r.score, r.entry.name))
    return scored[:top_n]
