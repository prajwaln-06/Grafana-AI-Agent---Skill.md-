# Prometheus concept vocabulary (query-plan cache)

The Prometheus query-plan cache reuses a validated PromQL plan for a reworded question. Until now two
wordings counted as the same only if they were literally the same words or the operator declared the
difference in `PROMETHEUS_QUERY_PLAN_CACHE_ALIASES`. The built-in **concept vocabulary**
(`app/prometheus_concepts.py`) removes the need to declare ordinary synonyms by hand:

| These wordings ... | ... become the concept |
|---|---|
| cpu usage, cpu utilization, processor usage, processor utilization | `cpu_utilization` |
| cpu load, cpu load average, load average, system load | `cpu_load` |
| memory usage, memory utilization, memory consumption, ram usage, ram utilization, ram consumption | `memory_utilization` |
| network traffic, network throughput | `network_traffic` |
| disk read rate, disk read throughput | `disk_read_throughput` |
| disk write rate, disk write throughput | `disk_write_throughput` |
| gpu usage, gpu utilization | `gpu_utilization` |
| gpu memory usage, gpu memory utilization, gpu memory consumption | `gpu_memory_utilization` |
| power usage, power consumption, power draw | `power_usage` |

(British "-isation" spellings are included. Plurals and tenses are folded by the matcher's own stemmer.)

## How a concept is produced

1. The question goes through the **same** tokenizer and stemmer as every other signature
   (`opensearch_catalog.query_cache.build_signature`).
2. Per-family aliases are applied first (see below).
3. Auxiliary words are dropped, then the longest matching **phrase** is looked up in the Prometheus phrase
   table and replaced by its concept. Longest first is what keeps "gpu memory usage" from being read as
   "gpu" + host "memory usage".
4. The concept lands in the `concepts` facet of the ordinary six-facet signature
   (operation, entities, concepts, literals, window_seconds, residual).
5. `CPU`, `GPU` and `RAM` are treated as ordinary words in this domain, so `CPU usage` and `cpu usage` are
   the same. Every other ALL-CAPS token (`PROD`, `A7`) stays a case-sensitive literal.

Two wordings are equivalent **only if every facet is equal**. A synonym therefore makes a question
*eligible*; it cannot make two different questions match.

## What is deliberately NOT merged

Matching is phrase-level and domain-aware. A bare `usage`, `utilization`, `load`, `consumption`,
`traffic`, `throughput` or `rate` maps to nothing and stays an unexplained word, which the matcher treats
as a difference. Pinned by tests (`DISTINCT_ON_PURPOSE`):

* `cpu load` vs `cpu utilization` (run-queue length vs time spent busy)
* `memory usage` vs `memory available` (in use vs still free), and vs `gpu memory usage` (host vs device)
* `network traffic` vs `network utilization` (volume vs share of link capacity), and vs direction words
  (`receive`, `transmit`, `rx`, `tx`)
* `disk read ...` vs `disk write ...`
* anything with a different node/environment filter, time window, grouping, aggregation or operation

Adding a concept means adding a `Concept(...)` entry with its phrases, a metric hint and a note on where
its boundary is. The module refuses at import time a phrase that names two concepts, and the tests check
that no phrase collides with the OpenSearch lexicon or swallows a time, compound, operation, entity or
filler word.

## What still decides a hit (nothing here is weakened)

A synonym match is only the first gate. On every hit the cache still requires, in order:

1. six-facet signature equality (literals, time window and every leftover word included);
2. the stored plan was **cross-checked at insertion**: its own question agrees with the query that really ran
   (time window, literals) **and every concept in it is backed by a plan metric** (`cpu_utilization` needs a
   `cpu` metric, `memory_utilization` a `memory` metric, ...). A plan that fails stays exact-wording only;
3. **catalog ownership** (`reference_ambiguity`, unchanged): the plan's reference must be alone on top and the
   plan's metrics still offered. It is asked about the question *plus the canonical wording of its recognised
   concepts*, because the catalog's keyword search does not know that "processor" means CPU (asked about the
   raw words it routes "processor" to the GPU reference and refuses);
4. full revalidation: schema, configuration / skill / catalog-entry / catalog-routing / live-label fingerprints,
   then the real `validator.validate_contract`;
5. **Prometheus is queried again.** Only the plan is cached, never data.

Any failed step evicts the entry and the normal Router/Generator pipeline runs.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `PROMETHEUS_QUERY_PLAN_CACHE_CONCEPTS_ENABLED` | `true` | Use the built-in vocabulary. `false` restores the previous behaviour exactly (only declared aliases bridge wordings). |
| `PROMETHEUS_QUERY_PLAN_CACHE_SEMANTIC_ENABLED` | `true` | Master switch for reworded matching; with `false` the vocabulary is irrelevant (exact wording only). |
| `PROMETHEUS_QUERY_PLAN_CACHE_ALIASES` | `{}` | Optional per-exporter word aliases. Unchanged and backward compatible. |

No `.env` change is needed for the synonyms above.

### Aliases still work, and are applied first

Aliases rewrite single words *before* phrase matching, so they extend or override the vocabulary for one
exporter family (the directory under `skills/references`, e.g. `node-exporter`):

* **Extend:** `{"node-exporter": {"proc": "cpu"}}` makes "proc usage" read "cpu usage", which the vocabulary
  then recognises.
* **Override:** `{"node-exporter": {"load": "utilization"}}` declares that for node-exporter "cpu load" means
  "cpu utilization" (the built-in distinction is dropped for that family only).
* The legacy `{"node-exporter": {"usage": "utilization"}}` keeps working and is harmless alongside the vocabulary.
* An alias declared for one family never applies to another.

## Known limits

* Concepts only help where the catalog has metrics for them. The committed catalog has no node network or
  disk-I/O metrics, so those concepts match at the signature level but no plan exists to reuse yet.
* "processor" is mapped to CPU. In a deployment that also exports GPU metrics a user *could* mean the GPU;
  the vocabulary resolves that as CPU, deterministically. Remove the phrases (or set the switch to `false`)
  if that is not wanted.
* Questions that are compound ("CPU usage **and** memory usage"), contain calendar times, or several
  windows are still never matched semantically.
