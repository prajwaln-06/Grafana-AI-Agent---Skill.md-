"""
pipeline.py

Orchestrates the full request lifecycle against the observability-query-
builder skill package: Router -> Generator -> deterministic Validator ->
Executor.

Two LLM calls, not three. The previous version of this pipeline spent a
third Gemini call re-checking the Generator's own output against rules that
are almost entirely mechanical (closed enums, required fields, a query
being non-empty, a label key belonging to a fixed discovered list). See
validator.py's module docstring for the full reasoning -- that phase is now
plain Python, which makes it strictly deterministic (same contract always
validates the same way), faster, cheaper, and not dependent on a second
model call succeeding.

  ROUTER     -- SKILL.md Section 6 Steps 1-2 (gate, then route against
                Section 4). ALSO identifies any part of a compound question
                that has no covering reference at all -- see "Partial
                datasource coverage" below.
  GENERATOR  -- SKILL.md Section 6 Steps 3-6 (select metric(s), apply
                Section 8's parameter defaults, construct the query/queries,
                assemble the mode/results shape).                 Given the full content of every matched reference, its sibling
                overview.md (for exporter/domain context), the catalog, the
                relevant *-fundamentals.md file(s),
                Section 7 (error handling -- needed for panic-mode framing),
                and live-discovered label keys / Attributes keys.
  VALIDATOR  -- no LLM. See validator.py.
  EXECUTOR   -- no LLM. See executor.py.

Partial datasource coverage (see also: skills/SKILL.md Section 4's
OpenSearch note, and skills/opensearch-fundamentals.md's status note):
today, only Prometheus-backed domains exist. A question that needs BOTH a
Prometheus measurement and an OpenSearch-backed one (e.g. "show CPU
utilization and recent error logs for node-1") must not have its OpenSearch
half silently dropped, and must not have its Prometheus half blocked just
because the OpenSearch half has nowhere to route yet. The Router's output
carries an `unresolved_topics` list for exactly this: sub-intents of the
question it could not match against Section 4's routing table. This
pipeline resolves whatever DID match normally, then deterministically
appends one `status: "unmapped"` result entry per unresolved topic to the
final response (see `_finalize`) -- so the frontend gets the CPU series it
asked for AND an explicit, structured signal that the logs half isn't
covered yet, rather than one silently swallowing the other. The moment
opensearch-* routing rows exist in SKILL.md, questions that need them start
matching normally and this fallback stops being exercised for them --
nothing about this mechanism is Prometheus/OpenSearch-specific; it's
generic to "a sub-intent this skill package doesn't cover yet."

Alert-rule creation (SKILL.md Section 12, feature-flagged off by default --
see app/config.py's `alert_rule_creation_enabled`): the narrow, explicit
exception to this skill's read-only nature. When the flag is off, the
Router and Generator prompts are built WITHOUT the alert-rule-creation
addenda at all (`_build_router_instructions` / `_build_generator_
instructions` below) -- an alert-creation request is classified exactly as
it always was, `out_of_scope_action`, with zero prompt-level change and
zero regression surface for ordinary read-query classification. When the
flag is on, the Router may tag a request with `action_intent:
"propose_alert_rule"`; this pipeline then deterministically injects the
deployment's default alert folder and forces `datasource_uid` to null
(`_apply_alert_rule_defaults`) before validation, since neither is ever
something the Generator should be trusted to supply itself (Section 12.5).
The resulting `status: "alert_rule_proposed"` result is never auto-executed
(see executor.py) and never creates anything in Grafana on its own -- that
only happens via the separate confirmation endpoint in
app/api/routes_alerts.py, which is this pipeline's sibling, not something
it calls.

Metric catalog integration -- final architecture (Batch 4, Phase 9: this
replaced two migration-only feature flags, `catalog_shadow_mode_enabled`
and `catalog_assisted_routing_enabled`/`catalog_generator_candidates_
enabled`, with unconditional behavior once Batches 1-3 were validated and
approved; see app/config.py's own comment for that history). Ownership
boundary:

    Catalog    = structured retrieval/narrowing over the metric universe
                 (app/catalog/*). Never a semantic knowledge base -- see
                 app/catalog/schema.py's own module docstring.
    Router     = semantic/reference routing (SKILL.md Section 6 Steps
                 1-2). Selects WHICH Markdown reference(s) apply; never
                 selects the final metric.
    Generator  = final metric/query selection (SKILL.md Section 6 Steps
                 3-6), operating within whatever candidate scope
                 `GeneratorContext.metric_metadata` currently offers.
    Validator  = deterministic, non-LLM contract enforcement (see
                 validator.py) -- known-metric, candidate-scope, and
                 catalog-status checks all stay independent and separate.
    Markdown   = semantic knowledge (Use-when guidance, confusable-metric
                 distinctions, query construction rules, domain
                 guardrails) that Catalog metadata deliberately does not
                 and must not replace.

The normal-request data flow:

    Catalog (Level-1 metric candidates + reference paths)
        |
        v
    Router (semantic/reference routing, narrowed to catalog-suggested
            Section 4 rows when the catalog has an opinion)
        |
        v
    semantic Markdown references (Router-matched)
        |
        v
    GeneratorContext.metric_metadata (Level-1 candidate scope)
        |
        v
    Generator (final metric/query selection)

Catalog participation is now MANDATORY for this flow, not optional: `_get_
catalog` is called unconditionally in `run_pipeline` (no settings flag
gates it), Router-level narrowing (`_maybe_narrow_section4`) runs whenever
`catalog is not None`, and Generator-level Level-1 candidate construction
(`_build_metric_metadata`) runs whenever `catalog is not None and
ctx.known_prometheus_metrics`. There is no permanent
`if catalog_enabled: new architecture else: old architecture` branch
anywhere in the normal query pipeline. What IS preserved, deliberately, is
every one of the original safety nets for when a catalog genuinely isn't
available (missing/corrupt catalog.json -- `_get_catalog` returns None and
caches that failure, see its own docstring) or doesn't recognize a
question (a miss, or a hit below `catalog_narrow_min_score`): every one of
those degrades to exactly the pre-catalog behavior (full Section 4, no
candidate block, no Level-1/2/3) for that request, never a crash and never
a blocked request. `known_prometheus_metrics` (and therefore label
discovery) stays deliberately decoupled from all of this -- it is always
the full Router-selected-reference scope, never narrowed by Level-1/2/3;
`metric_metadata` is the current Generator CANDIDATE scope, a strictly
narrower and more volatile idea layered on top.

Catalog status validation (see app/config.py's `catalog_metric_status_
validation_enabled` -- kept as a genuine, independent opt-in capability,
unlike the flags above): when enabled, `_catalog_status_by_metric` hands
validator.py a small {metric_name: catalog_status} lookup alongside
`known_metrics`, so a `discovered_pending_review`/`rejected` catalog
metric can never become a valid query or alert-rule metric even if the
same name also happens to appear in a Metric Directory this request
opened. This is purely additive to `known_metrics` -- see validator.py's
module docstring for the combined policy. The alert-rule-proposal
validation path (`validator._validate_alert_rule_proposed`) enforces the
SAME `known_metrics`/catalog-status policy the ordinary query path always
has.

Catalog-narrowed Generator candidates (Level 1) and the Level-1/Level-2
fallback: when the catalog confidently narrows the question to a
metric-level candidate set, `GeneratorContext.metric_metadata` carries
that set into the Generator's prompt, and `validator.py`'s
`_check_candidate_scope` deterministically enforces it on the resulting
contract -- a real, known metric that simply isn't one of this attempt's
candidates is rejected just as surely as a fabricated one, but
distinguishably so (see `ValidationResult.candidate_scope_violation`).
Exactly one retry is allowed: if the Generator reports `unsupported_metric`
for that candidate set, or the Validator catches an out-of-scope
selection, `run_pipeline` retries once with a Level-2 `GeneratorContext`
(every metric belonging to the Router-matched reference(s), via
`Catalog.by_reference_path()` -- `_build_level2_generator_context`), always a
superset of the Level-1 candidates (which are themselves scoped to those
same reference file(s)) and never broader than `known_prometheus_metrics`.
See `_run_generation_attempt`,
`_should_fallback_to_level2`, and `_build_level2_generator_context` for the
full orchestration; none of this touches the Router or the Generator's own
prompt-construction responsibility for query semantics.

Level 3 -- the final fallback layer: reached in exactly two cases, both
concrete evidence that the Router-selected reference universe itself is
wrong, not merely that the candidate scope within it was too narrow --
(1) the Level-2 attempt (already the broadest metric_metadata that
reference universe can offer) STILL reports `unsupported_metric`, or (2) a
CONFIDENT catalog decision shares zero overlap with `known_prometheus_
metrics` at all (`GeneratorContext.reference_universe_mismatch`), in which
case Level 1/2 are skipped entirely rather than wasted on a reference
universe the catalog itself contradicts. Level 3 (`_run_level3_fallback`)
reuses the existing full routing/search architecture VERBATIM -- it calls
`_run_router` again with `catalog=None`, which (per `_run_router`'s own
`catalog is not None` guard) exposes SKILL.md's Section 4 completely
unnarrowed, exactly as it was before the catalog existed; this is the
pre-catalog implementation invoked again, not a second one. Exactly one
routing pass + one Generator attempt, unconditionally final --
`_build_generator_context` is called with `catalog=None` for this attempt
too, so `metric_metadata` is always empty and there is nothing left to
escalate to, structurally ruling out recursion. An ordinary validation
failure at Level 2 (a fabricated metric, a malformed contract, etc.) is
never a Level-3 trigger on its own -- only `unsupported_metric`
specifically, or the pre-Level-1 reference-universe-mismatch case above.
`_alert_action_intent_gate` and `_no_match_contract` are pure extractions
of the primary routing call's own gate_stop/no-match/alert-flag handling,
shared verbatim by Level 3 so a second, independent Router call is held to
the exact same deterministic checks as the first.

Dependency-aware compound query resolution (opt-in -- see app/config.py's
`dependent_query_resolution_enabled`, default False): a narrow, orthogonal
addition for a compound question where a later part needs the concrete
entity RESOLVED BY an earlier part's already-executed result -- e.g. "which
node has the highest CPU utilization, and how much memory is available on
that node?" The second half has no concrete node until the first half's
query actually runs.

  Flag off (default): `_build_router_instructions`/`_build_generator_
  instructions` add nothing extra to their prompts, `run_pipeline` never
  looks for a dependency plan, and every request behaves exactly as it did
  before this feature existed -- one flat Generator call across every
  matched reference, validated once, returned unexecuted for
  app/agent.py's own executor.execute_contract() call, exactly like today.

  Flag on: the Router prompt gains `_DEPENDENCY_ROUTER_ADDENDUM`, which may
  annotate each `matched_references` entry with an `intent_id` and a
  `depends_on` list of earlier intent_ids. `_extract_dependency_plan`
  deterministically validates this metadata (every entry tagged, every
  `depends_on` id real, no self-dependency, no cycle -- via a plain Kahn's-
  algorithm topological sort) before trusting it at all; ANY defect simply
  returns `None`, and `run_pipeline` falls through to the ordinary flat
  path across the FULL matched set, unchanged. Only a validated, non-empty
  plan (at least one real dependent) reaches `_run_dependency_aware_
  pipeline`, which:

    1. Resolves every ROOT intent (no `depends_on`) in ONE Generator call,
       via the same `_resolve_matched_group` helper the flat path itself
       now uses (Level-1/2/3 catalog cascade included, verbatim -- this
       refactor changes no behavior for the ordinary path; it only gives
       the dependency path a reusable entry point). That call still sees
       the full compound question, so its prompt carries a STAGE SCOPE
       block (`_root_stage_scope_text`) naming exactly which intent(s) and
       reference(s) it owns and that every other part is a later stage's
       job; each dependent call gets the equivalent
       (`_dependent_stage_scope_text`). The Validator cannot enforce this
       scope itself -- its known references/metrics are exporter-wide, so
       a sibling reference's measurement passes validation inside a
       root-only call -- hence the deterministic check in step 2.
    2. If that root result needs a human answer before anything can be
       executed (`ambiguous_metric`, `parameter_requires_clarification`,
       or an alert-rule proposal), nothing is executed and the FULL
       matched set is re-resolved through the ordinary flat path so the
       whole response pauses together (the output contract -- HANDOFF.md's
       `session_id` / `result` pairing -- promises `result` carries no data
       while a clarification/confirmation is pending). That flat result is
       returned only if it still pauses or executes nothing; one that would
       execute is the unfiltered every-instance dependent query this
       feature exists to prevent, and becomes a `validation_failed` result
       instead. If the root stage did not honor its scope -- not exactly
       one result per root, or a result claiming another reference
       (`_index_executed_entries` returning `None`) -- the request ALSO
       fails with `validation_failed`, before anything executes. It is
       never re-resolved through the flat path: that path has no parent
       context and no entity-filter check.
    3. Otherwise executes the root result itself, in-process
       (`executor.execute_contract`), so `_resolve_dependent_entry` can
       read each dependent's parent's real, already-executed data.
    4. Resolves each dependent intent, in topological order, with exactly
       ONE Generator call each (`_run_generator`'s new, purely-additive
       `parent_context_text` parameter carries the parent's resolved
       entity label/value and latest execution value into that one
       prompt -- see `_summarize_parent_entries_for_prompt`). A parent's
       entity is resolved only from a successful result with EXACTLY ONE
       series (`_resolve_parent_entity` -- never an arbitrary pick of
       several); if any parent is unresolved the dependent's Generator is
       not even called. A dependent whose parent produced no usable data,
       whose own generation fails deterministic validation, or whose
       contract is multi-result or names a reference other than its own,
       becomes a plain, pipeline-authored
       `unsupported_metric` entry (`_dependency_unresolved_entry` -- the
       same "inject a fixed, already-valid-shape entry" precedent
       `_unmapped_entry_for` already establishes for unresolved topics),
       never a hard failure of the whole response. A dependent query that
       DOES validate, but whose generated PromQL does not actually retain
       an equality filter on its parent's resolved value
       (`_query_retains_entity_filter`, reusing validator.py's own
       Principle-9 label-equality extraction), is never executed as an
       unfiltered query -- it is returned with a synthetic
       `execution_status: "endpoint_error"` block instead, exactly as any
       other execution-time failure already looks to the frontend.
    5. Reassembles every intent's entry back into the question's original
       order, builds a deterministic (no second LLM call)
       `_build_dependency_synthesis` one-line summary from the combined,
       already-normalized `execution.series` data (best effort only -- an
       entry it can't confidently describe just drops out of the sentence;
       if nothing can be described at all, `synthesis` is `null`, exactly
       like an ordinary multi-result response's default), and marks the
       returned contract with the module-level `ALREADY_EXECUTED_MARKER`
       so app/agent.py knows to skip its own, normally-unconditional
       `executor.execute_contract` call for this one response -- see that
       marker's own docstring.

  `validator.py` needed no functional change for any of this: it already
  accepts a `mode:"multi"` contract with `synthesis: null` as long as the
  key itself is present (see its own `_extract_entries`), which is exactly
  the shape an ordinary (non-dependency) multi-result response has always
  produced -- this feature only ever POPULATES that already-optional field
  with a real string, on the one path that opts into building it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from app import executor, field_discovery, label_discovery, llm_client, validator
from app.catalog import rules as catalog_rules
from app.catalog import search as catalog_search
from app.catalog.generator import build_markdown_category_index
from app.catalog.loader import CatalogLoadError, load_catalog
from app.catalog.schema import Catalog, CatalogSchemaError
from app.config import Settings
from app.skill_index import ROUTING_ROW_RE, SkillIndex

logger = logging.getLogger(__name__)

ALREADY_EXECUTED_MARKER = "_dependency_resolution_already_executed"
# Dependency-aware compound query resolution ONLY (see this module's
# docstring; app/config.py's `dependent_query_resolution_enabled`). A
# private, pipeline-internal signal -- never part of SKILL.md Section 9's
# Output Contract -- set to True on the dict `run_pipeline` returns ONLY
# when `_run_dependency_aware_pipeline` already called
# `executor.execute_contract` itself (it must, to obtain each dependent
# query's parent data before generating it). app/agent.py pops this key
# (it must never reach the frontend) and, when True, skips its own
# otherwise-unconditional post-pipeline executor.execute_contract() call --
# without this, every already-successful query in a resolved dependency
# chain would be re-run against Prometheus/OpenSearch a second time for no
# reason. Absent (the ordinary flat path, which never executes anything
# itself) means app/agent.py's existing behavior is completely unchanged.

INTERNAL_VALIDATION_FAILED_STATUS = "validation_failed"
# NOTE: this status is NOT part of SKILL.md Section 9's enum. It's a
# pipeline-level safety net for the case where the deterministic Validator
# rejects what the Generator (or the Router's gate_stop, or this pipeline's
# own unresolved-topic merging) produced -- SKILL.md deliberately doesn't
# define a status for "the skill's own construction procedure produced
# something that failed a mechanical check," because that's a pipeline-
# implementation concern, not a semantic classification of the user's
# question. The frontend should treat it exactly like `declined` (no data,
# show the explanation) but it's worth logging/alerting on distinctly,
# since a rising rate of these indicates a Router/Generator prompt problem,
# not normal traffic.

ALERT_PROPOSED_STATUS = "alert_rule_proposed"
# SKILL.md Section 9/12. Imported by validator.py, executor.py (by
# omission -- see its module docstring), and app/api/routes_alerts.py.
_ALERT_ACTION_INTENT = "propose_alert_rule"
# The Router's optional Shape B field (see _ALERT_ROUTER_ADDENDUM below)
# that flags a request as alert-rule creation rather than a read question.
# Absent/anything else means an ordinary read question -- this is an
# additive, opt-in field, never a breaking change to Shape B's existing
# consumers.


@dataclass
class PipelineContext:
    """Optional prior-turn context for the clarification/follow-up flow
    (session_store.py populates this from a stored session)."""
    previous_question: str | None = None
    previous_result: dict | None = None
    clarification_answer: str | None = None


async def run_pipeline(question: str, skill_index: SkillIndex, settings: Settings,
                        context: PipelineContext | None = None,
                        catalog: Catalog | None = None) -> dict:
    context = context or PipelineContext()

    # `catalog` is an optional, explicit override (mainly for tests, and for
    # any future admin/reload path); ordinary callers (app/agent.py) never
    # pass it. Otherwise, catalog participation is mandatory for the normal
    # query pipeline (see this module's docstring) -- `_get_catalog` is
    # always called, and a missing/corrupt catalog.json degrades every
    # catalog-dependent behavior below to its pre-catalog equivalent (see
    # `_get_catalog`'s own docstring), never blocks or crashes a request.
    if catalog is None:
        catalog = _get_catalog(settings)

    router_output = await _run_router(question, skill_index, settings, context, catalog=catalog)
    unresolved_topics = _clean_unresolved_topics(router_output.get("unresolved_topics"))

    if router_output.get("gate_stop"):
        contract = _wrap_gate_stop(router_output["gate_stop"])
        return _finalize(contract, unresolved_topics)

    matched = router_output.get("matched_references", [])
    panic_mode = bool(router_output.get("panic_mode", False))
    action_intent = router_output.get("action_intent") or "read_query"

    alert_gate_contract = _alert_action_intent_gate(action_intent, settings, question)
    if alert_gate_contract is not None:
        return _finalize(alert_gate_contract, unresolved_topics)

    if not matched:
        return _finalize(_no_match_contract(question, unresolved_topics), unresolved_topics)

    # Dependency-aware compound query resolution (opt-in -- see this
    # module's docstring and app/config.py's `dependent_query_resolution_
    # enabled`). Never considered at all for an alert-rule-creation request
    # (SKILL.md Section 12 alert-specific properties never belong in this
    # kind of cross-intent data flow -- see that section's own scope note);
    # `_extract_dependency_plan` itself is the deterministic gate for
    # everything else (absent/incomplete/cyclic dependency metadata ->
    # `None` -> falls straight through to the exact same flat path below).
    if settings.dependent_query_resolution_enabled and action_intent != _ALERT_ACTION_INTENT:
        plan = _extract_dependency_plan(matched)
        if plan is not None:
            return await _run_dependency_aware_pipeline(
                question, matched, plan, panic_mode, action_intent, unresolved_topics,
                skill_index, settings, context, catalog,
            )

    return await _resolve_matched_group(
        question, matched, panic_mode, action_intent, unresolved_topics, skill_index, settings, context, catalog,
    )


async def _resolve_matched_group(question: str, matched: list[dict], panic_mode: bool, action_intent: str,
                                  unresolved_topics: list[dict], skill_index: SkillIndex, settings: Settings,
                                  context: PipelineContext, catalog: Catalog | None,
                                  stage_scope_text: str | None = None) -> dict:
    """The full Generator -> Level-1/2/3 catalog cascade -> deterministic-
    Validator pipeline for one group of matched references, resolved as a
    SINGLE flat Generator call (plus its Level-2/Level-3 retries, per this
    module's docstring). This is a pure extraction of what used to be the
    back half of `run_pipeline` itself -- `run_pipeline`'s own ordinary
    (non-dependency) path calls this with the FULL matched set, exactly as
    before, so this refactor changes no behavior for it whatsoever.
    `_run_dependency_aware_pipeline` additionally calls this with just a
    dependency plan's ROOT matched references, to resolve every root intent
    in one call before generating any dependent intent."""
    generator_context = _build_generator_context(matched, skill_index, settings, question=question, catalog=catalog)

    if generator_context.reference_universe_mismatch:
        # Batch 3 (Phase 5): a confident catalog decision shares zero
        # overlap with this reference universe's known_prometheus_metrics
        # -- ordinary Level 1/2 (which only ever broadens WITHIN this same
        # reference universe) cannot recover this by construction, so skip
        # straight to Level 3 (a fresh, catalog-unnarrowed routing pass)
        # rather than spending a Level-1/Level-2 attempt on a reference
        # universe the catalog itself disagrees with.
        logger.info(
            "catalog_generator_candidates: reference-universe mismatch detected for question "
            "%r -- skipping Level 1/2 and going directly to Level 3.", question,
        )
        return await _run_level3_fallback(question, skill_index, settings, context, catalog)

    known_references = set(generator_context.reference_texts.keys()) | set(generator_context.overview_texts.keys())
    known_datasources = {(m.get("data_source") or "").strip().lower() for m in matched}

    # Phase 12 (Batch 4): catalog status is a small, additive lookup table
    # handed to the Validator alongside known_metrics -- never the whole
    # Catalog object, and never a replacement for known_metrics itself. Only
    # built (and the catalog only loaded at all, see the gate above) when
    # settings.catalog_metric_status_validation_enabled is True; otherwise
    # this stays None and validator.py's catalog-status check is a no-op,
    # identical to before Phase 12 existed.
    catalog_status_by_metric = None
    if settings.catalog_metric_status_validation_enabled and catalog is not None:
        catalog_status_by_metric = _catalog_status_by_metric(catalog)

    # known_references/known_datasources/known_metrics/labels_by_metric are
    # ALL identical across a Level-1 attempt and its Level-2 retry (Batch 2
    # decision: Level 2 expands metric_metadata only, never
    # known_prometheus_metrics/labels_by_metric) -- computed once, above,
    # and reused for both attempts below.
    validator_kwargs = dict(
        known_metrics=generator_context.known_prometheus_metrics,
        labels_by_metric=generator_context.labels_by_metric,
        label_values_by_metric=generator_context.label_values_by_metric,
        known_references=known_references,
        known_datasources=known_datasources,
        catalog_status_by_metric=catalog_status_by_metric,
    )

    level1_candidate_scope = {m["name"] for m in generator_context.metric_metadata} or None
    contract, merged, validated = await _run_generation_attempt(
        question, matched, panic_mode, generator_context, skill_index, settings, context, action_intent,
        unresolved_topics, candidate_metric_scope=level1_candidate_scope, validator_kwargs=validator_kwargs,
        stage_scope_text=stage_scope_text,
    )

    # Batch 2 (Phase 4): Level-1 -> Level-2 fallback. Only ever considered
    # when Level 1 was actually active for this request (a non-empty
    # candidate scope was given to the Generator) -- a deployment with no
    # catalog available at all, or a Level-1 catalog miss/low-confidence
    # result, already behaves exactly like "no candidate scope", so there
    # is nothing to fall back FROM in that case; retrying would just
    # repeat the same unconstrained attempt.
    if level1_candidate_scope and _should_fallback_to_level2(merged, validated):
        logger.info(
            "catalog_generator_candidates: Level-1 candidate scope %s was insufficient for "
            "question %r (%s) -- retrying once with a Level-2 (all metrics belonging to the "
            "Router-matched reference(s)) GeneratorContext.",
            sorted(level1_candidate_scope), question,
            "Generator reported unsupported_metric" if validated.passed else "candidate_scope_violation",
        )
        level2_context = _build_level2_generator_context(generator_context, matched, catalog)
        contract, merged, validated = await _run_generation_attempt(
            question, matched, panic_mode, level2_context, skill_index, settings, context, action_intent,
            unresolved_topics, candidate_metric_scope=None, validator_kwargs=validator_kwargs,
            stage_scope_text=stage_scope_text,
        )

        # Batch 3 (Phase 5): Level-2 -> Level-3 fallback. The ONLY trigger:
        # the Generator's OWN Level-2 output (already the broadest
        # metric_metadata this reference universe can offer) still reports
        # unsupported_metric -- concrete evidence the request needs a
        # reference universe Level 1/2 never had access to at all, not
        # merely a candidate-scope problem within the one already
        # selected. An ordinary Level-2 validation failure for any OTHER
        # reason (fabricated metric, malformed contract, etc.) is NOT a
        # Level-3 trigger -- per the frozen architecture, "ordinary
        # validation failures are not automatically Level-3 routing
        # failures" -- and is simply returned as the final result below,
        # exactly as any Generator/Validator failure always has been.
        # `candidate_scope_violation` cannot occur at Level 2 at all
        # (candidate_metric_scope=None above is unconditionally fail-open
        # -- see validator.py's `_check_candidate_scope`), so
        # `_contract_reports_unsupported_metric` is the only signal to
        # check here.
        if _contract_reports_unsupported_metric(contract):
            logger.info(
                "catalog_generator_candidates: Level 2 was still insufficient for question %r -- "
                "falling back to Level 3 (re-running the existing full, catalog-unnarrowed "
                "routing/search architecture once).", question,
            )
            return await _run_level3_fallback(question, skill_index, settings, context, catalog)

    return _package_validated(merged, validated)


def _alert_action_intent_gate(action_intent: str, settings: Settings, question: str) -> dict | None:
    """Defense in depth, not the primary control: with the feature flag
    off, `_build_router_instructions` never tells the Router this field
    exists at all, so the Router should never produce
    `action_intent == "propose_alert_rule"` in the first place. If it
    somehow does anyway (a stale prompt cache, a Router that ignored its
    instructions, etc.), fail closed to the EXACT same behavior this
    request would have gotten before SKILL.md Section 12 existed --
    `out_of_scope_action` -- rather than trusting an LLM-supplied flag the
    deployment hasn't opted into. Returns None (no override needed) for
    every other case.

    A pure extraction of `run_pipeline`'s pre-Batch-3 inline check (no
    behavior change for the primary Router call) so Level 3's own fresh
    routing pass (`_run_level3_fallback`) can apply the exact same
    defense-in-depth to ITS `action_intent`, rather than trusting a
    second, independent Router call any more than the first one.
    """
    if action_intent != _ALERT_ACTION_INTENT or settings.alert_rule_creation_enabled:
        return None
    logger.warning(
        "Router returned action_intent=%r for question %r but "
        "alert_rule_creation_enabled is False on this deployment -- forcing a "
        "deterministic out_of_scope_action response instead of trusting the Router. "
        "This should not happen while the flag is off (the addendum that even "
        "mentions this field is never included in the Router's prompt); investigate "
        "if this recurs.", action_intent, question,
    )
    return _wrap_gate_stop({
        "status": "out_of_scope_action",
        "requested_action": "create a new Grafana alert rule",
        "explanation": (
            "Alert-rule creation is currently disabled on this deployment; this skill "
            "only constructs/runs read-only queries."
        ),
    })


def _no_match_contract(question: str, unresolved_topics: list[dict]) -> dict:
    """The 'Router returned no matched references' contract-construction
    logic -- a pure extraction of `run_pipeline`'s pre-Batch-3 inline
    logic (no behavior change for the primary Router call), shared with
    Level 3's own fresh routing pass (`_run_level3_fallback`), since a
    catalog-unnarrowed routing call can independently land here too."""
    if unresolved_topics:
        # The whole question consists entirely of unresolved sub-intents
        # (every part needs a datasource/domain this skill package
        # doesn't cover yet). Use an empty multi-mode accumulator as the
        # base so `_finalize` -> `_merge_unresolved_topics` builds the
        # entries list purely from `unresolved_topics` -- one entry per
        # topic, collapsing to mode:"single" automatically if there's
        # only one (see `_merge_unresolved_topics`). Do NOT also fold
        # these topics into a separate synthetic "nothing matched"
        # explanation here -- that would describe the same gap twice.
        return {"mode": "multi", "results": [], "synthesis": None}
    # True Router/prompt problem: no gate_stop, no matched reference, AND
    # no unresolved_topics describing why. Nothing here to guess through --
    # fall back to a generic unmapped response and log loudly, since a
    # rising rate of this specific path means the Router's prompt needs
    # attention.
    logger.warning(
        "Router returned zero matched_references, zero unresolved_topics, and no "
        "gate_stop for question %r. This is a Router/prompt problem, not a case to "
        "guess through -- falling back to a generic unmapped response.", question,
    )
    return _wrap_gate_stop({"status": "unmapped", "explanation": _unmapped_explanation()})


async def _run_level3_fallback(question: str, skill_index: SkillIndex, settings: Settings,
                                context: PipelineContext, catalog: Catalog | None) -> dict:
    """Batch 3 (Phase 5): the final fallback layer -- reached only when a
    Level-2 attempt (already the broadest metric_metadata its reference
    universe can offer) still reports `unsupported_metric`, or when a
    confident catalog decision shares zero overlap with the current
    reference universe at all (`GeneratorContext.reference_universe_
    mismatch` -- see `run_pipeline`). Both are concrete evidence that the
    request needs a DIFFERENT reference universe than the one originally
    selected, not merely a broader candidate scope within it.

    Reuses the EXISTING full routing/search architecture verbatim --
    `_run_router` called with `catalog=None`, which (per `_run_router`'s
    own `catalog is not None` guard) exposes SKILL.md's Section 4
    completely unnarrowed, exactly as it always was before the catalog
    existed. This is not a second routing implementation; it is the
    pre-catalog implementation, invoked again.

    Exactly one routing pass + one Generator attempt, unconditionally
    final. This function never calls itself and never re-checks
    `_should_fallback_to_level2`/re-derives a Level-1 candidate scope for
    its own attempt: `_build_generator_context` is called here with
    `catalog=None`, which structurally guarantees `metric_metadata` is
    always `()` for this attempt (see `_build_generator_context`'s own
    `catalog is not None` guard) -- so even if this attempt's Generator
    output ALSO happens to say `unsupported_metric`, there is no
    candidate scope for it to have violated and nothing left in this
    function to escalate to. The result -- success, unsupported_metric
    again, or a validation failure -- is unconditionally returned as
    final.

    Reuses `_alert_action_intent_gate`/`_no_match_contract` (the same
    helpers the primary routing call uses) so a fresh gate_stop, a
    disabled-alert-creation misfire, or a genuine no-match from THIS
    routing pass are all handled identically to how the primary pass
    would have handled them -- Level 3 is a full re-run of the same
    control flow, not a stripped-down parallel path.
    """
    router_output = await _run_router(question, skill_index, settings, context, catalog=None)
    unresolved_topics = _clean_unresolved_topics(router_output.get("unresolved_topics"))

    if router_output.get("gate_stop"):
        return _finalize(_wrap_gate_stop(router_output["gate_stop"]), unresolved_topics)

    matched = router_output.get("matched_references", [])
    panic_mode = bool(router_output.get("panic_mode", False))
    action_intent = router_output.get("action_intent") or "read_query"

    alert_gate_contract = _alert_action_intent_gate(action_intent, settings, question)
    if alert_gate_contract is not None:
        return _finalize(alert_gate_contract, unresolved_topics)

    if not matched:
        return _finalize(_no_match_contract(question, unresolved_topics), unresolved_topics)

    # catalog=None: this attempt gets no metric_metadata at all (see the
    # docstring above) -- Level 3 is a single unconstrained attempt, never
    # its own Level-1/Level-2 cascade.
    generator_context = _build_generator_context(matched, skill_index, settings, question=question, catalog=None)
    known_references = set(generator_context.reference_texts.keys()) | set(generator_context.overview_texts.keys())
    known_datasources = {(m.get("data_source") or "").strip().lower() for m in matched}

    # Phase 12 (Batch 4) catalog status is orthogonal to Level 1/2/3 and
    # stays active here if the deployment has it enabled -- Level 3 must
    # not accidentally bypass an existing, independent safety check.
    catalog_status_by_metric = None
    if settings.catalog_metric_status_validation_enabled and catalog is not None:
        catalog_status_by_metric = _catalog_status_by_metric(catalog)

    validator_kwargs = dict(
        known_metrics=generator_context.known_prometheus_metrics,
        labels_by_metric=generator_context.labels_by_metric,
        label_values_by_metric=generator_context.label_values_by_metric,
        known_references=known_references,
        known_datasources=known_datasources,
        catalog_status_by_metric=catalog_status_by_metric,
    )
    _, merged, validated = await _run_generation_attempt(
        question, matched, panic_mode, generator_context, skill_index, settings, context, action_intent,
        unresolved_topics, candidate_metric_scope=None, validator_kwargs=validator_kwargs,
    )
    return _package_validated(merged, validated)


# ---- Dependency-aware compound query resolution (opt-in) -----------------------
#
# See this module's top-level docstring for the full design note. Every
# function below is reached ONLY via `run_pipeline`'s own
# `settings.dependent_query_resolution_enabled` check, and only after
# `_extract_dependency_plan` has deterministically validated the Router's
# dependency metadata -- there is no path into any of this from the
# ordinary flat pipeline.

_STABLE_ENTITY_LABEL_PRIORITY = ("node_id", "device", "gpu", "instance")
# Preference order for which label key to treat as "the resolved entity"
# when a parent result's series carries more than one label. Stable,
# semantically-named keys are preferred over incidental ones (an
# `instance` value like "node-7:9100" is a live scrape target, not
# necessarily the same join key a sibling metric exposes) -- falls back to
# the first label present if none of these are.

_PERCENT_EXPR_RE = re.compile(r"\*\s*100\b")
_BYTES_NAME_HINT_RE = re.compile(r"bytes", re.IGNORECASE)


@dataclass(frozen=True)
class DependencyPlan:
    """The output of `_extract_dependency_plan`: a validated, acyclic
    dependency graph over one question's `matched_references`. `roots` are
    every intent with no (or an empty) `depends_on`; `dependent_order` is
    every remaining intent, topologically sorted so that by the time any
    entry is processed, every intent it `depends_on` has already been
    resolved. `by_intent_id` is a convenience lookup used throughout this
    section instead of re-scanning `roots`/`dependent_order` repeatedly."""
    roots: list[dict]
    dependent_order: list[dict]
    by_intent_id: dict[str, dict]


def _extract_dependency_plan(matched: list[dict]) -> DependencyPlan | None:
    """Deterministically validates the Router's optional `intent_id`/
    `depends_on` annotations on `matched_references` (see
    `_DEPENDENCY_ROUTER_ADDENDUM`). Returns `None` -- meaning "no usable
    dependency plan; the caller must fall through to the ordinary flat
    path" -- for every one of the following, each logged so a genuine
    Router/prompt problem is visible without ever blocking the request:

      - No entry declares a non-empty `depends_on` at all (the ordinary,
        overwhelmingly common case -- most questions have no dependency).
      - Any entry is missing a usable string `intent_id`, or two entries
        share one (incomplete/ambiguous tagging).
      - Any `depends_on` entry is not a list, references an unknown
        `intent_id`, or names its own entry (self-dependency).
      - The dependency graph contains a cycle (detected via a plain Kahn's-
        algorithm topological sort below; a cycle means the sort cannot
        place every intent, which is exactly the failure signal this
        checks for -- no separate cycle-detection pass is needed).

    A non-`None` return is guaranteed acyclic and complete: every entry in
    `dependent_order` depends only on intents that appear earlier in
    `roots + dependent_order`.
    """
    if not matched or not any(isinstance(m, dict) and m.get("depends_on") for m in matched):
        return None

    intent_ids: list[str] = []
    by_id: dict[str, dict] = {}
    for m in matched:
        intent_id = m.get("intent_id") if isinstance(m, dict) else None
        if not intent_id or not isinstance(intent_id, str):
            logger.warning(
                "dependent_query_resolution: at least one matched reference declared a "
                "dependency, but %r is missing a usable string intent_id -- falling back "
                "to the flat path for this question.", m,
            )
            return None
        if intent_id in by_id:
            logger.warning(
                "dependent_query_resolution: duplicate intent_id %r across matched_references "
                "-- falling back to the flat path for this question.", intent_id,
            )
            return None
        by_id[intent_id] = m
        intent_ids.append(intent_id)

    for intent_id in intent_ids:
        deps = by_id[intent_id].get("depends_on") or []
        if not isinstance(deps, list):
            logger.warning(
                "dependent_query_resolution: depends_on for intent %r is not a list (%r) -- "
                "falling back to the flat path for this question.", intent_id, deps,
            )
            return None
        for dep in deps:
            if dep == intent_id:
                logger.warning(
                    "dependent_query_resolution: intent %r depends on itself -- falling back "
                    "to the flat path for this question.", intent_id,
                )
                return None
            if dep not in by_id:
                logger.warning(
                    "dependent_query_resolution: intent %r declares depends_on %r, which is not "
                    "a known intent_id in this question -- falling back to the flat path.",
                    intent_id, dep,
                )
                return None

    # Kahn's algorithm: repeatedly resolve any intent whose dependencies
    # are already all resolved. If a full pass makes no progress before
    # every intent is resolved, the remainder forms a cycle.
    resolved: set[str] = set()
    dependent_order_ids: list[str] = []
    progressed = True
    while progressed and len(resolved) < len(intent_ids):
        progressed = False
        for intent_id in intent_ids:
            if intent_id in resolved:
                continue
            deps = by_id[intent_id].get("depends_on") or []
            if all(dep in resolved for dep in deps):
                resolved.add(intent_id)
                if deps:
                    dependent_order_ids.append(intent_id)
                progressed = True

    if len(resolved) != len(intent_ids):
        logger.warning(
            "dependent_query_resolution: dependency cycle detected among intents %s -- "
            "falling back to the flat path for this question.", intent_ids,
        )
        return None

    if not dependent_order_ids:
        # Every entry technically has an intent_id, but nothing actually
        # has a non-empty depends_on (can only happen if the loop above's
        # early `any(...)` check somehow disagreed with this one -- kept
        # as a defensive no-op path, never expected to trigger in
        # practice).
        return None

    roots = [by_id[i] for i in intent_ids if not (by_id[i].get("depends_on") or [])]
    dependent_order = [by_id[i] for i in dependent_order_ids]
    return DependencyPlan(roots=roots, dependent_order=dependent_order, by_intent_id=by_id)


def _contract_needs_pause(contract: dict) -> bool:
    """True if `contract` (or, for mode:'multi', any of its entries) needs
    a human answer before anything may be executed -- `ambiguous_metric`,
    `declined`/`parameter_requires_clarification`, or an alert-rule
    proposal (defensive only; `run_pipeline` never reaches the dependency
    path for an alert-rule-creation request in the first place). Mirrors
    app/agent.py's own `_needs_clarification`/`_needs_confirmation`
    predicates exactly, kept as an independent, pipeline-local copy rather
    than a shared import so app/pipeline.py never needs to import
    app/agent.py (which already imports app/pipeline.py)."""
    def _entry_needs_pause(entry: dict) -> bool:
        status = entry.get("status")
        if status == "ambiguous_metric":
            return True
        if status == "declined" and entry.get("reason") == "parameter_requires_clarification":
            return True
        if status == ALERT_PROPOSED_STATUS:
            return True
        return False

    if contract.get("mode") == "multi":
        return any(_entry_needs_pause(e) for e in contract.get("results", []) if isinstance(e, dict))
    return _entry_needs_pause(contract)


def _index_executed_entries(executed_contract: dict, roots: list[dict]) -> dict[str, dict] | None:
    """Maps each root intent's `intent_id` to its own executed entry, by
    POSITION: `_resolve_matched_group`'s single Generator call decides for
    itself how many result objects a group of matched references resolves
    to (SKILL.md Section 6 Step 3), so entries are not independently
    tagged with an intent_id the way `matched_references` are. For the
    overwhelmingly common case this feature targets -- each root intent
    resolving to exactly one result object, in the same order the roots
    were given to the Generator -- positional mapping is exact. If the
    entry count doesn't match the root count at all, this returns `None`
    rather than guess at a mapping that could silently attribute the wrong
    data to the wrong dependent.

    Positional mapping is also VERIFIED, not just assumed: any entry that
    names a `reference_used` must name exactly the reference its positional
    root was routed to. (The Validator cannot check this on its own -- its
    `known_references`/`known_metrics` are exporter-wide, so an entry for a
    sibling reference's measurement, e.g. memory.md's, can still pass
    validation inside a cpu.md-only root call.)

    A `None` return means the root stage did not honor its scope; the caller
    (`_run_dependency_aware_pipeline`) fails the request with a
    deterministic `validation_failed` result -- it never falls back to the
    flat path, which would resolve the dependent part with no parent
    context and no entity filter (see that function's docstring). Callable
    on either the pre-execution or the executed root contract (execution
    preserves entry order and count)."""
    entries = executed_contract["results"] if executed_contract.get("mode") == "multi" else [executed_contract]
    if len(entries) != len(roots):
        logger.warning(
            "dependent_query_resolution: root Generator call produced %d result object(s) for "
            "%d root intent(s) -- can't confidently attribute results to intents.",
            len(entries), len(roots),
        )
        return None
    for root, entry in zip(roots, entries):
        used = entry.get("reference_used") if isinstance(entry, dict) else None
        if used is not None and used != root.get("reference_path"):
            logger.warning(
                "dependent_query_resolution: root intent %r was routed to %r but its generated "
                "result claims reference_used=%r -- can't attribute it to this intent.",
                root.get("intent_id"), root.get("reference_path"), used,
            )
            return None
    return {root["intent_id"]: entry for root, entry in zip(roots, entries)}


def _dependency_stage_failure(reason: str) -> dict:
    """Deterministic, pipeline-authored failure for a dependency-resolution
    request whose staged resolution could not be completed SAFELY. Same
    shape/status as `_package_validated`'s validation failure (the frontend
    treats `validation_failed` exactly like `declined` -- HANDOFF.md 4.1) --
    deliberately NOT a silent re-resolution through the flat path, which
    would answer the dependent part with an unfiltered, every-instance
    query and present it as the answer."""
    logger.warning("dependent_query_resolution: failing request safely: %s", reason)
    return {"mode": "single", "status": INTERNAL_VALIDATION_FAILED_STATUS,
            "explanation": f"Dependent query resolution could not complete safely: {reason}"}


def _contract_has_executable_entry(contract: dict) -> bool:
    entries = contract.get("results", []) if contract.get("mode") == "multi" else [contract]
    return any(isinstance(e, dict) and e.get("status") in executor.EXECUTABLE_STATUSES for e in entries)


def _root_stage_scope_text(roots: list[dict]) -> str:
    """Prompt block for the ROOT-stage Generator call. The call still sees
    the full compound question (it needs it for constraints like
    "highest"), so it must be told, explicitly, which intent(s) it owns and
    that the rest is a LATER stage's job -- otherwise SKILL.md Step 3a/3e/6
    ("identify ALL requested measurements") leads it to answer the
    dependent half too."""
    lines = "\n".join(f"- intent {r.get('intent_id')!r} -> {r.get('reference_path')}" for r in roots)
    n = len(roots)
    shape = (
        'mode "single" with that result\'s fields inline (never mode "multi")' if n == 1 else
        f'mode "multi" with exactly {n} entries in `results`, in the order listed, and `synthesis: null`'
    )
    return (
        "STAGE SCOPE -- ROOT STAGE of dependent query resolution. The question above is COMPOUND, but in "
        "THIS call you resolve ONLY the following measurement intent(s), each already routed to the "
        f"reference shown:\n{lines}\n"
        "Every other part of the question (e.g. a later part that refers back to \"that node\") depends on "
        "your result and is resolved in a LATER, separate stage after your query has actually run. Do NOT "
        "build, mention, or return a result for any such part -- not even an unfiltered placeholder. "
        f"Return exactly {n} result object(s), one per intent listed, as {shape}. Each result's "
        "`reference_used` MUST be exactly the reference path listed for its intent."
    )


def _dependent_stage_scope_text(dep_matched: dict) -> str:
    """Prompt block for a DEPENDENT-stage Generator call (see
    `_root_stage_scope_text` for why this is needed)."""
    return (
        "STAGE SCOPE -- DEPENDENT STAGE of dependent query resolution. The question above is COMPOUND, but in "
        f"THIS call you resolve ONLY intent {dep_matched.get('intent_id')!r} -> {dep_matched.get('reference_path')}. "
        "The earlier part(s) of the question were already resolved and executed (see the upstream block below, "
        "if any) -- do NOT build a result for them again. Return exactly ONE result object, mode \"single\" "
        "(never mode \"multi\"), with `reference_used` exactly "
        f"{dep_matched.get('reference_path')!r}."
    )


def _resolve_parent_entity(parent_id: str, entry: dict | None) -> tuple[tuple[str, str, object] | None, str | None]:
    """Deterministically extracts the ONE entity a parent's already-executed
    result is about, as `((label_key, label_value, latest_value), None)`; or
    `(None, <prompt line explaining why not>)` when it can't be -- no entry,
    non-executable/unsuccessful execution, no series, no labels, or MORE
    THAN ONE series.

    More than one series is deliberately unresolvable: a Prometheus result's
    series order is arbitrary and unsorted (normalizer.py preserves it), so
    taking `series[0]` of several would silently pick an arbitrary entity as
    "the" winner. The root is asked to build a `topk(1, ...)` instant query
    (`_DEPENDENCY_GENERATOR_ADDENDUM`), but that is only a prompt
    instruction -- this is the mechanical enforcement of it."""
    if not entry:
        return None, f"- Upstream intent '{parent_id}': no result is available for it at all."
    execution = entry.get("execution") or {}
    status = entry.get("status")
    if status not in executor.EXECUTABLE_STATUSES or execution.get("execution_status") != "success":
        return None, (
            f"- Upstream intent '{parent_id}': did not produce usable data "
            f"(status={status!r}, execution_status={execution.get('execution_status')!r}). "
            "No entity value could be resolved from it automatically."
        )
    series = execution.get("series") or []
    if not series:
        return None, f"- Upstream intent '{parent_id}': produced no series to resolve an entity value from."
    if len(series) > 1:
        return None, (
            f"- Upstream intent '{parent_id}': returned {len(series)} series, so no single entity could "
            "be identified from it -- an entity is never picked arbitrarily from several."
        )
    labels = series[0].get("labels") or {}
    key = next((k for k in _STABLE_ENTITY_LABEL_PRIORITY if k in labels), next(iter(labels), None))
    if key is None:
        return None, f"- Upstream intent '{parent_id}': its result series carries no labels to join on."
    latest_point = (series[0].get("points") or [{}])[-1]
    return (key, labels[key], latest_point.get("value")), None


def _summarize_parent_entries_for_prompt(parent_ids: list[str],
                                          parent_entries: list[dict | None]) -> tuple[dict[str, str], str | None]:
    """Builds the `parent_context_text` block fed to a dependent intent's
    single Generator call (see `_run_generator`'s `parent_context_text`
    parameter and `_DEPENDENCY_GENERATOR_ADDENDUM`), and separately
    returns the resolved {label_key: value} mapping used by this module's
    own deterministic `_query_retains_entity_filter` safety guard. Reads
    ONLY already-normalized `execution.series` data (see normalizer.py) --
    never re-queries anything itself. A parent that didn't execute
    successfully, or whose series carry no labels at all, is described in
    the prompt as unresolved rather than silently omitted, so the
    dependent Generator call can make an informed `unsupported_metric`
    decision instead of guessing."""
    lines: list[str] = []
    resolved_values: dict[str, str] = {}
    for parent_id, entry in zip(parent_ids, parent_entries):
        resolved, unresolved_line = _resolve_parent_entity(parent_id, entry)
        if resolved is None:
            lines.append(unresolved_line)
            continue
        key, value, latest_value = resolved
        resolved_values[key] = value
        lines.append(
            f"- Upstream intent '{parent_id}' resolved: {key}=\"{value}\" (its latest value was "
            f"{latest_value!r}). The query you build below MUST filter on "
            f"{key}=\"{value}\" explicitly -- never construct this as an unfiltered query across "
            "every instance the metric reports for."
        )

    if not lines:
        return resolved_values, None

    note = (
        "This measurement is a DEPENDENT part of a compound question -- its concrete entity "
        "value(s) were resolved from an earlier, already-executed part of the SAME question:\n"
        + "\n".join(lines)
    )
    return resolved_values, note


def _query_retains_entity_filter(promql: str, resolved_values: dict[str, str]) -> bool:
    """Deterministic (no LLM), pipeline-internal safety guard specific to
    the dependency-resolution path: confirms a dependent's generated
    PromQL actually filters on every entity value resolved from its
    parent's already-executed result, rather than trusting the Generator's
    own compliance with `_DEPENDENCY_GENERATOR_ADDENDUM`'s instruction to
    do so. Reuses validator.py's own Principle-9 label-equality extraction
    (`_extract_promql_label_equalities`) rather than a second regex, so
    this can never disagree with how the rest of the pipeline already
    reads label selectors out of a PromQL string. Vacuously true when no
    entity values were resolved at all (nothing to check against)."""
    if not resolved_values:
        return True
    equalities = validator._extract_promql_label_equalities(promql)
    present_values = {value for (_key, op, value) in equalities if op == "="}
    return all(value in present_values for value in resolved_values.values())


def _dependency_unresolved_entry(dep_matched: dict, reason: str) -> dict:
    """A deterministic, pipeline-authored entry -- never Generator output
    -- for a dependent intent that could not be safely resolved (its own
    generation failed deterministic validation, or its parent produced no
    usable data at all). Mirrors the precedent `_unmapped_entry_for`
    already establishes for unresolved topics: inject a fixed,
    already-valid-shape entry into the response instead of letting one
    unresolved part of a compound question fail the whole request. Uses
    SKILL.md Section 9's existing `unsupported_metric` status -- the
    closest already-defined shape requiring no `execution` block and
    telling the frontend plainly this part of the question isn't currently
    answerable, without inventing a new status outside the closed enum
    validator.py enforces."""
    intent_id = dep_matched.get("intent_id") or "this part of the question"
    return {
        "status": "unsupported_metric",
        "reference_used": dep_matched.get("reference_path", ""),
        "requested_measurement": intent_id,
        "explanation": f"This part of the question depends on an earlier result and {reason}",
    }


def _reassemble_in_original_order(matched: list[dict], resolved_entries_by_id: dict[str, dict]) -> list[dict]:
    """Reorders every intent's resolved entry back into the question's
    original `matched_references` order (root/dependent resolution order
    is purely an internal execution detail -- the frontend should see
    results in the same order the question asked for them, exactly as the
    ordinary flat path already would)."""
    ordered = []
    for m in matched:
        entry = resolved_entries_by_id.get(m.get("intent_id"))
        if entry is not None:
            ordered.append(entry)
    return ordered


async def _resolve_dependent_entry(question: str, dep_matched: dict, resolved_entries_by_id: dict[str, dict],
                                    panic_mode: bool, action_intent: str, skill_index: SkillIndex,
                                    settings: Settings, context: PipelineContext,
                                    catalog: Catalog | None) -> dict:
    """Resolves exactly ONE dependent intent with exactly ONE Generator
    call (no Level-2/Level-3 cascade -- a dependent intent is already
    scoped to a single matched reference, and its concrete entity value
    doesn't exist before its parent executes, so retrying the same call
    again would never change the outcome the way a broader candidate scope
    can for the flat path). See this module's docstring for the full
    per-step design note."""
    intent_id = dep_matched["intent_id"]
    parent_ids = dep_matched.get("depends_on") or []
    parent_entries = [resolved_entries_by_id.get(pid) for pid in parent_ids]
    resolved_values, parent_context_text = _summarize_parent_entries_for_prompt(parent_ids, parent_entries)

    if not all(_resolve_parent_entity(pid, e)[0] is not None for pid, e in zip(parent_ids, parent_entries)):
        # Deterministic, not left to the LLM: with any parent unresolved
        # there is no entity to filter on, so `_query_retains_entity_filter`
        # below would be vacuously true and an every-instance query would
        # run if the Generator ignored the prompt's "unsupported_metric"
        # instruction. Never call the Generator at all in that case.
        logger.warning(
            "dependent_query_resolution: dependent intent %r has an unresolved parent (%s); not "
            "generating or executing it.", intent_id, parent_context_text,
        )
        return _dependency_unresolved_entry(
            dep_matched,
            "its earlier result did not resolve to exactly one entity, so this part was not queried "
            "(it is never run across every instance instead).",
        )

    generator_context = _build_generator_context(
        [dep_matched], skill_index, settings, question=question, catalog=catalog,
    )
    if generator_context.reference_universe_mismatch:
        # No Level 3 for a single dependent intent -- Level 3 is a
        # Router-scope concept (a fresh, catalog-unnarrowed routing pass)
        # and dependency resolution never re-routes mid-chain. Treat this
        # exactly like any other unresolvable dependent below.
        return _dependency_unresolved_entry(
            dep_matched,
            "its catalog-identified candidates did not overlap with its own routed reference.",
        )

    known_references = set(generator_context.reference_texts.keys()) | set(generator_context.overview_texts.keys())
    known_datasources = {(dep_matched.get("data_source") or "").strip().lower()}
    catalog_status_by_metric = None
    if settings.catalog_metric_status_validation_enabled and catalog is not None:
        catalog_status_by_metric = _catalog_status_by_metric(catalog)
    candidate_scope = {m["name"] for m in generator_context.metric_metadata} or None

    raw_contract = await _run_generator(
        question, [dep_matched], panic_mode, generator_context, skill_index, settings, context,
        action_intent=action_intent, parent_context_text=parent_context_text,
        stage_scope_text=_dependent_stage_scope_text(dep_matched),
    )
    normalized = _normalize_contract_shape(raw_contract)
    validated = validator.validate_contract(
        normalized,
        candidate_metric_scope=candidate_scope,
        known_metrics=generator_context.known_prometheus_metrics,
        labels_by_metric=generator_context.labels_by_metric,
        label_values_by_metric=generator_context.label_values_by_metric,
        known_references=known_references,
        known_datasources=known_datasources,
        catalog_status_by_metric=catalog_status_by_metric,
    )
    if not validated.passed:
        logger.warning(
            "dependent_query_resolution: dependent intent %r failed validation: %s",
            intent_id, validated.reason,
        )
        return _dependency_unresolved_entry(dep_matched, f"could not be validated ({validated.reason}).")

    if normalized.get("mode") == "multi":
        # A dependent's own Generator call is scoped to exactly one matched
        # reference and must resolve to exactly one measurement. A multi
        # result means it did not honor that scope (typically: it re-built
        # the upstream part too) -- never guess which entry was the
        # intended one (the first may well be the upstream measurement).
        logger.warning(
            "dependent_query_resolution: dependent intent %r produced a multi-result contract "
            "(%d entries) for one dependent measurement; rejecting it.",
            intent_id, len(normalized.get("results", [])),
        )
        return _dependency_unresolved_entry(
            dep_matched, "produced more than one result for a single dependent measurement.",
        )
    entry = {k: v for k, v in normalized.items() if k != "mode"}
    used = entry.get("reference_used")
    if used is not None and used != dep_matched.get("reference_path"):
        # Exporter-wide `known_references` (it includes the shared
        # overview.md) means the Validator alone can't confirm this
        # result is for the reference this dependent was routed to.
        logger.warning(
            "dependent_query_resolution: dependent intent %r was routed to %r but its result claims "
            "reference_used=%r; rejecting it.", intent_id, dep_matched.get("reference_path"), used,
        )
        return _dependency_unresolved_entry(
            dep_matched, f"claimed an unexpected reference ({used!r}) instead of its routed one.",
        )

    if (entry.get("status") in executor.EXECUTABLE_STATUSES
            and (entry.get("data_source") or "").strip().lower() == "prometheus"
            and resolved_values):
        promql = entry.get("query")
        if isinstance(promql, str) and not _query_retains_entity_filter(promql, resolved_values):
            logger.warning(
                "dependent_query_resolution: generated query for intent %r did not retain the "
                "resolved entity filter %s; refusing to execute it as an unfiltered query.",
                intent_id, resolved_values,
            )
            return {
                **entry,
                "execution": {
                    "execution_status": "endpoint_error",
                    "error": (
                        f"Dependent query did not retain the resolved entity value(s) "
                        f"{resolved_values!r} from the upstream result; refusing to run it as an "
                        "unfiltered query."
                    ),
                    "result_type": None,
                    "endpoint": None,
                    "fetched_at": _now_iso(),
                },
            }

    return executor.execute_contract({"mode": "single", **entry}, settings)


def _describe_entry_for_synthesis(entry: dict) -> str | None:
    """Deterministic (no LLM), best-effort one-sentence description of a
    single already-executed entry, for `_build_dependency_synthesis`.
    Formats a byte-denominated metric's latest value as GB/MB, and an
    expression that visibly multiplies by 100 as a percentage -- anything
    it can't confidently describe (no data, a non-`ok`/`panic_mode_
    best_effort` status, a non-series result shape) returns `None` rather
    than guessing, so it simply drops out of the combined sentence instead
    of ever contributing a wrong or misleading fragment."""
    if entry.get("status") not in executor.EXECUTABLE_STATUSES:
        return None
    execution = entry.get("execution") or {}
    if execution.get("execution_status") != "success":
        return None
    series = execution.get("series") or []
    if not series:
        return None
    points = series[0].get("points") or []
    if not points:
        return None
    value = points[-1].get("value")
    if value is None:
        return None

    legend = series[0].get("legend_label")
    query = entry.get("query")
    metric_name = (entry.get("measurement_used") or {}).get("name") or ""
    # Name the measurement (not just the entity), otherwise a combined
    # sentence reads "node_id=node-00 is 38.7%. node_id=node-00 is 3.20 GB."
    # with no way to tell which number is which.
    subject = metric_name or "this measurement"
    if legend and legend != "value":
        subject += f" for {legend}"

    if isinstance(query, str) and _PERCENT_EXPR_RE.search(query):
        return f"{subject} is {value:.1f}%."
    if _BYTES_NAME_HINT_RE.search(metric_name):
        return f"{subject} is {_format_bytes(value)}."
    return f"{subject} is {value:g}."


def _format_bytes(value: float) -> str:
    """GB when the magnitude is at least 1 GB, MB otherwise -- matches the
    "formats bytes as GB/MB" behavior this feature is specified to have.
    """
    gb = value / (1024 ** 3)
    if abs(gb) >= 1:
        return f"{gb:.2f} GB"
    return f"{value / (1024 ** 2):.2f} MB"


def _build_dependency_synthesis(entries: list[dict]) -> str | None:
    """Deterministic (no second LLM call) one-line summary tying a
    dependency-resolved compound response's results together (SKILL.md
    Section 9's `synthesis` field) -- built entirely from each entry's own
    already-normalized `execution.series` data. Never allowed to turn a
    working multi-result response into an error: any entry that can't be
    confidently described just silently drops out of the sentence, and if
    NOTHING across the whole response can be described, this returns
    `None` (validator.py already accepts `synthesis: null` whenever the
    key itself is present -- see this module's docstring)."""
    fragments = [f for f in (_describe_entry_for_synthesis(e) for e in entries) if f]
    if not fragments:
        return None
    return " ".join(fragments)


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


async def _run_dependency_aware_pipeline(question: str, matched: list[dict], plan: DependencyPlan,
                                          panic_mode: bool, action_intent: str, unresolved_topics: list[dict],
                                          skill_index: SkillIndex, settings: Settings, context: PipelineContext,
                                          catalog: Catalog | None) -> dict:
    """Orchestrates the staged lifecycle this module's docstring describes,
    for a question `_extract_dependency_plan` has already confirmed has a
    genuine, validated dependency.

    The ONLY time this re-resolves through the ordinary flat path across
    the FULL `matched` set is when a root needs a human answer (see
    `_contract_needs_pause`), because nothing may be executed while a
    clarification is pending. Even then the flat result is returned only if
    it still pauses or executes nothing: the flat path knows nothing about
    the dependency (no parent context, no entity-filter check), so a flat
    result that WOULD execute is the every-instance dependent query this
    feature exists to prevent, and is replaced by a deterministic failure.

    Every other problem -- a root stage that did not honor its scope (wrong
    result count or provenance, see `_index_executed_entries`) -- is a
    deterministic `validation_failed` result, never a flat fallback.
    """
    root_contract = await _resolve_matched_group(
        question, plan.roots, panic_mode, action_intent, [], skill_index, settings, context, catalog,
        stage_scope_text=_root_stage_scope_text(plan.roots),
    )

    if root_contract.get("status") == INTERNAL_VALIDATION_FAILED_STATUS:
        # The roots themselves were invalid -- nothing to execute, and
        # nothing a dependency-aware retry could fix (a Level-1/2/3
        # cascade already ran inside `_resolve_matched_group`).
        return root_contract

    if _contract_needs_pause(root_contract):
        # A root needs clarification or confirmation before ANYTHING may
        # be executed (HANDOFF.md: `result` carries no data while a
        # clarification/confirmation is pending). Never partially execute
        # -- re-resolve through the ordinary flat path across the full
        # matched set so the whole response pauses together, but see this
        # function's docstring: that flat result may not be allowed to
        # execute.
        flat_contract = await _resolve_matched_group(
            question, matched, panic_mode, action_intent, unresolved_topics,
            skill_index, settings, context, catalog,
        )
        if (flat_contract.get("status") == INTERNAL_VALIDATION_FAILED_STATUS
                or _contract_needs_pause(flat_contract)
                or not _contract_has_executable_entry(flat_contract)):
            return flat_contract
        return _dependency_stage_failure(
            "a part of the question needed clarification, and re-resolving the whole question without "
            "dependency context would have queried the dependent part for every instance."
        )

    # Verify, BEFORE executing anything, that the root stage honored its
    # scope: exactly one result per root, each for its own routed reference.
    # (The Validator cannot -- see `_index_executed_entries`.)
    if _index_executed_entries(root_contract, plan.roots) is None:
        return _dependency_stage_failure(
            "the root stage did not resolve exactly the measurement(s) assigned to it, so its result "
            "can't be safely used to resolve the dependent part."
        )

    executed_root = executor.execute_contract(root_contract, settings)
    root_entries_by_id = _index_executed_entries(executed_root, plan.roots)
    if root_entries_by_id is None:  # defensive: execution preserves count/order, so not expected
        return _dependency_stage_failure("the executed root result could not be attributed to its intent(s).")

    resolved_entries_by_id: dict[str, dict] = dict(root_entries_by_id)
    for dep_matched in plan.dependent_order:
        entry = await _resolve_dependent_entry(
            question, dep_matched, resolved_entries_by_id, panic_mode, action_intent,
            skill_index, settings, context, catalog,
        )
        resolved_entries_by_id[dep_matched["intent_id"]] = entry

    ordered_results = _reassemble_in_original_order(matched, resolved_entries_by_id)
    ordered_results += [_unmapped_entry_for(t) for t in unresolved_topics]

    if len(ordered_results) == 1:
        combined = {"mode": "single", **ordered_results[0]}
    else:
        combined = {
            "mode": "multi",
            "results": ordered_results,
            "synthesis": _build_dependency_synthesis(ordered_results),
        }
    combined[ALREADY_EXECUTED_MARKER] = True
    return combined


async def _run_generation_attempt(question: str, matched: list[dict], panic_mode: bool,
                                   generator_context: "GeneratorContext", skill_index: SkillIndex,
                                   settings: Settings, context: "PipelineContext", action_intent: str,
                                   unresolved_topics: list[dict], *, candidate_metric_scope: set[str] | None,
                                   validator_kwargs: dict,
                                   stage_scope_text: str | None = None) -> tuple[dict, dict, "validator.ValidationResult"]:
    """Batch 2 (Phase 4): one full Generator -> normalize -> alert-defaults
    -> merge-unresolved-topics -> validate pass, parameterized only by
    which `GeneratorContext` (Level 1 or Level 2) and which
    `candidate_metric_scope` to validate against -- everything else
    (Router output, unresolved_topics, the known_metrics/labels_by_metric/
    known_references/known_datasources/catalog_status_by_metric bundle in
    `validator_kwargs`) is identical between a Level-1 attempt and its
    Level-2 retry, per Batch 2's explicit instruction that Level 2 expands
    metric_metadata only.

    Returns `(contract, merged, validated)`: `contract` is the raw
    Generator output (pre-unresolved-topics-merge, used by
    `_should_fallback_to_level2` to inspect the Generator's OWN reported
    status); `merged` has unresolved topics folded in (what
    `_package_validated` below actually returns to the caller); `validated`
    is `merged`'s `ValidationResult`, computed once here so the caller
    never has to validate the same contract twice.
    """
    contract = await _run_generator(question, matched, panic_mode, generator_context, skill_index, settings,
                                     context, action_intent=action_intent, stage_scope_text=stage_scope_text)
    contract = _normalize_contract_shape(contract)
    contract = _apply_alert_rule_defaults(contract, settings)
    merged = _merge_unresolved_topics(contract, unresolved_topics)
    validated = validator.validate_contract(merged, candidate_metric_scope=candidate_metric_scope,
                                             **validator_kwargs)
    return contract, merged, validated


def _contract_reports_unsupported_metric(contract: dict) -> bool:
    """True if the Generator's OWN output (before unresolved-topics merge)
    reported `status: "unsupported_metric"` -- for either a single-mode
    contract or ANY entry of a multi-mode one (Batch 2 Phase 3's "do not
    only validate the first metric" requirement applies here too: a
    multi-metric question where only the second sub-query was actually
    outside the Level-1 candidate set must still trigger the Level-2
    retry, not just have that one entry silently reported as
    unsupported)."""
    if not isinstance(contract, dict):
        return False
    if contract.get("mode") == "multi":
        return any(isinstance(r, dict) and r.get("status") == "unsupported_metric"
                   for r in contract.get("results", []))
    return contract.get("status") == "unsupported_metric"


def _should_fallback_to_level2(merged_contract: dict, validated) -> bool:
    """Batch 2 (Phase 4)'s two allowed Level-2 triggers, and only these
    two -- never mere question ambiguity, never an LLM confidence score:

      1. The Generator itself reported candidate-scope insufficiency via
         the existing `unsupported_metric` status (a structurally VALID
         contract -- `validated.passed` is True here, so this can't be
         detected from `validated` alone).
      2. Deterministic post-generation validation caught the Generator
         selecting a real, known metric that was simply outside this
         attempt's candidate scope (`validated.candidate_scope_violation`
         -- a validation FAILURE, but a specifically retryable kind; see
         validator.py's `_check_candidate_scope`).

    `merged_contract` (unresolved-topics already folded in) is safe to
    inspect here for (1): unresolved-topic entries are always `status:
    "unmapped"`, never `"unsupported_metric"`, so merging first can never
    manufacture a false-positive trigger.
    """
    if validated.passed:
        return _contract_reports_unsupported_metric(merged_contract)
    return validated.candidate_scope_violation


def _build_level2_generator_context(base_ctx: "GeneratorContext", matched: list[dict],
                                     catalog: Catalog | None) -> "GeneratorContext":
    """Batch 2 (Phase 4)'s Level-2 candidate construction:

        Router-selected references -> all metrics belonging to those
        references -> metric_metadata -> Generator retry

    Reuses `Catalog.by_reference_path()` (added in Batch 1 for exactly
    this) unioned across every matched reference, restricted to
    `base_ctx.known_prometheus_metrics` as a consistency cross-check (a
    metric absent from the Metric Directory this request actually opened
    should never appear as a Level-2 candidate even if catalog.json lists
    it under this reference_path -- keeps Level 2 from ever showing the
    Generator something `known_metrics` validation would reject anyway).

    Touches ONLY `metric_metadata` (via `dataclasses.replace`) -- every
    other field (`known_prometheus_metrics`, `labels_by_metric`,
    `reference_texts`, `overview_texts`, etc.) is carried over unchanged
    from `base_ctx`, per Batch 2's explicit instruction that Level 2 does
    not require changing `known_prometheus_metrics`/`labels_by_metric`.

    If `catalog` is None (should not happen -- Level 2 is only ever
    reached after a Level-1 attempt that required the catalog to be
    loaded -- but handled defensively) or no matched reference yields any
    catalog entries at all, this degrades to an empty `metric_metadata`,
    i.e. behaviorally identical to no catalog being available for the
    retry -- never an error.
    """
    if catalog is None:
        return replace(base_ctx, metric_metadata=())

    by_name: dict[str, dict] = {}
    for m in matched:
        reference_path = m.get("reference_path")
        if not reference_path:
            continue
        for entry in catalog.by_reference_path(reference_path):
            if entry.name in base_ctx.known_prometheus_metrics and entry.name not in by_name:
                by_name[entry.name] = _metric_metadata_dict(entry)

    # Level 2 broadens Level 1, it never replaces it: carry over any Level-1
    # candidate the per-file lookup above does not cover (e.g. a vendor-only
    # metric with no reference_path), so Level 1 is always a subset of Level 2.
    for candidate in base_ctx.metric_metadata:
        by_name.setdefault(candidate["name"], candidate)

    return replace(base_ctx, metric_metadata=tuple(by_name.values()))


def _apply_alert_rule_defaults(contract: dict, settings: Settings) -> dict:
    """SKILL.md Section 12.5: `alert_rule.folder` is supplied by the
    surrounding application (this function), never invented by the
    Generator; `alert_rule.datasource_uid` is always null coming out of the
    Generator, resolved only at confirmation time. This is deterministic
    Python, not an LLM decision, for the same reason validator.py's checks
    are deterministic -- there's exactly one right value for each (the
    deployment's configured default folder; null), so there's nothing here
    that benefits from a model's judgement. Only touches a contract that is
    ALREADY shaped like `alert_rule_proposed`; anything else (including a
    malformed near-miss) is returned untouched and left for validator.py to
    reject on its own terms."""
    if not isinstance(contract, dict) or contract.get("status") != ALERT_PROPOSED_STATUS:
        return contract
    alert_rule = contract.get("alert_rule")
    if not isinstance(alert_rule, dict):
        return contract
    updated_alert_rule = dict(alert_rule)
    if not updated_alert_rule.get("folder"):
        updated_alert_rule["folder"] = settings.grafana_default_folder_uid
    # Always overwritten, never merely defaulted -- Section 12.5 requires
    # this to be null regardless of anything the Generator supplied, since a
    # datasource UID is live Grafana configuration the Generator has no way
    # to verify (Principle 9's "runtime-only fact" category).
    updated_alert_rule["datasource_uid"] = None
    return {**contract, "alert_rule": updated_alert_rule}


# ---- Generator output normalization (structural only, never content) ---------------


def _normalize_contract_shape(contract: dict) -> dict:
    """Repairs two specific, observed classes of Generator near-miss.
    Both are purely wrapper/envelope inference -- neither ever invents,
    guesses, or alters any field's VALUE, so neither can introduce a
    fabricated metric name, label key, or query the way a content-level
    "fix" would; anything not matching one of these two narrow shapes is
    returned untouched and the deterministic validator reports whatever's
    actually wrong with it.

    1. Missing `mode` entirely: the model produces a structurally-correct
       single result object (a valid `status` plus that status's own
       required fields, e.g. a well-formed `unsupported_metric` entry) or
       a structurally-correct multi accumulator (`results` + `synthesis`)
       but omits the top-level `"mode"` envelope key SKILL.md Section 9
       requires around it.

    2. A known status value placed directly in `mode`, with no separate
       `status` key at all (e.g. `{"mode": "ambiguous_metric", ...}`):
       observed from a live "How can I tell if the system is overloaded?"
       query, traced to SKILL.md Section 9's `ambiguous_metric` and
       `unsupported_metric` examples not showing `"mode": "single"`
       explicitly the way every other status's example does (now fixed --
       see Section 9). Only triggers when `mode`'s value is one of
       validator._VALID_STATUSES and there's no pre-existing `status` key;
       a `mode` value that is neither "single"/"multi" nor a recognized
       status (i.e. genuinely unrecognized input, not this specific
       near-miss) is left untouched, and validate_contract's own "'mode'
       must be 'single' or 'multi'" error still fires for it exactly as
       before.
    """
    if not isinstance(contract, dict):
        return contract

    if "mode" not in contract:
        if isinstance(contract.get("results"), list) and "synthesis" in contract:
            return {"mode": "multi", **contract}
        if "status" in contract:
            return {"mode": "single", **contract}
        return contract

    mode = contract["mode"]
    if (
        mode not in ("single", "multi")
        and "status" not in contract
        and mode in validator._VALID_STATUSES
    ):
        rest = {k: v for k, v in contract.items() if k != "mode"}
        return {"mode": "single", "status": mode, **rest}

    return contract


# ---- final assembly: validate, then merge in any unresolved topics -----------------


def _finalize(contract: dict, unresolved_topics: list[dict], **validator_kwargs) -> dict:
    """Merges in any unresolved-topic entries FIRST, then validates the
    combined result exactly once. Merging before validating (rather than
    validating `contract` alone and trusting the merge afterwards) matters
    for one specific shape: when nothing at all matched but unresolved
    topics exist, `contract` starts as an intentionally-empty multi
    accumulator ({"results": []}) that isn't valid on its own (Section 6
    Step 6 requires 2+ entries for mode:"multi") -- it only becomes valid
    once the unresolved-topic entries are merged in. Validating the merged
    result also means `_unmapped_entry_for`'s fixed shape gets the same
    defense-in-depth check as everything else, at negligible cost.

    Used directly by every non-Generator response path (gate_stop, no
    matched reference). The Generator path (`run_pipeline`'s Level-1/
    Level-2 section) does its own merge+validate via
    `_run_generation_attempt` instead -- so it can inspect the
    `ValidationResult` BEFORE deciding whether to retry -- and calls
    `_package_validated` (this function's second half) directly on
    whichever attempt wins, rather than validating that contract twice.
    """
    merged = _merge_unresolved_topics(contract, unresolved_topics)
    result = validator.validate_contract(merged, **validator_kwargs)
    return _package_validated(merged, result)


def _package_validated(merged: dict, result) -> dict:
    """The logging + success/failure-shape half of what used to be all of
    `_finalize` -- split out in Batch 2 so the Level-1/Level-2 retry
    orchestration in `run_pipeline` can validate a contract once (via
    `_run_generation_attempt`), decide whether to retry, and only then
    call this to produce the actual return value -- instead of validating
    the eventual winner a second time."""
    if not result.passed:
        logger.warning("Deterministic validation failed for question contract: %s | contract=%r",
                        result.reason, merged)
        return {"mode": "single", "status": INTERNAL_VALIDATION_FAILED_STATUS, "explanation": result.reason}
    for w in result.warnings:
        logger.info("Validator warning (non-fatal): %s", w)

    return merged


def _merge_unresolved_topics(contract: dict, unresolved_topics: list[dict]) -> dict:
    if not unresolved_topics:
        return contract

    entries = list(contract["results"]) if contract.get("mode") == "multi" else [
        {k: v for k, v in contract.items() if k != "mode"}
    ]
    for topic in unresolved_topics:
        entries.append(_unmapped_entry_for(topic))

    if len(entries) == 1:
        return {"mode": "single", **entries[0]}
    return {"mode": "multi", "results": entries, "synthesis": None}


def _unmapped_entry_for(topic: dict) -> dict:
    description = topic.get("description") or "part of this question"
    reason = topic.get("reason") or ""
    explanation = f"No reference in the currently-loaded skill package covers: {description}."
    if reason:
        explanation += f" {reason}"
    return {"status": "unmapped", "explanation": explanation}


def _unmapped_explanation() -> str:
    """Only used for the true Router/prompt-problem fallback (see the
    warning logged right before this is called) -- the normal "some or all
    of the question is unresolved" paths describe themselves via
    `unresolved_topics` -> `_unmapped_entry_for` instead, one entry per
    topic, which is more specific than any single fixed string here could
    be."""
    return ("No reference's purpose plausibly covers this question, and the routing phase "
            "did not return a matched reference, an explicit gate_stop, or an unresolved-topic "
            "description for it.")


def _clean_unresolved_topics(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    cleaned = []
    for item in raw:
        if isinstance(item, dict) and item.get("description"):
            cleaned.append({"description": str(item["description"]), "reason": str(item.get("reason") or "")})
    return cleaned


# ---- Router --------------------------------------------------------------------


_ROUTER_INSTRUCTIONS = """\
You are the routing phase of the observability-query-builder skill. You
implement SKILL.md Section 6, Steps 1-2 ONLY -- gating and routing. You do
NOT select a metric, construct a query, or produce an Output Contract; a
later phase does that.

Respond with ONLY a JSON object, no other text, matching exactly one of
these two shapes:

Shape A -- a gating condition stopped the request immediately (Step 1), or
truly nothing in the question is covered and panic_mode is false, or
panic_mode is true with truly zero domain signal (Section 7.4):
{
  "gate_stop": {
    "status": "out_of_scope_action" | "declined" | "unmapped",
    "reason": "nonsensical_input" | "prompt_injection_attempt" | "parameter_requires_clarification" | null,
    "requested_action": "<only for out_of_scope_action>" | null,
    "clarification": "<only when reason is parameter_requires_clarification>" | null,
    "explanation": "<short, factual>"
  },
  "matched_references": [],
  "panic_mode": false,
  "unresolved_topics": []
}

IMPORTANT -- do not confuse imperative PHRASING with an actual action
request: "out_of_scope_action" is ONLY for requests to perform a WRITE or
MUTATING action against a live system -- restart something, delete
something, silence an alert, change a configuration. "Show me X," "give me
X," "get me X," "display X," "pull up X" are all ordinary data-retrieval
requests phrased as commands -- this is completely normal, conversational
phrasing for "I want to see data about X," and must NEVER be classified as
out_of_scope_action just because it reads like an imperative sentence. If
you are ever tempted to set status to "out_of_scope_action", first confirm
you can name the concrete mutating action being requested (and that it
belongs in "requested_action", which is REQUIRED and must never be left
empty) -- if you can't name one, this is a normal data request; route it
through Shape B instead.

Shape B -- one or more routing-table rows plausibly match AT LEAST PART of
the question:
{
  "gate_stop": null,
  "matched_references": [
    {"reference_path": "references/...", "data_source": "prometheus" | "opensearch" | "..."}
  ],
  "panic_mode": true | false,
  "unresolved_topics": [
    {"description": "<the specific measurement/intent this covers, restated briefly>",
     "reason": "<why no routing-table row covers it -- e.g. 'needs an OpenSearch-backed log measurement; no domain reference currently defines one'>"}
  ]
}

COMPOUND QUESTIONS -- READ CAREFULLY: a single question can ask for more
than one measurement at once (e.g. "show CPU utilization and recent error
logs for node-1"). Identify each distinct measurement/intent in the
question SEPARATELY and match each one against Section 4's routing table
independently:
  - An intent that matches one or more rows -> include those reference(s)
    in `matched_references` (Shape B). Do this even if only PART of the
    overall question is covered this way.
  - An intent that matches NO row at all (most commonly because it needs a
    datasource or domain this skill package doesn't have reference content
    for yet, e.g. an OpenSearch-backed measurement while
    opensearch-fundamentals.md's own note says no domain routes there yet)
    -> do NOT silently drop it, and do NOT let it force the whole question
    into `unmapped`. Instead, describe it in `unresolved_topics` (Shape B)
    alongside whatever else DID match.
  - Only use Shape A's `unmapped` gate_stop when NOTHING in the question
    matches ANY row at all -- if even one sub-intent matches, use Shape B
    with the matching part in `matched_references` and the rest in
    `unresolved_topics`.
  - A single-measurement question that matches nothing still uses Shape A's
    `unmapped`, exactly as before; `unresolved_topics` is for compound
    questions with a genuinely mixed match, not a replacement for
    `unmapped`.

Follow SKILL.md's Section 3 (when to use/not use), Section 4 (routing table
and its keyword-matching rule), and Section 7 (error handling) exactly as
written below. Do not invent a status outside the enum shown above.
"""

_ALERT_ROUTER_ADDENDUM = """\

ALERT-RULE CREATION IS ENABLED ON THIS DEPLOYMENT (SKILL.md Section 12).
This adds exactly one narrow thing to everything above -- it changes nothing
else about how you gate or route a request.

A request to CREATE a brand-new alert rule for a metric this skill covers
("alert me if CPU exceeds 90%", "create an alert for high GPU temperature",
"set up alerting on low disk space", "notify me when swap usage goes above
2GB") is NOT out_of_scope_action. Route it exactly like an ordinary Shape B
data question -- match it against Section 4's table, resolve
matched_references the same way -- and add exactly one extra top-level
field to your Shape B response:

{
  "gate_stop": null,
  "matched_references": [...],
  "panic_mode": false,
  "unresolved_topics": [],
  "action_intent": "propose_alert_rule"
}

Omit `action_intent` entirely (or leave it out of your response) for an
ordinary read question -- it defaults to a normal data request. Only set it
to "propose_alert_rule" when the user is UNAMBIGUOUSLY asking to CREATE a
new alerting rule. Never set it merely because the question mentions the
word "alert" in passing -- "is there an alert on this?" or "show me active
alerts" are read questions about alert STATE, not creation requests, and get
no `action_intent` field at all.

This changes NOTHING about requests to silence, acknowledge, delete, or
otherwise modify an EXISTING alert or resource -- "silence this alert,"
"delete that alert rule," "turn off the GPU temperature alert," and similar
remain out_of_scope_action (Shape A), exactly as described above, with NO
exception. If a request could plausibly be read either way -- creating
something brand-new vs. mutating something that already exists -- classify
it as out_of_scope_action; under-triggering this narrow addition is always
the safer failure mode. When genuinely unsure, prefer out_of_scope_action.
"""

_DEPENDENCY_ROUTER_ADDENDUM = """\

DEPENDENT QUERY RESOLUTION IS ENABLED ON THIS DEPLOYMENT. This adds exactly
one narrow thing to Shape B above -- it changes nothing about gating, Shape
A, or how you decide which reference(s) match.

A compound question can ask for a second measurement that only makes sense
AFTER an earlier part of the SAME question has actually run -- e.g. "which
node has the highest CPU utilization, and how much memory is available on
that node?" The second half ("that node") names no concrete entity of its
own; it needs the first half's result.

For every object in `matched_references`, add exactly two extra fields:

{
  "reference_path": "references/...",
  "data_source": "prometheus" | "opensearch" | "...",
  "intent_id": "<a short id you invent for this distinct measurement/intent -- e.g. 'q1', 'q2'>",
  "depends_on": ["<intent_id of an EARLIER intent in this question this one needs the resolved result of>"]
}

Rules:
- Give EVERY object in `matched_references` an `intent_id`, even ones with
  no dependency at all -- for those, `depends_on` is simply `[]` (or
  omitted).
- Only set `depends_on` when the question's OWN wording makes one part
  genuinely conditional on another part's concrete result -- a pronoun or
  definite reference ("that node", "it", "the same GPU") pointing back at
  an entity resolved by an earlier part. Two independent measurements that
  merely share a topic, or that each already name their own concrete
  entity (e.g. "CPU and memory usage on node-1"), are NOT dependent on each
  other -- leave `depends_on` empty for both.
- Never create a dependency cycle, and never make an intent depend on
  itself.
- A single-measurement question, or a compound question where every part
  already names its own concrete entity, needs no `depends_on` at all --
  omit it (or leave it `[]`) exactly as you always would have.
"""


def _build_router_instructions(settings: Settings) -> str:
    """Returns the Router's system instructions, appending the alert-rule-
    creation addendum ONLY when that feature flag is on, and/or the
    dependency-resolution addendum ONLY when THAT feature flag is on (see
    app/config.py's `dependent_query_resolution_enabled` and this module's
    docstring). Each addendum is independently gated behind its own config
    flag that defaults to False -- keeping the base `_ROUTER_INSTRUCTIONS`
    string byte-for-byte unchanged when both are off means existing
    read-query routing gets ZERO prompt changes unless a deployment has
    explicitly opted into one or the other (or both). A regression in
    either addendum can also never leak into a deployment that hasn't
    turned its flag on, and a bad rollout of either is fully reversible by
    flipping that flag back off -- no prompt rollback required."""
    instructions = _ROUTER_INSTRUCTIONS
    if settings.alert_rule_creation_enabled:
        instructions += _ALERT_ROUTER_ADDENDUM
    if settings.dependent_query_resolution_enabled:
        instructions += _DEPENDENCY_ROUTER_ADDENDUM
    return instructions


async def _run_router(question: str, skill_index: SkillIndex, settings: Settings,
                       context: PipelineContext, catalog: Catalog | None = None) -> dict:
    section_headers = ["## 3.", "## 4.", "## 7."]
    if settings.alert_rule_creation_enabled:
        # Only loaded into the prompt when the deployment has opted in --
        # see _build_router_instructions' docstring for why this mirrors
        # the same "flag off => zero prompt change" guarantee.
        section_headers.append("## 12.")

    section_texts = [skill_index.section(h) for h in section_headers]
    if catalog is not None:
        # Router-level narrowing is unconditional (Batch 4: the
        # catalog_assisted_routing_enabled migration switch was removed --
        # see this module's docstring). Replace ONLY the "## 4." entry, in
        # place, with a per-question-narrowed version; section_headers/
        # section_texts stay index-aligned, so this is a targeted
        # substitution, not a rebuild. If `catalog` is None (no catalog.json
        # on this deployment, or it failed to load -- see `_get_catalog`),
        # this branch never runs and `sections` below is byte-for-byte the
        # full, unnarrowed routing table -- the same safe degradation
        # `_maybe_narrow_section4`'s own miss/low-confidence paths already
        # provide for a catalog that loads but doesn't recognize the
        # question.
        idx = section_headers.index("## 4.")
        section_texts[idx] = _maybe_narrow_section4(
            section_texts[idx], skill_index, catalog, question,
            min_confident_score=settings.catalog_narrow_min_score,
        )

    sections = "\n\n".join(section_texts)
    prompt_parts = [f"SKILL.md reference sections:\n\n{sections}", f"\nUser question: {question}"]
    if context.previous_question:
        prompt_parts.append(
            f"\nThis is a follow-up in an ongoing clarification exchange.\n"
            f"Original question: {context.previous_question}\n"
            f"Prior result required clarification: {context.previous_result}\n"
            f"User's clarifying answer: {context.clarification_answer}\n"
            f"Route based on the FULL combined intent, not the follow-up text alone."
        )
    prompt = "\n\n".join(prompt_parts)

    response = llm_client.call_llm_json(
        prompt=prompt,
        system_instruction=_build_router_instructions(settings),
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    return response.parsed


# ---- Generator context assembly (deterministic, no LLM) -----------------------


@dataclass
class GeneratorContext:
    reference_texts: dict[str, str] = field(default_factory=dict)
    overview_texts: dict[str, str] = field(default_factory=dict)
    fundamentals_texts: dict[str, str] = field(default_factory=dict)
    # Structured discovery data, kept alongside the formatted prompt text so
    # the deterministic validator can check against it directly instead of
    # re-parsing prose it was never meant to be machine-read from.
    known_prometheus_metrics: set[str] = field(default_factory=set)
    labels_by_metric: dict[str, list[str] | None] = field(default_factory=dict)
    # Value half of Principle 9 (label_discovery.LabelDetails.values,
    # projected down to just the values dict per metric -- the
    # `truncated_keys` distinction is only meaningful for prompt formatting,
    # which already happened by the time this reaches the Validator; a key
    # simply absent here (whether because it was truncated or discovery
    # failed for the metric entirely) means "cannot confirm either way,
    # skip the value check for it" -- same fail-open posture as
    # labels_by_metric's own None-means-skip handling.
    label_values_by_metric: dict[str, dict[str, list[str]] | None] = field(default_factory=dict)
    attributes_by_pattern: dict[str, dict] = field(default_factory=dict)
    labels_block: str = ""
    attributes_block: str = ""
    # Batch 1 (Phase 2): the Level-1 catalog-narrowed Generator-candidate
    # set (see app/config.py's `catalog_generator_candidates_enabled` and
    # `catalog_metric_candidates` above). Each entry is a plain dict with
    # EXACTLY the four keys `_metric_metadata_dict` produces --
    # name/type/help/unit -- never a CatalogEntry and never any of
    # keywords/priority/status/exporter/reference_path; those stay
    # catalog-internal per the frozen architecture (§4 of the Phase 0
    # handoff). Deliberately does NOT narrow `known_prometheus_metrics`
    # above (Batch 1 decision #4) -- that set stays at its existing,
    # broader Router-selected-reference scope, so label discovery and
    # known_metrics validation are completely unaffected by this field.
    # Empty (the default) whenever no catalog is available, the catalog
    # missed, or the catalog's top score didn't clear the confidence floor
    # -- in every one of those cases the Generator prompt gets no candidate
    # block at all, which is exactly the pre-catalog behavior.
    metric_metadata: tuple[dict[str, str | None], ...] = field(default_factory=tuple)
    # Batch 3 (Phase 5). Pipeline-internal orchestration signal ONLY --
    # never rendered into the Generator prompt (see
    # _format_metric_metadata_for_prompt, which only ever reads
    # `metric_metadata`). True exactly when a CONFIDENT catalog decision's
    # candidates shared zero overlap with `known_prometheus_metrics` (see
    # `Level1Outcome`/`_build_metric_metadata`'s docstring) -- tells
    # `run_pipeline` to skip Level 1/2 entirely for this request and go
    # straight to Level 3, rather than silently running an unconstrained
    # Level-1 attempt against a reference universe the catalog itself
    # disagrees with.
    reference_universe_mismatch: bool = False


@dataclass(frozen=True)
class Level1Outcome:
    """See `_build_metric_metadata`'s docstring for what
    `reference_universe_mismatch` distinguishes and why it matters."""
    metric_metadata: tuple[dict[str, str | None], ...] = ()
    reference_universe_mismatch: bool = False


def _build_generator_context(matched: list[dict], skill_index: SkillIndex,
                              settings: Settings, question: str = "",
                              catalog: Catalog | None = None) -> GeneratorContext:
    ctx = GeneratorContext()
    datasources: set[str] = set()

    for m in matched:
        ref_path = m["reference_path"]
        ds = (m.get("data_source") or "").strip().lower()
        datasources.add(ds)

        ctx.reference_texts[ref_path] = skill_index.read_reference(ref_path)

        overview_path = skill_index.overview_path_for(ref_path)
        if overview_path and overview_path not in ctx.overview_texts:
            ctx.overview_texts[overview_path] = skill_index.read_reference(overview_path)
            if ds == "prometheus":
                # The catalog is the sole metric-universe source. The
                # compatibility lookup only supplies paths for legacy skill
                # packages that still carry an overview table.
                ctx.known_prometheus_metrics.update(skill_index.metric_directory(overview_path).keys())

    # Load the correct *-fundamentals.md for every real datasource in play,
    # regardless of whether the router happened to route directly to it --
    # Step 5 requires it for construction either way. Looked up by scanning
    # the routing table (see fundamentals_reference_for's docstring) --
    # never a hardcoded {data_source: path} map, so a third data source's
    # fundamentals file is picked up with zero code change once SKILL.md
    # has a routing row for it.
    for ds in datasources:
        path = skill_index.fundamentals_reference_for(ds)
        if path:
            try:
                ctx.fundamentals_texts[path] = skill_index.read_reference(path)
            except Exception:
                logger.warning("Could not load fundamentals reference for datasource %r", ds)
        else:
            logger.warning("No *-fundamentals.md routing row found for datasource %r; "
                            "construction will proceed without it.", ds)

    if "prometheus" in datasources and ctx.known_prometheus_metrics:
        label_details = label_discovery.discover_label_details_for_metrics(
            settings.prometheus_url, sorted(ctx.known_prometheus_metrics),
            lookback=timedelta(hours=settings.label_discovery_lookback_hours),
        )
        # Project down to the two shapes the rest of the pipeline already
        # expects: labels_by_metric (keys-only, unchanged shape/consumers --
        # Principle 9's key check, the keys half of the prompt block) and
        # label_values_by_metric (new -- Principle 9's value check, the
        # values half of the prompt block). Both are derived from the same
        # single discovery pass above; this never doubles the number of
        # requests made against Prometheus.
        ctx.labels_by_metric = {
            metric: (details.keys if details is not None else None)
            for metric, details in label_details.items()
        }
        ctx.label_values_by_metric = {
            metric: (details.values if details is not None else None)
            for metric, details in label_details.items()
        }
        keys_block = label_discovery.format_labels_for_prompt(ctx.labels_by_metric)
        values_block = label_discovery.format_label_values_for_prompt(label_details)
        ctx.labels_block = f"{keys_block}\n\n{values_block}" if values_block else keys_block

    if "opensearch" in datasources:
        ctx.attributes_by_pattern = field_discovery.discover_attributes_for_all_known_patterns(settings.opensearch_url)
        ctx.attributes_block = field_discovery.format_attributes_for_prompt(ctx.attributes_by_pattern)

    if catalog is not None and ctx.known_prometheus_metrics:
        # Level-1 Generator-candidate construction is unconditional (Batch
        # 4: the catalog_generator_candidates_enabled migration switch was
        # removed -- see this module's docstring). If `catalog` is None
        # (no catalog.json on this deployment, a load failure, or Level 3's
        # own deliberate `catalog=None` call -- see `_run_level3_fallback`),
        # this branch never runs and `metric_metadata` stays `()`, exactly
        # the pre-catalog/Level-3 behavior.
        outcome = _build_metric_metadata(catalog, question, ctx.known_prometheus_metrics,
                                          min_confident_score=settings.catalog_narrow_min_score,
                                          matched=matched)
        ctx.metric_metadata = outcome.metric_metadata
        ctx.reference_universe_mismatch = outcome.reference_universe_mismatch

    return ctx


def _matched_reference_metric_names(catalog: Catalog, matched: list[dict],
                                    known_prometheus_metrics: set[str]) -> set[str]:
    """The metrics belonging to the Router-matched reference file(s): every
    catalog entry whose `reference_path` is one of them, plus any metric of
    the SAME exporter that has no `reference_path` at all (a vendor-only
    metric has no Markdown file to be scoped to, so exporter level is the
    narrowest scope it has -- excluding it would silently drop it from
    Level 1). Restricted to `known_prometheus_metrics`, like Level 2.

    Deliberately narrower than `known_prometheus_metrics`, which is the
    whole exporter's Metric Directory and stays the validator's / label
    discovery's universe. Level 1 candidates are drawn from this set and
    Level 2 is a superset of it (`_build_level2_generator_context`)."""
    names: set[str] = set()
    exporters: set[str] = set()
    for m in matched:
        reference_path = m.get("reference_path")
        if not reference_path:
            continue
        for entry in catalog.by_reference_path(reference_path):
            exporters.add(entry.exporter)
            if entry.name in known_prometheus_metrics:
                names.add(entry.name)
    for exporter in exporters:
        for entry in catalog.by_exporter(exporter):
            if entry.reference_path is None and entry.name in known_prometheus_metrics:
                names.add(entry.name)
    return names


def _build_metric_metadata(catalog: Catalog, question: str, known_prometheus_metrics: set[str],
                            min_confident_score: float,
                            matched: list[dict] | None = None) -> "Level1Outcome":
    """Batch 1 (Phase 2): builds `GeneratorContext.metric_metadata` from a
    confident metric-level catalog decision (`catalog_metric_candidates`),
    restricted to metrics ALSO present in `known_prometheus_metrics` (the
    Metric Directory this request already opened via its matched
    reference(s)).

    That intersection -- not the raw catalog candidates -- is deliberately
    the safe boundary: every metric shown to the Generator as a
    "candidate" is also a metric the rest of this same request's context
    (Markdown, known_prometheus_metrics, label discovery) already
    recognizes as belonging to a reference the Router actually matched --
    never a catalog-only metric whose Markdown was never loaded into this
    prompt at all.

    Returns a `Level1Outcome` rather than a bare tuple (Batch 3, Phase 5)
    because an empty result has two importantly different causes that
    `run_pipeline` must treat differently:

      - a catalog miss or a low-confidence top score: Level 1 simply had
        no information for this question -- `reference_universe_mismatch=
        False`, and this behaves exactly like the feature being off (no
        candidate block, no Level-1/2/3 machinery engaged at all).
      - a CONFIDENT catalog hit whose candidates share ZERO overlap with
        known_prometheus_metrics: `reference_universe_mismatch=True`. This
        is not "no information" -- the catalog has a confident opinion,
        and it disagrees entirely with the reference(s) the Router
        selected. Broadening metric_metadata within that same,
        already-disagreeing, reference universe (ordinary Level 2) can
        never recover this, so `run_pipeline` routes it directly to Level
        3 (a fresh, catalog-unnarrowed routing pass) instead of spending a
        Level-1/Level-2 attempt on a reference universe the catalog
        itself contradicts.

    When `matched` is given, Level 1 is additionally scoped to the metrics
    of those Router-matched reference file(s) (`_matched_reference_metric_
    names`), not the whole exporter's directory: a generic query word such
    as "node" scores every `node_*` metric of the exporter, and without
    this scope the filesystem/memory metrics would be offered for a CPU
    question. That keeps Level 1 a subset of Level 2 by construction. The
    mismatch signal above is unchanged (still zero overlap with
    `known_prometheus_metrics`). If candidates exist in the exporter but
    none in the matched file(s), the Router's file simply is not the one
    the catalog ranked: Level 1 has no usable scope, so this returns an
    empty, non-mismatch outcome (no candidate block, no Level 1/2
    machinery -- the same fail-open as a catalog miss) rather than
    escalating. `matched=None` keeps the exporter-wide behavior.
    """
    decision = catalog_metric_candidates(catalog, question, min_confident_score=min_confident_score)
    if not decision.confident:
        logger.info(
            "catalog_generator_candidates: no confident metric-level candidates for question %r "
            "(top_score=%s, min_confident_score=%s); Generator prompt gets no candidate block "
            "(fail-safe fallback -- identical to no catalog being available for this request).",
            question, decision.top_score, min_confident_score,
        )
        return Level1Outcome(metric_metadata=(), reference_universe_mismatch=False)

    in_scope = tuple(e for e in decision.candidates if e.name in known_prometheus_metrics)
    if not in_scope:
        logger.info(
            "catalog_generator_candidates: confident catalog candidates for question %r were "
            "entirely outside known_prometheus_metrics (top_score=%s) -- a Catalog/reference-"
            "universe mismatch, not an ordinary Level-1 miss (Batch 3 Phase 5: this routes "
            "directly to Level 3 rather than an empty candidate block).",
            question, decision.top_score,
        )
        return Level1Outcome(metric_metadata=(), reference_universe_mismatch=True)

    if matched is not None:
        reference_scope = _matched_reference_metric_names(catalog, matched, known_prometheus_metrics)
        in_reference = tuple(e for e in in_scope if e.name in reference_scope)
        if not in_reference:
            logger.info(
                "catalog_generator_candidates: candidates for question %r exist in the exporter but none "
                "in the Router-matched reference(s) %s (top_score=%s); no Level-1 scope for this request "
                "(fail-open: no candidate block).",
                question, [m.get("reference_path") for m in matched], decision.top_score,
            )
            return Level1Outcome(metric_metadata=(), reference_universe_mismatch=False)
        in_scope = in_reference

    return Level1Outcome(metric_metadata=tuple(_metric_metadata_dict(e) for e in in_scope),
                          reference_universe_mismatch=False)


# ---- Generator --------------------------------------------------------------


_GENERATOR_INSTRUCTIONS = """\
You are the query-construction phase of the observability-query-builder
skill. You implement SKILL.md Section 6, Steps 3-6: select the correct
metric(s) or measurement(s), apply Section 8's parameter defaults for
anything unstated, construct the query for each resolved measurement per
Section 5's Operating Principles (especially Principle 9 -- use ONLY the
live-confirmed label/Attributes keys provided below, never one you infer),
and assemble the final response in EXACTLY the shape defined in Section 9
(Output Contract).

Section 7 (Error Handling and Refusal Conditions) is included below because
Steps 3f/3g of your own procedure resolve directly into two of its statuses
(`ambiguous_metric`, `unsupported_metric`), and Section 7.4 governs exactly
how to construct a `panic_mode_best_effort` result when panic_mode is true
below -- read it before assuming the broadest interpretation is always
correct.

Only use metric names that appear in the Metric Directory / matched
reference content given to you. Only use label keys that appear in the
live-confirmed label list given to you below -- if a scope constraint the
user gave you can't be mapped to a confirmed label key, do not guess; use
declined/parameter_requires_clarification (Section 5 Principle 9, Section
7.2, Section 8) instead of inventing one.

When you classify a request as `ambiguous_metric`, every entry in
`candidates` must be a metric whose OWN documented Purpose genuinely matches
what was asked -- not merely a metric from a topic area that seems related.
For example, if asked "how much memory is being used," a GPU metric that
literally represents used memory is a correct candidate; a host metric like
"available" or "free" memory is NOT a correct candidate for that same
question even though it's in the same general topic, because neither one's
documented Purpose is "used" -- listing it anyway makes the clarification
question less useful, not more thorough. If NO metric in an opened reference
has a Purpose matching what was asked, that reference contributes no
candidate at all (it may still be `unsupported_metric` on its own, per Step
3g, if it was the only reference opened).

CLOSED ENUMS -- do not invent values outside these lists, ever, under any
framing. `status` is one of the exact values listed in Section 9's contract
(`ok`, `panic_mode_best_effort`, `ambiguous_metric`, `unsupported_metric`,
`unmapped`, `declined`, `out_of_scope_action`, and -- when applicable per
Section 12 -- `alert_rule_proposed`). `measurement_used.type` is one of
exactly two values: `"raw_metric"` (a single metric, whether or not
functions like `rate()` / `sum()` are applied to it) or
`"derived_measurement"` (a value computed from two or more distinct
metrics combined). If you find yourself about to write anything else for
either field (e.g. `raw_counter`, `raw_cluster_or_counter`,
`calculated`, `aggregated`), stop -- pick `raw_metric` if it's a single
metric with transformations, or `derived_measurement` if it truly
combines multiple distinct metrics.

`query_type` IS A REQUIRED FIELD on every Prometheus-backed `ok`/
`panic_mode_best_effort` result (Section 8's "Instant vs. range", Section 9)
-- never omit it and never default it to `"range"` out of habit. It is one
of exactly two values: `"instant"` (the question asks for a single current
value -- build a Prometheus instant query, `time_range: {"time": "now"}` or
another single resolvable point) or `"range"` (the question implies a trend,
history, or an explicit time window -- build a Prometheus range query,
`time_range: {"from", "to", "step"}` as before). Decide which one per
Section 8's rules BEFORE constructing `time_range` -- a bare "what is/how
much/how busy" question with no window language is `"instant"`, not a
short-window `"range"` standing in for it.

Respond with ONLY the JSON object matching Section 9's contract -- no other
text, no markdown fences.
"""

_ALERT_GENERATOR_ADDENDUM = """\

ALERT-RULE CREATION (SKILL.md Section 12): the routing phase determined this
request is asking to CREATE a brand-new Grafana alert rule
(action_intent = "propose_alert_rule" -- see below), not to retrieve data.
Resolve the metric using the EXACT same Step 3 procedure as always --
`ambiguous_metric` and `unsupported_metric` still apply exactly as for a
read question if metric selection doesn't cleanly resolve. Once (and only
once) a metric resolves cleanly, build `alert_rule.condition_query` using
THE EXACT SAME Step 5 procedure you would use to build an ordinary read-only
query for this metric (Section 12.4) -- there is no separate "alert query"
concept and no separate per-metric alert-specific field to consult. This is
deliberate: Step 5's own construction discipline (the metric's Query
examples when verified, its Metric-Specific Query/Result Semantics
otherwise, runtime-confirmed label keys per Principle 9, datasource
fundamentals) is already the mechanism trusted not to fabricate a query --
an alert condition is that same expression, compared against a threshold.

1. Build the condition query per Step 5, exactly as you would for a read
   question about this same metric:
   - A verified Query Example exists -> build the condition query the same
     way Step 5 would for a read question; the example may inform
     construction but is never copied verbatim if the request differs from
     it.
   - No verified Query Example, but the metric's query/result semantics are
     otherwise established (its Metric-Specific Query/Result Semantics
     section, or datasource fundamentals) -> build it fresh, exactly as
     Step 5 already allows for a read question; a missing worked example
     does not by itself block construction.
   - That metric's query/result semantics are THEMSELVES stated as
     unverified for the interpretation being asked (Principle 8 -- e.g. an
     unverified exposed unit) -> STOP, exactly as Step 5 already requires
     for a read question about that same interpretation. Classify the
     result as `unsupported_metric`, with `explanation` stating plainly
     what is unverified. This is the ONLY case that blocks alert-condition
     construction -- identical to, never stricter than, what already blocks
     an ordinary read query for that same metric and interpretation. Do NOT
     derive a condition from general domain knowledge about what a
     "sensible" alert for this kind of metric might look like -- only from
     Step 5's own construction procedure.
2. Preserve any scope constraint (entity/device) the user explicitly
   provided, using a live-confirmed label key (Principle 9) exactly as Step
   5 already requires for a read question.
3. The threshold value and comparison operator (>, <, >=, <=, ==, !=) are
   NEVER supplied by a reference file and are never invented by you -- they
   must be explicitly stated by the user. If either is missing, classify as
   `declined`, `reason: "parameter_requires_clarification"`, with a
   `clarification` field asking for the specific missing piece.
   **`comparison.threshold` MUST be a raw JSON number, not a string. Extract
   the numeric value from whatever unit or phrasing the user used and put
   only that number here** -- e.g. the user saying "90%" becomes
   `"threshold": 90` (not `"90"` and not `"90%"`); "85 degrees" becomes
   `"threshold": 85`; "2GB" becomes `"threshold": 2` if the underlying
   metric is in GB, or `"threshold": 2147483648` only if the metric is in
   bytes AND the conversion is unambiguous from the metric's documented
   Unit -- if the unit conversion is at all ambiguous, treat this as a
   missing parameter and use `declined`/`parameter_requires_clarification`
   instead. Same for `alert_rule.for_duration` (how long the condition
   must hold before firing): if the user didn't state one, ask for it
   rather than inventing a plausible-sounding duration like "5m". The
   `for_duration` field IS a string (e.g. `"5m"`), unlike `threshold`.
4. If (and only if) the condition query was successfully built per step 1
   AND the user supplied both a threshold value and a comparison operator,
   assemble
   `status: "alert_rule_proposed"` (Section 9) with `alert_rule.title`,
   `alert_rule.condition_query`, `alert_rule.comparison` (an object with
   `operator` and `threshold`, verbatim from the user), and
   `alert_rule.for_duration` (verbatim from the user). Leave
   `alert_rule.folder` empty/omitted and set `alert_rule.datasource_uid` to
   `null` -- both are filled in deterministically by the surrounding
   application afterward (Section 12.5); inventing either yourself will
   fail validation.

This entire capability applies ONLY when action_intent is
"propose_alert_rule" (given to you explicitly below). Never produce
`status: "alert_rule_proposed"` for an ordinary read question, and never let
its existence change how you would otherwise handle a request to silence,
delete, or modify an EXISTING alert -- that was already resolved as
`out_of_scope_action` by the routing phase before you ever saw this
request.
"""

_DEPENDENCY_GENERATOR_ADDENDUM = """\

DEPENDENT QUERY RESOLUTION IS ENABLED ON THIS DEPLOYMENT. Two narrow
additions to your normal construction procedure -- everything else above
still applies exactly as written:

1. If the measurement you are building is the ROOT of a dependency (an
   earlier part of a compound question whose result a LATER part needs a
   concrete entity from -- e.g. "which node has the highest CPU
   utilization" feeding "...on that node"), and the question asks for a
   highest/lowest/top/bottom entity, build it as an INSTANT top/bottom-N
   query (e.g. `topk(1, ...)` / `bottomk(1, ...)`, `query_type: "instant"`)
   rather than a range query. A range-query top/bottom-N can return the
   union of different winners across the window, which is unsafe to hand
   to a dependent query expecting exactly one resolved entity.
2. If a block below states that THIS measurement is a DEPENDENT part of a
   compound question with an already-resolved entity value (e.g.
   `node_id="node-7"`, taken from an earlier part's real, already-executed
   result), your query for this measurement MUST include an explicit
   equality filter on that exact resolved value -- never omit it, and never
   construct this as an unfiltered query across every instance the metric
   reports for. If that block instead says the upstream result could not
   be resolved, and the measurement cannot be meaningfully answered without
   it, respond with `status: "unsupported_metric"` (explaining that the
   required upstream value was unavailable) rather than guessing or
   returning an unfiltered query.
"""


def _build_generator_instructions(settings: Settings, action_intent: str) -> str:
    """Mirrors _build_router_instructions' gating: the alert-rule-creation
    addendum is appended ONLY when that feature flag is on AND this
    specific request was flagged by the Router as alert-rule creation; the
    dependency-resolution addendum is appended ONLY when ITS feature flag
    is on (independent of the alert-rule flag/action_intent -- see this
    module's docstring and app/config.py's `dependent_query_resolution_
    enabled`). An ordinary read question on a deployment with neither flag
    enabled gets the exact same `_GENERATOR_INSTRUCTIONS` string every
    prior version of this pipeline used, unchanged."""
    instructions = _GENERATOR_INSTRUCTIONS
    if settings.alert_rule_creation_enabled and action_intent == _ALERT_ACTION_INTENT:
        instructions += _ALERT_GENERATOR_ADDENDUM
    if settings.dependent_query_resolution_enabled:
        instructions += _DEPENDENCY_GENERATOR_ADDENDUM
    return instructions


async def _run_generator(question: str, matched: list[dict], panic_mode: bool,
                          ctx: GeneratorContext, skill_index: SkillIndex, settings: Settings,
                          context: PipelineContext, action_intent: str = "read_query",
                          parent_context_text: str | None = None,
                          stage_scope_text: str | None = None) -> dict:
    section_headers = ["## 5.", "## 6.", "## 7.", "## 8.", "## 9."]
    if settings.alert_rule_creation_enabled and action_intent == _ALERT_ACTION_INTENT:
        section_headers.append("## 12.")
    sections = "\n\n".join(skill_index.section(h) for h in section_headers)

    ref_blocks = "\n\n".join(f"--- {path} ---\n{text}" for path, text in ctx.reference_texts.items())
    overview_blocks = "\n\n".join(f"--- {path} ---\n{text}" for path, text in ctx.overview_texts.items())
    fundamentals_blocks = "\n\n".join(f"--- {path} ---\n{text}" for path, text in ctx.fundamentals_texts.items())

    prompt_parts = [
        f"SKILL.md reference sections:\n\n{sections}",
        f"\nMatched reference file(s):\n\n{ref_blocks}",
    ]
    if overview_blocks:
        prompt_parts.append(f"\nParent exporter overview(s) (for Metric Directory lookup):\n\n{overview_blocks}")
    if ctx.metric_metadata:
        prompt_parts.append(f"\n{_format_metric_metadata_for_prompt(ctx.metric_metadata)}")
    if fundamentals_blocks:
        prompt_parts.append(f"\nDatasource fundamentals:\n\n{fundamentals_blocks}")
    if ctx.labels_block:
        prompt_parts.append(f"\n{ctx.labels_block}")
    if ctx.attributes_block:
        prompt_parts.append(f"\n{ctx.attributes_block}")
    prompt_parts.append(f"\nUser question: {question}")
    prompt_parts.append(f"\npanic_mode is currently: {panic_mode}")
    prompt_parts.append(f"\naction_intent (from the routing phase): {action_intent!r}")
    if stage_scope_text:
        # Dependency-aware compound query resolution ONLY: tells this
        # call exactly which intent(s) of the (still fully-quoted) compound
        # question it resolves, and which it must leave to another stage.
        # None for every ordinary call site, so their prompts are unchanged.
        prompt_parts.append(f"\n{stage_scope_text}")
    if parent_context_text:
        # Dependency-aware compound query resolution ONLY (see this
        # module's docstring) -- a purely additive block naming this
        # measurement's already-executed parent's resolved entity
        # label/value (or explicitly stating that none could be resolved).
        # None for every other call site, including every ordinary/root
        # Generator call, so this changes nothing about their prompts.
        prompt_parts.append(f"\n{parent_context_text}")
    if context.previous_question:
        prompt_parts.append(
            f"\nThis is a follow-up clarification. Original question: "
            f"{context.previous_question}\nUser's clarifying answer: "
            f"{context.clarification_answer}\nResolve using the FULL combined intent."
        )

    prompt = "\n\n".join(prompt_parts)
    response = llm_client.call_llm_json(
        prompt=prompt,
        system_instruction=_build_generator_instructions(settings, action_intent),
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    return response.parsed


# ---- Batch 3 catalog integration (Phases 7-9) ------------------------------
#
# Explicit architectural note (carried over from Batch 2 review): the
# catalog these functions consume is agnostic to how it was produced.
# app/catalog/generator.py's Markdown-parsing generator remains the
# BOOTSTRAP mechanism for the 43 metrics this project already had
# hand-authored Markdown for; it is not meant to become the permanent
# gate for onboarding new metrics (see app/catalog/reconciler.py's module
# docstring for the intended ongoing path). Nothing below cares which
# mechanism produced app/catalog/catalog.json -- it just loads whatever
# Catalog document is there.

_catalog_cache: Catalog | None = None
_catalog_load_failed_path: str | None = None


def _format_metric_metadata_for_prompt(metric_metadata: tuple[dict[str, str | None], ...]) -> str:
    """Renders `GeneratorContext.metric_metadata` as an explicit,
    clearly-scoped block in the Generator's prompt. Framed as basic
    identity/description information layered ON TOP of the Markdown
    context above (never a replacement for it, and never as authoritative
    for query semantics, Use-when guidance, or confusable-metric
    distinctions -- those remain exclusively the matched reference file's
    job, per the frozen architecture's §5). Deliberately does not repeat
    keywords/priority/status/exporter/reference_path -- those never reach
    this structure in the first place (see `_metric_metadata_dict`)."""
    lines = [
        "Catalog-identified candidate metrics for this question (structured basic "
        "identity metadata only -- name, Prometheus type, HELP text, and unit). "
        "These are the metrics currently eligible for selection for this question. "
        "This is descriptive/identity information ONLY -- it is not a substitute for "
        "the matched reference Markdown above, which remains authoritative for "
        "Use-when guidance, confusable-metric distinctions, query construction, and "
        "all other semantic interpretation. If, after consulting the Markdown above, "
        "none of these candidates is actually the right metric for this question, "
        "respond with status 'unsupported_metric' rather than selecting a metric "
        "outside this list:",
    ]
    for m in metric_metadata:
        unit = m["unit"] if m["unit"] else "n/a"
        lines.append(f"- {m['name']} (type: {m['type']}, unit: {unit}) -- {m['help']}")
    return "\n".join(lines)


def _get_catalog(settings: Settings) -> Catalog | None:
    """Lazily loads and caches the catalog from `settings.catalog_path`,
    then applies Phase 5's rules (app/catalog/rules.py) to it in memory
    before caching. The on-disk catalog.json committed by generator.py
    (Phase 2) is deliberately left as pure, source-derived bootstrap
    output -- keywords/priority classification is applied here, at load
    time, rather than baked into the committed artifact, so the two
    concerns (what generator.py derived from Markdown vs. what rules.py
    classified) stay visibly separate and re-running apply_rules() here
    can never drift from what's actually committed to the repo.

    Returns None (never raises) if the file is missing or invalid -- a
    broken or absent catalog.json must degrade every catalog-dependent
    behavior (Router narrowing, Generator Level-1/2/3 candidates, catalog
    status validation) to its pre-catalog no-op, never crash a request or
    block startup, matching the "catalog integration is additive"
    principle -- catalog participation being MANDATORY (Batch 4) means
    mandatory to ATTEMPT, never mandatory to SUCCEED. Logs the failure
    once per distinct path, not on every request, to avoid log-flooding a
    deployment that has simply not generated a catalog yet."""
    global _catalog_cache, _catalog_load_failed_path
    if _catalog_cache is not None:
        return _catalog_cache
    path_str = str(settings.catalog_path)
    if _catalog_load_failed_path == path_str:
        return None
    try:
        loaded = load_catalog(settings.catalog_path)
    except (CatalogLoadError, CatalogSchemaError, OSError) as e:
        logger.warning(
            "Catalog integration (Router narrowing / Generator candidates) disabled: "
            "failed to load catalog at %s: %s", settings.catalog_path, e,
        )
        _catalog_load_failed_path = path_str
        return None
    # Batch 2 (Option C): Markdown-Category -> keyword enrichment is
    # additive vocabulary only (see app/catalog/rules.py's own
    # docstring) -- computed here, alongside the existing apply_rules()
    # call, rather than baked into the committed catalog.json, for the
    # same reason keywords/priority already are: it must never silently
    # drift from what generate_production_catalog/CatalogEntry.category
    # actually persisted (Vendor Category), and it must degrade cleanly
    # (no enrichment, not a crash) if skills_root has no/partial Markdown
    # -- build_markdown_category_index() already returns {} in that case.
    markdown_category_index = build_markdown_category_index(settings.skills_root)
    _catalog_cache = catalog_rules.apply_rules(
        loaded, markdown_category_index=markdown_category_index
    )
    return _catalog_cache


def reset_catalog_cache() -> None:
    """Clears the module-level catalog cache. Exists for tests (and for a
    future admin/reload endpoint mirroring SkillIndex's own reload_skill())
    that need a fresh load after catalog.json changes on disk -- ordinary
    request handling never needs to call this."""
    global _catalog_cache, _catalog_load_failed_path
    _catalog_cache = None
    _catalog_load_failed_path = None


def _catalog_status_by_metric(catalog: Catalog) -> dict[str, str]:
    """Phase 12 (Batch 4): the minimal additive interface validator.py needs
    -- {metric_name: catalog_status_string} -- rather than threading the
    entire Catalog object through _finalize/validate_contract. Built fresh
    from the (already-cached-by-_get_catalog) Catalog on every request that
    opts in; cheap (43 entries today, bounded by catalog size generally)
    and avoids validator.py importing anything beyond
    app.catalog.schema.CatalogStatus for its own comparison."""
    return {m.name: m.status for m in catalog.metrics}


def _catalog_covered_reference_paths(catalog: Catalog) -> set[str]:
    """Every reference_path the catalog has at least one entry for. Section
    4 rows outside this set (overview.md rows, *-fundamentals.md rows,
    execution-contract.md's n/a row, and any future domain the catalog
    doesn't cover yet) are rows the catalog has no opinion about at all --
    see _maybe_narrow_section4's docstring for why those are always kept."""
    return {m.reference_path for m in catalog.metrics if m.reference_path}


def _narrow_section4_text(section4_text: str, allowed_reference_paths: set[str]) -> str:
    """Filters SKILL.md's Section 4 table down to only the data rows whose
    linked reference_path is in `allowed_reference_paths`, preserving every
    non-data-row line (headers, the intro paragraph, the separator row, the
    trailing "Keyword rule..." guidance) byte-for-byte and unconditionally.
    Rows are matched and dropped by filtering the ORIGINAL markdown lines
    (via skill_index.ROUTING_ROW_RE, the same regex SkillIndex itself
    parses Section 4 with -- not a second, duplicated pattern) rather than
    regenerating row text from RoutingRow fields, so every kept row is
    guaranteed byte-identical to what the Router has always seen for that
    row.
    """
    kept_lines: list[str] = []
    for line in section4_text.splitlines():
        stripped = line.strip()
        is_table_row = stripped.startswith("|")
        if not is_table_row:
            kept_lines.append(line)
            continue
        if stripped.startswith("|---") or stripped.startswith("| ---"):
            kept_lines.append(line)
            continue
        if stripped.lower().startswith("| route when"):
            kept_lines.append(line)
            continue
        m = ROUTING_ROW_RE.match(stripped)
        if not m:
            # Doesn't parse as a routing row at all -- keep it rather than
            # risk silently dropping content this function doesn't
            # recognize; narrowing should only ever remove rows it is
            # confident are catalog-covered-but-not-suggested.
            kept_lines.append(line)
            continue
        link_path = m.group("link_path").strip()
        if link_path in allowed_reference_paths:
            kept_lines.append(line)
        # else: dropped -- a catalog-covered domain row the catalog did not
        # suggest as relevant to this specific question.
    return "\n".join(kept_lines)


# Batch 4 (Phase 10) finding: a non-empty catalog search result is not
# necessarily a safe routing result. search.py's own top_n truncates to
# DEFAULT_TOP_N=5 by design (a sane "show the user 5 candidates" cutoff),
# but Phase 9's narrowing decision isn't picking candidates to show a
# person -- it's deciding which Section 4 rows the Router is even allowed
# to see. Truncating that decision to 5 means a genuinely correct
# reference that would have scored, say, 6th, is silently dropped from the
# Router's options entirely. Measured on the Batch 3 question set: capping
# at top_n=5 loses the correct reference in 13/46 cases, but running the
# exact same search with this uncapped top_n over the same 46 questions
# loses it in only 6/46 -- almost all of the "miss" rate at top_n=5 was
# rank-truncation, not genuine zero-overlap. Narrowing therefore always
# searches uncapped (bounded only by catalog size, never by rank) and
# leans on `min_confident_score` instead of a rank cutoff to control what
# counts as "suggested" -- consistent with search.py's own stated
# principle that "False positives/over-inclusion are preferable to false
# negatives," applied here to the narrowing decision specifically.
_NARROW_SEARCH_TOP_N = 1_000_000  # i.e. "no rank cap" -- see above.


@dataclass(frozen=True)
class NarrowingDecision:
    """Pure result of a catalog-narrowing lookup, independent of SkillIndex/
    Section 4 text -- kept separate from `_maybe_narrow_section4` so
    scripts/evaluate_catalog_retrieval.py (and tests) can measure/exercise
    the actual narrowing decision directly, rather than re-deriving it from
    parsed markdown output, which would risk drifting from what production
    actually decides."""
    confident: bool
    suggested_paths: frozenset[str]
    top_score: float | None


def catalog_narrowing_candidates(catalog: Catalog, question: str,
                                  min_confident_score: float) -> NarrowingDecision:
    """The actual narrowing decision Phase 9 acts on: uncapped-by-rank
    catalog search (see `_NARROW_SEARCH_TOP_N` above), gated by a
    conservative confidence floor on the TOP candidate's score.

    Two distinct "don't narrow" cases, both folded into `confident=False`
    for the caller (either one falls back to the full, unnarrowed Section
    4 the same way):

      - True catalog miss: zero candidates score above search.py's own
        zero-overlap floor at all. Per §2.6, always falls back.
      - Low-confidence hit: candidates exist, but even the best one scores
        below `min_confident_score` -- e.g. a single incidental "help" or
        "exporter" token match (search.py's own docstring flags free-prose
        help text as "most likely to contain incidental word overlap").
        Batch 4 measurement: this floor cannot, by itself, fix cases where
        an unrelated metric scores moderately-to-highly on genuine
        name/keyword token overlap (that is a weight-precision problem for
        a future tuning phase, not a confidence-threshold problem) -- it
        specifically guards the weak end of the score range against acting
        on what is essentially noise.

    Uses search.py's default status filter (approved + approved_unavailable
    only -- discovered_pending_review excluded), so an unreviewed
    runtime-only metric can never influence what the Router even sees,
    consistent with the Batch 2 clarification that pending-review metrics
    must not become eligible for query generation.
    """
    results = catalog_search.search(catalog, question, top_n=_NARROW_SEARCH_TOP_N)
    if not results:
        return NarrowingDecision(confident=False, suggested_paths=frozenset(), top_score=None)

    top_score = results[0].score
    if top_score < min_confident_score:
        return NarrowingDecision(confident=False, suggested_paths=frozenset(), top_score=top_score)

    suggested_paths = frozenset(r.entry.reference_path for r in results if r.entry.reference_path)
    return NarrowingDecision(confident=True, suggested_paths=suggested_paths, top_score=top_score)


@dataclass(frozen=True)
class MetricCandidateDecision:
    """Pure result of a metric-level catalog-narrowing lookup -- the
    Level-1 Generator-candidate counterpart to `NarrowingDecision` above.
    Deliberately a separate type (rather than reusing `NarrowingDecision`)
    because it carries full `CatalogEntry` objects, not just reference
    paths: `_build_generator_context` needs each candidate's name/type/
    help/unit to populate `GeneratorContext.metric_metadata`, whereas the
    Router-facing decision only ever needed to know which Section 4 rows
    to keep."""
    confident: bool
    candidates: tuple = ()
    top_score: float | None = None


def catalog_metric_candidates(catalog: Catalog, question: str,
                               min_confident_score: float) -> MetricCandidateDecision:
    """Batch 1 (Phase 2)'s Level-1 candidate lookup: the metric-level
    analogue of `catalog_narrowing_candidates` above, reusing the exact
    same two safety nets (uncapped-by-rank search via
    `_NARROW_SEARCH_TOP_N`, then a confidence floor on the TOP candidate's
    score) and the same default status filter (search.py's
    `DEFAULT_STATUSES` -- approved + approved_unavailable only), but
    returning the matching `CatalogEntry` objects themselves instead of a
    deduplicated set of reference paths, since that is what
    `_build_generator_context` needs to build `metric_metadata`.

    `confident=False` (an empty `candidates` tuple) covers the same two
    cases `catalog_narrowing_candidates` distinguishes in its own
    docstring -- a true catalog miss, or a low-confidence hit -- and the
    caller must treat both identically: fall back to today's unnarrowed
    Generator-context behavior for this request, never surface an error
    or an empty-but-mandatory candidate list.
    """
    results = catalog_search.search(catalog, question, top_n=_NARROW_SEARCH_TOP_N)
    if not results:
        return MetricCandidateDecision(confident=False, candidates=(), top_score=None)

    top_score = results[0].score
    if top_score < min_confident_score:
        return MetricCandidateDecision(confident=False, candidates=(), top_score=top_score)

    return MetricCandidateDecision(confident=True, candidates=tuple(r.entry for r in results),
                                    top_score=top_score)


def _metric_metadata_dict(entry) -> dict[str, str | None]:
    """Projects one `CatalogEntry` down to exactly the four fields the
    frozen architecture allows onto `GeneratorContext.metric_metadata`
    (name/type/help/unit) -- deliberately a plain dict built field-by-field
    rather than passing the `CatalogEntry` through directly, so that
    keywords/priority/status/exporter/reference_path can never leak into
    the Generator-facing structure even by accident (e.g. a future
    CatalogEntry field addition automatically reaching the Generator)."""
    return {"name": entry.name, "type": entry.type, "help": entry.help, "unit": entry.unit}


def _maybe_narrow_section4(section4_text: str, skill_index: SkillIndex,
                            catalog: Catalog, question: str,
                            min_confident_score: float) -> str:
    """Phase 9 (revised in Phase 10/Batch 4). Narrows Section 4's text to
    catalog-suggested rows for this specific question, with three
    independent safety nets, any one of which alone is enough to guarantee
    "never narrow away something that might matter":

      1. Only rows whose reference_path the catalog actually covers (today:
         the 8 node-exporter/dcgm-exporter domain files) are eligible to be
         dropped at all. Every other row -- both overview.md rows, both
         *-fundamentals.md rows, and execution-contract.md's row -- is
         always kept, because the catalog has no opinion about them
         (§2.7: "Catalog narrows what SkillIndex provides to Router. It
         does not replace Router.").
      2. A catalog MISS for this question (zero candidates at all) returns
         `section4_text` completely unchanged -- per §2.6, "A catalog miss
         must fall back to the existing full routing path. Never convert a
         catalog miss into 'unsupported.'"
      3. (New, Batch 4) A low-confidence hit -- candidates exist, but none
         clear `min_confident_score` -- is treated identically to a miss.
         See `catalog_narrowing_candidates`'s docstring for why a
         non-empty result isn't, by itself, a safe basis for narrowing.
    """
    decision = catalog_narrowing_candidates(catalog, question, min_confident_score)
    if not decision.confident:
        logger.info(
            "catalog_assisted_routing: no confident candidates for question %r "
            "(top_score=%s, min_confident_score=%s); using the full, unnarrowed "
            "Section 4 (fail-safe fallback).",
            question, decision.top_score, min_confident_score,
        )
        return section4_text

    covered_paths = _catalog_covered_reference_paths(catalog)
    all_paths = set(skill_index.reference_paths())
    uncovered_paths = all_paths - covered_paths
    allowed_paths = decision.suggested_paths | uncovered_paths

    narrowed = _narrow_section4_text(section4_text, allowed_paths)
    logger.info(
        "catalog_assisted_routing: narrowed Section 4 for question %r -- "
        "catalog-suggested=%s, always-kept (uncovered)=%s, top_score=%s",
        question, sorted(decision.suggested_paths), sorted(uncovered_paths), decision.top_score,
    )
    return narrowed


# ---- helpers --------------------------------------------------------------------


def _wrap_gate_stop(gate_stop: dict) -> dict:
    result = {"mode": "single", "status": gate_stop["status"]}
    for key in ("reason", "requested_action", "clarification", "explanation", "candidates"):
        if gate_stop.get(key) is not None:
            result[key] = gate_stop[key]
    return result