"""
prometheus_concepts.py

The built-in Prometheus CONCEPT VOCABULARY for the Prometheus query-plan cache: a fixed, reviewed table
that says which differently worded phrases name the same measurement ("cpu usage", "cpu utilization",
"processor usage" -> `cpu_utilization`), so a reworded question is recognised without an operator having
to list every synonym in PROMETHEUS_QUERY_PLAN_CACHE_ALIASES.

What this module is, and is not
-------------------------------
* It is VOCABULARY only. It plugs into the shared matcher (`opensearch_catalog.query_cache.build_signature`
  through its optional `Vocabulary` argument) and produces ordinary signature CONCEPTS. The six-facet
  equality (operation, entities, concepts, literals, window, residual) is still what decides equivalence,
  so a synonym can only make two questions ELIGIBLE to match; it never makes them match by itself.
* It is isolated. The OpenSearch caches never pass this vocabulary, so nothing here can change their
  matching (tests pin that). The built-in OpenSearch lexicon always wins a collision.
* It is deterministic: a lookup table, stemmed with the matcher's own stemmer. No LLM, no scoring.
* It is DOMAIN-AWARE, not a flat synonym list. Words such as usage / utilization / consumption / load /
  traffic / throughput are only merged INSIDE a phrase naming one measurement of one resource, and the
  phrases that mean something different stay distinct (see DISTINCT_ON_PURPOSE). A bare "usage" or
  "load" maps to nothing: it remains an unexplained word, which the matcher treats as a difference.

Safety added on top of the matcher (all fail closed, none replaces an existing check)
--------------------------------------------------------------------------------------
* `unsupported_concept`: a stored plan is offered to reworded questions only if its metrics agree with
  every concept its own question carries ("cpu_utilization" needs a CPU metric, ...). A plan whose
  question and query disagree stays exact-wording only.
* `ownership_text`: the catalog ownership check (`reference_ambiguity`) is still the unchanged check, but
  it is asked about the question PLUS the canonical wording of the concepts found in it, because the
  catalog's keyword search does not know that "processor" means CPU here. Without that the check would
  (correctly, for the words it sees) route "processor usage" to the GPU reference and no synonym would
  ever hit. It must still put the plan's reference alone on top and still offer every plan metric.
* The master switch PROMETHEUS_QUERY_PLAN_CACHE_CONCEPTS_ENABLED turns the whole vocabulary off, which
  restores the previous behaviour exactly (only declared aliases bridge wordings).

Per-family aliases (PROMETHEUS_QUERY_PLAN_CACHE_ALIASES) are applied BEFORE phrase matching, so they stay
an optional override/extension: an alias rewrites a word first and the rewritten words are then matched
against this table ({"load": "utilization"} makes "cpu load" read "cpu utilization" for that exporter).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.opensearch_catalog import query_cache as _qc


@dataclass(frozen=True)
class Concept:
    name: str                       # canonical concept id; appears in QuestionSignature.concepts
    phrases: tuple[str, ...]        # the wordings that name it (auxiliaries/plurals are folded by the matcher)
    metric_hints: tuple[str, ...]   # a plan for this concept must use a metric containing one of these
    canonical: str                  # the wording the catalog ownership check is also asked about
    note: str = ""                  # why the boundary is where it is


# Phrase-level on purpose. Every phrase names ONE measurement of ONE resource.
CONCEPTS: tuple[Concept, ...] = (
    Concept("cpu_utilization",
            ("cpu usage", "cpu utilization", "cpu utilisation", "processor usage", "processor utilization",
             "processor utilisation"),
            ("cpu",), "cpu utilization",
            "How busy the processors are. NOT load average (a run-queue measure) and NOT per-core / per-mode "
            "breakdowns: those words stay in the signature and make the question different."),
    Concept("cpu_load",
            ("cpu load", "cpu load average", "load average", "system load", "system load average"),
            ("load",), "cpu load average",
            "Load average (node_load1/5/15). Deliberately a different concept from cpu_utilization."),
    Concept("memory_utilization",
            ("memory usage", "memory utilization", "memory utilisation", "memory consumption",
             "ram usage", "ram utilization", "ram utilisation", "ram consumption"),
            ("memory",), "memory utilization",
            "Overall memory in use. NOT available / free / cached / swap / total: those words are not merged."),
    Concept("network_traffic",
            ("network traffic", "network throughput"),
            ("network",), "network traffic",
            "Data moved over network interfaces. NOT 'network utilization' (a share of link capacity), "
            "'bandwidth', errors or drops; direction words (receive, transmit, rx, tx) stay in the signature."),
    Concept("disk_read_throughput",
            ("disk read rate", "disk read throughput"),
            ("disk_read",), "disk read throughput",
            "Disk read volume per time. NOT latency, IOPS or queue depth."),
    Concept("disk_write_throughput",
            ("disk write rate", "disk write throughput"),
            ("disk_writ",), "disk write throughput",
            "Disk write volume per time (read and write are different concepts)."),
    Concept("gpu_utilization",
            ("gpu usage", "gpu utilization", "gpu utilisation"),
            ("gpu_util", "engine_active", "sm_active"), "gpu utilization",
            "How busy the GPUs are. 'gpu memory ...' is its own concept below, matched first (longest phrase)."),
    Concept("gpu_memory_utilization",
            ("gpu memory usage", "gpu memory utilization", "gpu memory utilisation", "gpu memory consumption"),
            ("mem_copy_util", "fb_used", "fb_free", "dram_active"), "gpu memory utilization",
            "Device memory in use; never merged with host memory_utilization."),
    Concept("power_usage",
            ("power usage", "power consumption", "power draw"),
            ("power",), "power usage",
            "Electrical power drawn. Power violations, limits and energy totals are not merged."),
)

# Wordings that are intentionally NOT merged (documented, and pinned by tests). Each is a pair that a
# naive synonym list would equate but that name different measurements.
DISTINCT_ON_PURPOSE: tuple[tuple[str, str, str], ...] = (
    ("cpu load", "cpu utilization", "load average is a run-queue length, utilization is time spent busy"),
    ("cpu usage", "memory usage", "different resources"),
    ("memory usage", "gpu memory usage", "host memory vs device memory"),
    ("network traffic", "network utilization", "volume vs share of link capacity"),
    ("network traffic", "disk read throughput", "different resources"),
    ("disk read throughput", "disk write throughput", "different directions"),
    ("memory usage", "memory available", "in use vs still free"),
    ("usage", "utilization", "bare words are never merged outside a named measurement"),
)

# ALL-CAPS tokens that are concept words in this domain, not data (so "CPU" and "cpu" are the same word).
WORD_ACRONYMS = frozenset({"CPU", "GPU", "RAM"})


def _build() -> tuple[dict[tuple[str, ...], tuple[str, ...]], dict[str, Concept]]:
    table: dict[tuple[str, ...], tuple[str, ...]] = {}
    by_name: dict[str, Concept] = {}
    for c in CONCEPTS:
        if c.name in by_name:
            raise ValueError(f"duplicate Prometheus concept {c.name!r}")
        by_name[c.name] = c
        for phrase in c.phrases:
            key = _qc.phrase_key(*phrase.split())
            if not key:
                raise ValueError(f"empty phrase for concept {c.name!r}")
            if key in table:
                raise ValueError(f"phrase {phrase!r} names more than one concept (ambiguous)")
            table[key] = (c.name,)
    return table, by_name


_TABLE, BY_NAME = _build()
VOCABULARY = _qc.Vocabulary(phrases=tuple(sorted(_TABLE.items())), word_acronyms=WORD_ACRONYMS)


def signature(question: str, alias_items=(), *, enabled: bool = True) -> _qc.QuestionSignature:
    """The question's signature under the Prometheus vocabulary (or the plain shared one when the
    vocabulary is switched off). `alias_items` are the per-family aliases, applied first."""
    return _qc.build_signature(question, alias_items, VOCABULARY if enabled else None)


def concepts_in(sig: _qc.QuestionSignature) -> list[Concept]:
    return [BY_NAME[n] for n in sorted(sig.concepts) if n in BY_NAME]


def unsupported_concept(sig: _qc.QuestionSignature, metrics) -> str | None:
    """None if every Prometheus concept in `sig` is backed by at least one of the plan's metrics, else why
    not. Substring match on the metric name, case-insensitive; an unbacked concept fails closed."""
    names = [m.lower() for m in metrics]
    for c in concepts_in(sig):
        if not any(h in n for n in names for h in c.metric_hints):
            return f"the question's concept {c.name!r} is not supported by the plan's metrics"
    return None


def ownership_text(question: str, alias_items=(), *, enabled: bool = True) -> str:
    """What the catalog ownership check is asked about: the question as worded, plus the canonical wording
    of each concept found in it (so the catalog's keyword search sees "cpu" for "processor"). Without a
    recognised concept, or with the vocabulary off, this is the question unchanged."""
    if not enabled:
        return question
    found = concepts_in(signature(question, alias_items, enabled=True))
    if not found:
        return question
    return question + " " + " ".join(c.canonical for c in found)



def describe() -> dict[str, list[str]]:
    """Concept -> phrases, for docs and the status endpoint."""
    return {c.name: list(c.phrases) for c in CONCEPTS}

