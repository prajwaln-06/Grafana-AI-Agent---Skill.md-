"""
pipeline.py

Orchestrates the full request lifecycle against the observability-query-
builder skill package: Router -> Generator -> deterministic Validator ->
Executor.

The default flat path uses two LLM calls, not three. The previous version of
this pipeline spent a third Gemini call re-checking the Generator's own output
against rules that are almost entirely mechanical (closed enums, required
fields, a query being non-empty, a label key belonging to a fixed discovered
list). See validator.py's module docstring for the full reasoning -- that
phase is now plain Python, which makes it strictly deterministic (same
contract always validates the same way), faster, cheaper, and not dependent on
a second model call succeeding. The opt-in dependency-aware path intentionally
adds one narrow Generator call per dependent intent, after its upstream query
has executed; its post-execution synthesis remains deterministic Python.

  ROUTER     -- SKILL.md Section 6 Steps 1-2 (gate, then route against
                Section 4). ALSO identifies any part of a compound question
                that has no covering reference at all -- see "Partial
                datasource coverage" below.
  GENERATOR  -- SKILL.md Section 6 Steps 3-6 (select metric(s), apply
                Section 8's parameter defaults, construct the query/queries,
                assemble the mode/results shape). Given the full content of
                every matched reference, its sibling overview.md (for the
                Metric Directory), the relevant *-fundamentals.md file(s),
                Section 7 (error handling -- needed for panic-mode framing),
                and live-discovered label keys / Attributes keys.
  VALIDATOR  -- no LLM. See validator.py.
  EXECUTOR   -- no LLM. See executor.py. In the opt-in dependency-aware
                branch, roots execute before each dependent Generator pass.

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
"""
from __future__ import annotations

import logging
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta

from app import executor, field_discovery, label_discovery, llm_client, validator
from app.config import Settings
from app.skill_index import SkillIndex

logger = logging.getLogger(__name__)

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

# This deliberately narrow, deterministic backstop protects the feature-off
# boundary when an LLM omits `action_intent` despite an unambiguous creation
# request. It is not the normal alert-intent classifier (the Router remains
# that when the feature is enabled); it merely recognizes direct creation
# wording that must never be allowed to reach alert proposal construction in
# a disabled deployment.
_EXPLICIT_ALERT_CREATION_RE = re.compile(
    r"\b(?:create|add|set\s+up|setup|configure|define|make)\s+"
    r"(?:an?\s+)?(?:new\s+)?(?:grafana\s+)?alert(?:\s+rule)?\b"
    r"|\b(?:alert|notify)\s+me\s+(?:when|if)\b"
    r"|\b(?:i\s+(?:want|need)|please)\s+(?:an?\s+)?alert(?:\s+rule)?\s+(?:when|if|for)\b",
    re.IGNORECASE,
)


@dataclass
class PipelineContext:
    """Optional prior-turn context for the clarification/follow-up flow
    (session_store.py populates this from a stored session)."""
    previous_question: str | None = None
    previous_result: dict | None = None
    clarification_answer: str | None = None


@dataclass
class PipelineExecutionResult:
    """The feature-flagged staged path's result for the ADK wrapper.

    `already_executed` is deliberately transport-level metadata, not part of
    SKILL.md's user-facing Output Contract.  It lets agent.py avoid running a
    second flat executor pass after this module has already staged roots and
    dependents.
    """
    contract: dict
    already_executed: bool = False


@dataclass
class _RoutedRequest:
    unresolved_topics: list[dict]
    matched: list[dict] = field(default_factory=list)
    panic_mode: bool = False
    action_intent: str = "read_query"
    terminal_contract: dict | None = None


async def run_pipeline(question: str, skill_index: SkillIndex, settings: Settings,
                        context: PipelineContext | None = None) -> dict:
    context = context or PipelineContext()

    routed = await _route_request(question, skill_index, settings, context)
    if routed.terminal_contract is not None:
        return routed.terminal_contract
    return await _generate_standard_contract(question, routed, skill_index, settings, context)


async def _route_request(question: str, skill_index: SkillIndex, settings: Settings,
                         context: PipelineContext) -> _RoutedRequest:
    """Runs the common Router/gate portion shared by flat and staged paths.

    Keeping this before the feature branch is what retains the Router's
    exactly-once property even for a dependency-aware request.
    """

    router_output = await _run_router(question, skill_index, settings, context)
    unresolved_topics = _clean_unresolved_topics(router_output.get("unresolved_topics"))

    if (not settings.alert_rule_creation_enabled
            and _is_explicit_alert_creation_request(question)):
        # The Router normally identifies this as an out-of-scope action while
        # the flag is off. Do not make that model behavior the sole security
        # boundary: it can omit the optional action_intent annotation, after
        # which the Generator's general output schema still contains the
        # alert_rule_proposed status. Preserve Router-once semantics, but
        # refuse the explicit disabled action before Generator construction.
        logger.warning(
            "Refusing explicit alert-rule creation request while "
            "alert_rule_creation_enabled is False: %r", question,
        )
        contract = _disabled_alert_creation_contract()
        return _RoutedRequest(
            unresolved_topics=unresolved_topics,
            terminal_contract=_finalize(contract, unresolved_topics),
        )

    if router_output.get("gate_stop"):
        contract = _wrap_gate_stop(router_output["gate_stop"])
        return _RoutedRequest(unresolved_topics=unresolved_topics,
                              terminal_contract=_finalize(contract, unresolved_topics))

    matched = router_output.get("matched_references", [])
    panic_mode = bool(router_output.get("panic_mode", False))
    action_intent = router_output.get("action_intent") or "read_query"

    if action_intent == _ALERT_ACTION_INTENT and not settings.alert_rule_creation_enabled:
        # Defense in depth, not the primary control: with the feature flag
        # off, _build_router_instructions never tells the Router this field
        # exists at all (see below), so the Router should never produce it.
        # If it somehow does anyway (a stale prompt cache, a Router that
        # ignored its instructions, etc.), fail closed to the EXACT same
        # behavior this request would have gotten before SKILL.md Section 12
        # existed -- out_of_scope_action -- rather than trusting an
        # LLM-supplied flag the deployment hasn't opted into. This keeps the
        # "flag off = zero behavior change" guarantee true even under a
        # Router misfire, not just under normal operation.
        logger.warning(
            "Router returned action_intent=%r for question %r but "
            "alert_rule_creation_enabled is False on this deployment -- forcing a "
            "deterministic out_of_scope_action response instead of trusting the Router. "
            "This should not happen while the flag is off (the addendum that even "
            "mentions this field is never included in the Router's prompt); investigate "
            "if this recurs.", action_intent, question,
        )
        contract = _wrap_gate_stop({
            "status": "out_of_scope_action",
            "requested_action": "create a new Grafana alert rule",
            "explanation": (
                "Alert-rule creation is currently disabled on this deployment; this skill "
                "only constructs/runs read-only queries."
            ),
        })
        return _RoutedRequest(unresolved_topics=unresolved_topics,
                              terminal_contract=_finalize(contract, unresolved_topics))

    if not matched:
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
            contract = {"mode": "multi", "results": [], "synthesis": None}
        else:
            # True Router/prompt problem: no gate_stop, no matched
            # reference, AND no unresolved_topics describing why. Nothing
            # here to guess through -- fall back to a generic unmapped
            # response and log loudly, since a rising rate of this specific
            # path means the Router's prompt needs attention.
            logger.warning(
                "Router returned zero matched_references, zero unresolved_topics, and no "
                "gate_stop for question %r. This is a Router/prompt problem, not a case to "
                "guess through -- falling back to a generic unmapped response.", question,
            )
            contract = _wrap_gate_stop({"status": "unmapped", "explanation": _unmapped_explanation()})
        return _RoutedRequest(unresolved_topics=unresolved_topics,
                              terminal_contract=_finalize(contract, unresolved_topics))

    return _RoutedRequest(
        unresolved_topics=unresolved_topics,
        matched=matched,
        panic_mode=panic_mode,
        action_intent=action_intent,
    )


async def _generate_standard_contract(question: str, routed: _RoutedRequest,
                                      skill_index: SkillIndex, settings: Settings,
                                      context: PipelineContext) -> dict:
    """The legacy one-shot Generator/Validator path, factored without changing it."""
    matched = routed.matched
    generator_context = _build_generator_context(matched, skill_index, settings)
    contract = await _run_generator(question, matched, routed.panic_mode, generator_context, skill_index, settings,
                                     context, action_intent=routed.action_intent)
    contract = _normalize_contract_shape(contract)
    contract = _block_disabled_alert_proposals(contract, settings)
    contract = _apply_alert_rule_defaults(contract, settings)

    known_references = set(generator_context.reference_texts.keys()) | set(generator_context.overview_texts.keys())
    known_datasources = {(m.get("data_source") or "").strip().lower() for m in matched}

    return _finalize(
        contract, routed.unresolved_topics,
        known_metrics=generator_context.known_prometheus_metrics,
        labels_by_metric=generator_context.labels_by_metric,
        known_references=known_references,
        known_datasources=known_datasources,
    )


# ---- Dependency-aware compound resolution (feature-flagged) -----------------


@dataclass
class _IntentPlan:
    intent_id: str
    matched_references: list[dict]
    depends_on: list[str]


async def run_dependency_aware_pipeline(
    question: str,
    skill_index: SkillIndex,
    settings: Settings,
    context: PipelineContext | None = None,
) -> PipelineExecutionResult:
    """Opt-in staged Router -> Generator -> Validator -> Executor flow.

    The default path deliberately remains `run_pipeline()` followed by the
    Agent's one executor call.  Only a Router plan with an explicit, valid
    dependency reaches this function's staged branch.  Root intents are
    generated and executed first; dependent intents receive only normalized
    already-executed values before their own narrower generation/execution.
    """
    context = context or PipelineContext()
    routed = await _route_request(question, skill_index, settings, context)
    if routed.terminal_contract is not None:
        return PipelineExecutionResult(routed.terminal_contract)

    plan = _dependency_plan_from_matches(routed.matched)
    if plan is None or not any(intent.depends_on for intent in plan):
        # Even when the deployment has opted in, independent compounds use
        # the legacy one-shot construction path. agent.py adds best-effort
        # deterministic synthesis only after its ordinary executor pass.
        return PipelineExecutionResult(
            await _generate_standard_contract(question, routed, skill_index, settings, context)
        )

    return await _run_staged_dependency_plan(question, routed, plan, skill_index, settings, context)


def _dependency_plan_from_matches(matched: list[dict]) -> list[_IntentPlan] | None:
    """Builds an ordered, acyclic intent graph from opt-in Router metadata.

    Returning None is intentionally fail-safe: malformed metadata never
    creates an invented relationship or changes the legacy execution shape.
    """
    groups: dict[str, _IntentPlan] = {}
    saw_dependency_field = False
    for item in matched:
        if not isinstance(item, dict):
            return None
        intent_id = item.get("intent_id")
        depends_on = item.get("depends_on")
        if intent_id is None and depends_on is None:
            continue
        saw_dependency_field = True
        if not isinstance(intent_id, str) or not intent_id or not isinstance(depends_on, list):
            return None
        if not all(isinstance(parent, str) and parent for parent in depends_on):
            return None
        existing = groups.get(intent_id)
        if existing is None:
            groups[intent_id] = _IntentPlan(intent_id, [item], list(depends_on))
        elif existing.depends_on == depends_on:
            existing.matched_references.append(item)
        else:
            return None

    if not saw_dependency_field or not groups:
        return None
    if len(groups) == 1 and next(iter(groups.values())).depends_on:
        return None
    if set(groups) != {item.get("intent_id") for item in matched if isinstance(item, dict)}:
        return None
    for intent in groups.values():
        if intent.intent_id in intent.depends_on or any(parent not in groups for parent in intent.depends_on):
            return None

    # Kahn-style validation while preserving the Router's first-seen order.
    remaining = {intent.intent_id: set(intent.depends_on) for intent in groups.values()}
    ordered: list[_IntentPlan] = []
    while remaining:
        ready = [intent_id for intent_id, parents in remaining.items() if not parents]
        if not ready:
            logger.warning("Ignoring cyclic dependency metadata from Router: %r", matched)
            return None
        for intent_id in ready:
            ordered.append(groups[intent_id])
            del remaining[intent_id]
        ready_set = set(ready)
        for parents in remaining.values():
            parents.difference_update(ready_set)
    return ordered


async def _run_staged_dependency_plan(question: str, routed: _RoutedRequest,
                                      plan: list[_IntentPlan], skill_index: SkillIndex,
                                      settings: Settings, context: PipelineContext) -> PipelineExecutionResult:
    roots = [intent for intent in plan if not intent.depends_on]
    root_matches = [reference for intent in roots for reference in intent.matched_references]
    root_ctx = _build_generator_context(root_matches, skill_index, settings)
    root_contract = await _run_generator(
        question, root_matches, routed.panic_mode, root_ctx, skill_index, settings, context,
        action_intent=routed.action_intent,
        resolution_ids=[intent.intent_id for intent in roots],
    )
    root_contract = _normalize_contract_shape(root_contract)
    root_contract = _block_disabled_alert_proposals(root_contract, settings)
    root_contract = _apply_alert_rule_defaults(root_contract, settings)
    root_contract = _finalize(root_contract, [], **_validator_kwargs(root_ctx, root_matches))
    root_entries_by_id = _stage_entries_by_id(root_contract, {intent.intent_id for intent in roots})
    if root_entries_by_id is None:
        return PipelineExecutionResult(_dependency_validation_failure(
            "The root dependency-resolution stage did not return one valid result for each routed intent."
        ))

    root_entries = [_strip_resolution_id(root_entries_by_id[intent.intent_id]) for intent in roots]
    root_visible_contract = _contract_for_entries(root_entries, routed.unresolved_topics)
    if any(_entry_needs_clarification(entry) for entry in root_entries):
        # Do not execute a partial root set while the normal clarification
        # flow is active; agent.py will preserve the same session behavior as
        # the flat path.
        return PipelineExecutionResult(_finalize(
            root_visible_contract, [], **_validator_kwargs(root_ctx, root_matches)
        ))

    executed_roots = executor.execute_contract(_contract_for_entries(root_entries), settings)
    completed: dict[str, dict] = {
        intent.intent_id: entry
        for intent, entry in zip(roots, _entries_from_contract(executed_roots), strict=True)
    }

    for intent in plan:
        if not intent.depends_on:
            continue
        parents = [completed[parent] for parent in intent.depends_on]
        if not all(_has_usable_execution(parent) for parent in parents):
            completed[intent.intent_id] = _dependency_scope_unavailable_entry(intent)
            continue

        dep_ctx = _build_generator_context(intent.matched_references, skill_index, settings)
        dependent_contract = await _run_generator(
            question, intent.matched_references, routed.panic_mode, dep_ctx, skill_index, settings, context,
            action_intent=routed.action_intent,
            resolution_ids=[intent.intent_id],
            resolved_dependencies=_dependency_prompt_data(intent.depends_on, parents),
        )
        dependent_contract = _normalize_contract_shape(dependent_contract)
        dependent_contract = _block_disabled_alert_proposals(dependent_contract, settings)
        dependent_contract = _apply_alert_rule_defaults(dependent_contract, settings)
        dependent_contract = _finalize(
            dependent_contract, [], **_validator_kwargs(dep_ctx, intent.matched_references)
        )
        stage_entries = _stage_entries_by_id(dependent_contract, {intent.intent_id})
        if stage_entries is None:
            completed[intent.intent_id] = _dependency_validation_failure_entry(intent)
            continue
        entry = _strip_resolution_id(stage_entries[intent.intent_id])
        if not _query_contains_resolved_scope(entry, parents):
            entry = _dependency_scope_unavailable_entry(intent)
        completed[intent.intent_id] = executor.execute_contract(entry, settings)

    entries = [completed[intent.intent_id] for intent in plan]
    final_contract = _contract_for_entries(entries, routed.unresolved_topics)
    # Each generated stage was validated against exactly its own opened
    # references before execution. `_contract_for_entries` only assembles
    # those valid entries and the same fixed unresolved-topic entries that
    # `_finalize` normally adds; revalidating here against the root-only
    # discovery set would incorrectly reject valid dependent references.
    return PipelineExecutionResult(synthesize_executed_contract(final_contract), already_executed=True)


def _validator_kwargs(ctx: GeneratorContext, matched: list[dict]) -> dict:
    return {
        "known_metrics": ctx.known_prometheus_metrics,
        "labels_by_metric": ctx.labels_by_metric,
        "known_references": set(ctx.reference_texts) | set(ctx.overview_texts),
        "known_datasources": {(item.get("data_source") or "").strip().lower() for item in matched},
    }


def _entries_from_contract(contract: dict) -> list[dict]:
    return list(contract.get("results", [])) if contract.get("mode") == "multi" else [
        {key: value for key, value in contract.items() if key != "mode"}
    ]


def _contract_for_entries(entries: list[dict], unresolved_topics: list[dict] | None = None) -> dict:
    unresolved_topics = unresolved_topics or []
    if len(entries) == 1 and not unresolved_topics:
        return {"mode": "single", **entries[0]}
    return _merge_unresolved_topics({"mode": "multi", "results": entries, "synthesis": None}, unresolved_topics)


def _stage_entries_by_id(contract: dict, expected_ids: set[str]) -> dict[str, dict] | None:
    entries = _entries_from_contract(contract)
    mapped: dict[str, dict] = {}
    for entry in entries:
        resolution_id = entry.get("resolution_id")
        if not isinstance(resolution_id, str) or resolution_id not in expected_ids or resolution_id in mapped:
            return None
        mapped[resolution_id] = entry
    return mapped if set(mapped) == expected_ids else None


def _strip_resolution_id(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key != "resolution_id"}


def _entry_needs_clarification(entry: dict) -> bool:
    return entry.get("status") == "ambiguous_metric" or (
        entry.get("status") == "declined" and entry.get("reason") == "parameter_requires_clarification"
    )


def _has_usable_execution(entry: dict) -> bool:
    execution = entry.get("execution")
    return isinstance(execution, dict) and execution.get("execution_status") == "success" and bool(execution.get("series"))


def _dependency_prompt_data(intent_ids: list[str], entries: list[dict]) -> list[dict]:
    """A bounded, normalized upstream view: labels plus each series' latest value."""
    data = []
    for intent_id, entry in zip(intent_ids, entries, strict=True):
        series_data = []
        for series in entry.get("execution", {}).get("series", []):
            latest = _latest_numeric_value(series)
            series_data.append({"labels": series.get("labels", {}), "latest_value": latest})
        data.append({
            "intent_id": intent_id,
            "measurement": entry.get("measurement_used", {}).get("name"),
            "series": series_data,
        })
    return data


def _query_contains_resolved_scope(entry: dict, parents: list[dict]) -> bool:
    """Fail closed if a dependent PromQL query discarded its resolved scope."""
    if entry.get("status") not in {"ok", "panic_mode_best_effort"}:
        return True
    if (entry.get("data_source") or "").strip().lower() != "prometheus":
        return True
    query = entry.get("query")
    if not isinstance(query, str):
        return False
    values = []
    for parent in parents:
        for series in parent.get("execution", {}).get("series", []):
            for label_key, value in series.get("labels", {}).items():
                if _looks_like_entity_label(label_key) and value:
                    values.append(str(value))
    # A top-N upstream result may contain several entities. All resolved
    # entity values must survive in the dependent selector (typically as a
    # regex alternation), never merely one arbitrarily selected winner.
    return bool(values) and all(value in query for value in set(values))


def _looks_like_entity_label(label_key: str) -> bool:
    normalized = label_key.lower()
    return any(token in normalized for token in ("node", "host", "instance", "device", "gpu", "pod"))


def _dependency_scope_unavailable_entry(intent: _IntentPlan) -> dict:
    return {
        "status": "declined",
        "reason": "parameter_requires_clarification",
        "clarification": "I could not safely retain the entity selected by the earlier result. Please specify the entity directly.",
        "explanation": "The dependent measurement was not executed because its required resolved scope was unavailable or could not be represented safely.",
    }


def _dependency_validation_failure_entry(intent: _IntentPlan) -> dict:
    return {
        "status": "declined",
        "reason": "parameter_requires_clarification",
        "clarification": "Please specify the entity directly for the dependent measurement.",
        "explanation": "The dependent query could not be validated after resolving the earlier result.",
    }


def _dependency_validation_failure(explanation: str) -> dict:
    return {"mode": "single", "status": INTERNAL_VALIDATION_FAILED_STATUS, "explanation": explanation}


def synthesize_executed_contract(contract: dict) -> dict:
    """Best-effort deterministic synthesis over already-normalized results.

    This intentionally has no LLM call and never raises. A failure or an
    unrepresentable result leaves the Section 9-compatible `synthesis: null`
    fallback intact.
    """
    if not isinstance(contract, dict) or contract.get("mode") != "multi":
        return contract
    # Generator-era synthesis is only a pre-execution placeholder. Once this
    # opt-in stage runs, replace it with a deterministic result or null --
    # never expose an LLM-written summary as though it came from live data.
    base_contract = {**contract, "synthesis": None}
    try:
        return _synthesize_executed_contract(base_contract)
    except Exception:  # noqa: BLE001 -- synthesis must never block a response
        logger.exception("Best-effort multi-result synthesis failed")
        return base_contract


def _synthesize_executed_contract(contract: dict) -> dict:
    if contract.get("mode") != "multi" or contract.get("synthesis") is not None:
        return contract
    entries = contract.get("results")
    if not isinstance(entries, list) or len(entries) < 2:
        return contract
    if not all(_has_usable_execution(entry) for entry in entries if entry.get("status") in executor.EXECUTABLE_STATUSES):
        return contract
    executable_entries = [entry for entry in entries if entry.get("status") in executor.EXECUTABLE_STATUSES]
    if len(executable_entries) < 2:
        return contract

    grouped: dict[str, list[str]] = {}
    for entry in executable_entries:
        measurement = entry.get("measurement_used", {}).get("name")
        if not isinstance(measurement, str) or not measurement:
            return contract
        for series in entry.get("execution", {}).get("series", []):
            value = _latest_numeric_value(series)
            if value is None:
                return contract
            entity = _series_entity_label(series.get("labels", {}))
            grouped.setdefault(entity, []).append(f"{measurement} ({_format_synthesis_value(value, entry)})")
    if not grouped or any(len(values) < 2 for values in grouped.values()):
        return contract
    sentences = [f"{entity} has " + " and ".join(values) + "." for entity, values in grouped.items()]
    return {**contract, "synthesis": " ".join(sentences)}


def _latest_numeric_value(series: dict) -> float | None:
    points = series.get("points", []) if isinstance(series, dict) else []
    for point in reversed(points):
        value = point.get("value") if isinstance(point, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _series_entity_label(labels: dict) -> str:
    if isinstance(labels, dict):
        # Prefer stable entity identity labels over incidental transport
        # labels such as `instance`. This lets a root result carrying only
        # `node_id=node-02` join a dependent result that also has
        # `instance=node-02:9200`, `job`, and metric-name labels.
        for preferred_key in ("node_id", "node", "hostname", "host", "instance", "device", "gpu", "pod"):
            value = labels.get(preferred_key)
            if value:
                return f"{preferred_key}={value}"
        for key, value in labels.items():
            if _looks_like_entity_label(str(key)) and value:
                return f"{key}={value}"
        if labels:
            key = sorted(labels)[0]
            return f"{key}={labels[key]}"
    return "the resolved result"


def _format_synthesis_value(value: float, entry: dict) -> str:
    metric = str(entry.get("measurement_used", {}).get("name") or "").lower()
    query = str(entry.get("query") or "")
    if metric.endswith("_bytes"):
        gib = value / (1024 ** 3)
        if abs(gib) >= 1:
            return f"{gib:.2f} GB"
        return f"{value / (1024 ** 2):.2f} MB"
    if re.search(r"\*\s*100\b", query):
        return f"{value:.1f}%"
    return f"{value:.3g}"


def _is_explicit_alert_creation_request(question: str) -> bool:
    return bool(_EXPLICIT_ALERT_CREATION_RE.search(question or ""))


def _disabled_alert_creation_contract() -> dict:
    return {
        "mode": "single",
        "status": "out_of_scope_action",
        "requested_action": "create a new Grafana alert rule",
        "explanation": (
            "Alert-rule creation is currently disabled on this deployment; this skill "
            "only constructs/runs read-only queries."
        ),
    }


def _block_disabled_alert_proposals(contract: dict, settings: Settings) -> dict:
    """Fail closed if an LLM leaks alert_rule_proposed while the flag is off.

    Section 9 must document every possible output status, including the
    feature-gated one. A Generator that sees that general schema can still
    emit the status even without the enabled-only prompt addendum. This
    deterministic output guard is therefore the final proposal-generation
    boundary; confirmation has a separate write-boundary check in
    run_server.py / agent.py.
    """
    if settings.alert_rule_creation_enabled or not isinstance(contract, dict):
        return contract

    if contract.get("mode") == "single" and contract.get("status") == ALERT_PROPOSED_STATUS:
        logger.warning("Blocked leaked alert_rule_proposed output while feature is disabled")
        return _disabled_alert_creation_contract()

    if contract.get("mode") == "multi" and isinstance(contract.get("results"), list):
        replaced = False
        results = []
        for entry in contract["results"]:
            if isinstance(entry, dict) and entry.get("status") == ALERT_PROPOSED_STATUS:
                replaced = True
                results.append({k: v for k, v in _disabled_alert_creation_contract().items() if k != "mode"})
            else:
                results.append(entry)
        if replaced:
            logger.warning("Blocked leaked alert_rule_proposed entry while feature is disabled")
            return {**contract, "results": results, "synthesis": None}
    return contract


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
    """Repairs safe, envelope-only Generator near-misses.

    A model can omit `mode` from an otherwise-complete result, or choose a
    `multi` envelope while producing exactly one result. Both have a single
    deterministic representation in Section 9 and can be repaired without
    changing any result content. Other malformed shapes remain validator
    failures rather than being guessed into validity.
    """
    if not isinstance(contract, dict):
        return contract
    if contract.get("mode") == "multi" and isinstance(contract.get("results"), list) \
            and len(contract["results"]) == 1:
        return {"mode": "single", **contract["results"][0]}
    if "mode" in contract:
        return contract
    if isinstance(contract.get("results"), list) and "synthesis" in contract:
        return {"mode": "multi", **contract}
    if "status" in contract:
        return {"mode": "single", **contract}
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
    defense-in-depth check as everything else, at negligible cost."""
    merged = _merge_unresolved_topics(contract, unresolved_topics)
    result = validator.validate_contract(merged, **validator_kwargs)
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

DEPENDENT COMPOUND-QUERY RESOLUTION IS ENABLED ON THIS DEPLOYMENT.
This is an additive routing annotation for ordinary read questions only.
For every item in `matched_references`, add an `intent_id` and `depends_on`:

{
  "reference_path": "references/...",
  "data_source": "prometheus",
  "intent_id": "short_stable_identifier_for_one_sub_intent",
  "depends_on": []
}

Use the same `intent_id` for every reference needed to resolve one sub-intent.
`depends_on` is a list of earlier `intent_id` values. Leave it empty for an
independent intent. Add a dependency ONLY when the later intent's entity/scope
cannot be known until an earlier query is executed, such as "memory available
on that node" after "which node has highest CPU". In that case, the memory
intent depends on the top-CPU intent. Do not add dependencies merely because
two independent measurements happen to mention the same user-supplied node.
Never create a cycle, self-dependency, or reference to an unknown intent.
"""


def _build_router_instructions(settings: Settings) -> str:
    """Returns the base Router prompt plus only enabled feature addenda.

    With both feature flags false this returns `_ROUTER_INSTRUCTIONS`
    byte-for-byte, preserving the established no-regression property for
    existing read traffic. Each addendum is independently reversible by its
    corresponding deployment flag.
    """
    instructions = _ROUTER_INSTRUCTIONS
    if settings.alert_rule_creation_enabled:
        instructions += _ALERT_ROUTER_ADDENDUM
    if settings.dependent_query_resolution_enabled:
        instructions += _DEPENDENCY_ROUTER_ADDENDUM
    return instructions


async def _run_router(question: str, skill_index: SkillIndex, settings: Settings,
                       context: PipelineContext) -> dict:
    section_headers = ["## 3.", "## 4.", "## 7."]
    if settings.alert_rule_creation_enabled:
        # Only loaded into the prompt when the deployment has opted in --
        # see _build_router_instructions' docstring for why this mirrors
        # the same "flag off => zero prompt change" guarantee.
        section_headers.append("## 12.")
    sections = "\n\n".join(skill_index.section(h) for h in section_headers)
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
    attributes_by_pattern: dict[str, dict] = field(default_factory=dict)
    labels_block: str = ""
    attributes_block: str = ""


def _build_generator_context(matched: list[dict], skill_index: SkillIndex,
                              settings: Settings) -> GeneratorContext:
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
        ctx.labels_by_metric = label_discovery.discover_labels_for_metrics(
            settings.prometheus_url, sorted(ctx.known_prometheus_metrics),
            lookback=timedelta(hours=settings.label_discovery_lookback_hours),
        )
        ctx.labels_block = label_discovery.format_labels_for_prompt(ctx.labels_by_metric)

    if "opensearch" in datasources:
        ctx.attributes_by_pattern = field_discovery.discover_attributes_for_all_known_patterns(settings.opensearch_url)
        ctx.attributes_block = field_discovery.format_attributes_for_prompt(ctx.attributes_by_pattern)

    return ctx


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

Representative live label values, when supplied below, are runtime metadata
that may establish a label key's semantic scope. For example, observed values
such as `node-01` under `node_id` establish that this confirmed key carries
node identifiers, so you may use it to preserve a user-supplied node value
(the sample is not an exhaustive value-existence check). Do not infer scope
from the label key's name; if the runtime values do not establish a mapping,
decline for clarification.

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

DEPENDENCY-RESOLUTION STAGE: this is a deliberately narrow construction
stage, not the normal flat compound-question pass. The prompt identifies the
intent IDs you must resolve and, for a dependent stage, supplies normalized
results already returned by earlier queries. Produce exactly one result object
per requested intent ID and include that ID as a top-level `resolution_id` on
that result object. `resolution_id` is pipeline bookkeeping and is removed
before the user receives the Output Contract.

For a dependent stage, use the concrete labels/values supplied in the resolved
upstream data to scope the new query. Do not rerun, restate, or emit an entry
for an upstream intent. In particular, a phrase such as "that node" must
become an actual selector with the resolved value, never an unfiltered query.
When an upstream intent asks for the current top/bottom N entities, use an
instant query for that ranking. A range `topk` query can return the union of
different winners across the time window, which is not a current top-N set.
If the supplied data cannot be represented safely as a concrete scope using a
runtime-confirmed label key, return the normal `declined` /
`parameter_requires_clarification` result instead of dropping the constraint.
"""


def _build_generator_instructions(settings: Settings, action_intent: str,
                                  dependency_stage: bool = False) -> str:
    """Mirrors _build_router_instructions' gating: the alert-rule-creation
    addendum is appended ONLY when the feature flag is on AND this specific
    request was flagged by the Router as alert-rule creation. An ordinary
    read question -- even on a deployment with the flag enabled -- gets the
    exact same `_GENERATOR_INSTRUCTIONS` string every prior version of this
    pipeline used, unchanged."""
    instructions = _GENERATOR_INSTRUCTIONS
    if settings.alert_rule_creation_enabled and action_intent == _ALERT_ACTION_INTENT:
        instructions += _ALERT_GENERATOR_ADDENDUM
    if dependency_stage:
        instructions += _DEPENDENCY_GENERATOR_ADDENDUM
    return instructions


async def _run_generator(question: str, matched: list[dict], panic_mode: bool,
                          ctx: GeneratorContext, skill_index: SkillIndex, settings: Settings,
                          context: PipelineContext, action_intent: str = "read_query",
                          resolution_ids: list[str] | None = None,
                          resolved_dependencies: list[dict] | None = None) -> dict:
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
    if fundamentals_blocks:
        prompt_parts.append(f"\nDatasource fundamentals:\n\n{fundamentals_blocks}")
    if ctx.labels_block:
        prompt_parts.append(f"\n{ctx.labels_block}")
    if ctx.attributes_block:
        prompt_parts.append(f"\n{ctx.attributes_block}")
    prompt_parts.append(f"\nUser question: {question}")
    prompt_parts.append(f"\npanic_mode is currently: {panic_mode}")
    prompt_parts.append(f"\naction_intent (from the routing phase): {action_intent!r}")
    if resolution_ids:
        prompt_parts.append(
            f"\nDependency-resolution stage: produce results ONLY for these intent IDs: "
            f"{json.dumps(resolution_ids)}. Include the matching `resolution_id` on every result."
        )
    if resolved_dependencies:
        prompt_parts.append(
            "\nAlready-executed dependency results (authoritative runtime data; use their concrete "
            "labels/values to scope this stage's query):\n"
            + json.dumps(resolved_dependencies, ensure_ascii=False)
        )
    if context.previous_question:
        prompt_parts.append(
            f"\nThis is a follow-up clarification. Original question: "
            f"{context.previous_question}\nUser's clarifying answer: "
            f"{context.clarification_answer}\nResolve using the FULL combined intent."
        )

    prompt = "\n\n".join(prompt_parts)
    response = llm_client.call_llm_json(
        prompt=prompt,
        system_instruction=_build_generator_instructions(
            settings, action_intent, dependency_stage=bool(resolution_ids)
        ),
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    return response.parsed


# ---- helpers --------------------------------------------------------------------


def _wrap_gate_stop(gate_stop: dict) -> dict:
    result = {"mode": "single", "status": gate_stop["status"]}
    for key in ("reason", "requested_action", "clarification", "explanation", "candidates"):
        if gate_stop.get(key) is not None:
            result[key] = gate_stop[key]
    return result
