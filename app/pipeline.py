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

Catalog status validation (Phase 12, Batch 4 -- feature-flagged off by
default, see app/config.py's `catalog_metric_status_validation_enabled`):
when enabled, `_catalog_status_by_metric` hands validator.py a small
{metric_name: catalog_status} lookup alongside `known_metrics`, so a
`discovered_pending_review`/`rejected` catalog metric can never become a
valid query or alert-rule metric even if the same name also happens to
appear in a Metric Directory this request opened. This is purely additive
to `known_metrics` -- see validator.py's module docstring for the combined
policy, and app/pipeline.py's `_catalog_status_by_metric` for how the
mapping is built. Phase 13 (same batch) closes a related gap: the
alert-rule-proposal validation path (`validator._validate_alert_rule_proposed`)
now enforces the SAME `known_metrics`/catalog-status policy the ordinary
query path always has, rather than skipping metric-name validation for
alert proposals specifically.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from app import field_discovery, label_discovery, llm_client, validator
from app.catalog import rules as catalog_rules
from app.catalog import search as catalog_search
from app.catalog.loader import CatalogLoadError, load_catalog
from app.catalog.schema import Catalog, CatalogSchemaError
from app.config import Settings
from app.skill_index import ROUTING_ROW_RE, SkillIndex

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

    # `catalog` is an optional, explicit override (mainly for tests); ordinary
    # callers (app/agent.py) never pass it. If neither Batch 3 catalog
    # feature is enabled, _get_catalog() is never called at all -- zero
    # catalog load, zero extra I/O, for any deployment that hasn't opted in.
    if catalog is None and (settings.catalog_shadow_mode_enabled or settings.catalog_assisted_routing_enabled
                             or settings.catalog_metric_status_validation_enabled):
        catalog = _get_catalog(settings)

    router_output = await _run_router(question, skill_index, settings, context, catalog=catalog)
    unresolved_topics = _clean_unresolved_topics(router_output.get("unresolved_topics"))

    if router_output.get("gate_stop"):
        contract = _wrap_gate_stop(router_output["gate_stop"])
        return _finalize(contract, unresolved_topics)

    matched = router_output.get("matched_references", [])
    panic_mode = bool(router_output.get("panic_mode", False))
    action_intent = router_output.get("action_intent") or "read_query"

    if settings.catalog_shadow_mode_enabled and catalog is not None:
        # Phase 8: purely observational. See _log_catalog_shadow_comparison's
        # own docstring for why this can never affect `matched`, `contract`,
        # or the response -- it is called for its logging side effect only
        # and its return value is discarded.
        _log_catalog_shadow_comparison(question, matched, catalog)

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
        return _finalize(contract, unresolved_topics)

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
        return _finalize(contract, unresolved_topics)

    generator_context = _build_generator_context(matched, skill_index, settings)
    contract = await _run_generator(question, matched, panic_mode, generator_context, skill_index, settings,
                                     context, action_intent=action_intent)
    contract = _normalize_contract_shape(contract)
    contract = _apply_alert_rule_defaults(contract, settings)

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

    return _finalize(
        contract, unresolved_topics,
        known_metrics=generator_context.known_prometheus_metrics,
        labels_by_metric=generator_context.labels_by_metric,
        known_references=known_references,
        known_datasources=known_datasources,
        catalog_status_by_metric=catalog_status_by_metric,
    )


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
    """Repairs one specific, observed class of Generator near-miss: the
    model produces a structurally-correct single result object (a valid
    `status` plus that status's own required fields, e.g. a well-formed
    `unsupported_metric` entry) but omits the top-level `"mode"` envelope
    key SKILL.md Section 9 requires around it. This is purely a wrapper
    inference -- it never invents, guesses, or alters any field's VALUE, so
    it cannot introduce a fabricated metric name, label key, or query the
    way a content-level "fix" would. If `contract` already has a `mode`
    key, or doesn't look like a recognizable single/multi shape at all,
    it's returned untouched and the deterministic validator will report
    whatever's actually wrong with it -- this function only ever adds the
    one specific key it's confident about, never papers over anything else.
    """
    if not isinstance(contract, dict) or "mode" in contract:
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


def _build_router_instructions(settings: Settings) -> str:
    """Returns the Router's system instructions, appending the alert-rule-
    creation addendum ONLY when the feature flag is on. This is the
    highest-risk prompt-engineering surface in the alert-rule-creation
    feature (SKILL.md Section 12) -- keeping the base `_ROUTER_INSTRUCTIONS`
    string byte-for-byte unchanged, and gating the addendum behind a config
    flag that defaults to False, means existing read-query routing gets
    ZERO prompt changes unless a deployment has explicitly opted in. A
    regression in alert-rule-creation classification can also never leak
    into a deployment that hasn't turned the flag on, and a bad rollout is
    fully reversible by flipping the flag back off -- no prompt rollback
    required."""
    if not settings.alert_rule_creation_enabled:
        return _ROUTER_INSTRUCTIONS
    return _ROUTER_INSTRUCTIONS + _ALERT_ROUTER_ADDENDUM


async def _run_router(question: str, skill_index: SkillIndex, settings: Settings,
                       context: PipelineContext, catalog: Catalog | None = None) -> dict:
    section_headers = ["## 3.", "## 4.", "## 7."]
    if settings.alert_rule_creation_enabled:
        # Only loaded into the prompt when the deployment has opted in --
        # see _build_router_instructions' docstring for why this mirrors
        # the same "flag off => zero prompt change" guarantee.
        section_headers.append("## 12.")

    section_texts = [skill_index.section(h) for h in section_headers]
    if settings.catalog_assisted_routing_enabled and catalog is not None:
        # Phase 9: replace ONLY the "## 4." entry, in place, with a
        # per-question-narrowed version. section_headers/section_texts stay
        # index-aligned, so this is a targeted substitution, not a
        # rebuild -- if catalog_assisted_routing_enabled is False (the
        # default), this branch never runs and `sections` below is
        # byte-for-byte what it always was.
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


def _build_generator_instructions(settings: Settings, action_intent: str) -> str:
    """Mirrors _build_router_instructions' gating: the alert-rule-creation
    addendum is appended ONLY when the feature flag is on AND this specific
    request was flagged by the Router as alert-rule creation. An ordinary
    read question -- even on a deployment with the flag enabled -- gets the
    exact same `_GENERATOR_INSTRUCTIONS` string every prior version of this
    pipeline used, unchanged."""
    if settings.alert_rule_creation_enabled and action_intent == _ALERT_ACTION_INTENT:
        return _GENERATOR_INSTRUCTIONS + _ALERT_GENERATOR_ADDENDUM
    return _GENERATOR_INSTRUCTIONS


async def _run_generator(question: str, matched: list[dict], panic_mode: bool,
                          ctx: GeneratorContext, skill_index: SkillIndex, settings: Settings,
                          context: PipelineContext, action_intent: str = "read_query") -> dict:
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
    broken or absent catalog.json must degrade both Batch 3 features to a
    no-op, never crash a request or block startup, matching the
    "catalog integration is additive" principle. Logs the failure once per
    distinct path, not on every request, to avoid log-flooding a
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
            "Catalog integration (shadow mode / assisted routing) disabled: "
            "failed to load catalog at %s: %s", settings.catalog_path, e,
        )
        _catalog_load_failed_path = path_str
        return None
    _catalog_cache = catalog_rules.apply_rules(loaded)
    return _catalog_cache


def reset_catalog_cache() -> None:
    """Clears the module-level catalog cache. Exists for tests (and for a
    future admin/reload endpoint mirroring SkillIndex's own reload_skill())
    that need a fresh load after catalog.json changes on disk -- ordinary
    request handling never needs to call this."""
    global _catalog_cache, _catalog_load_failed_path
    _catalog_cache = None
    _catalog_load_failed_path = None


def _log_catalog_shadow_comparison(question: str, matched: list[dict], catalog: Catalog) -> None:
    """Phase 8. Runs catalog search independently of, and never influencing,
    the real Router decision, purely to log whether the two agree. Wrapped
    in its own try/except -- a bug in catalog search or in this comparison
    itself must never surface as a pipeline failure for an ordinary
    request; shadow mode failing loudly enough to break production traffic
    would defeat the entire point of it being a shadow.
    """
    try:
        results = catalog_search.search(catalog, question)
        catalog_paths = {r.entry.reference_path for r in results if r.entry.reference_path}
        router_paths = {m.get("reference_path") for m in matched if m.get("reference_path")}
        agreement = bool(router_paths & catalog_paths) if (router_paths or catalog_paths) else True
        logger.info(
            "catalog_shadow_mode: question=%r router_matched=%s catalog_suggested=%s "
            "agreement=%s", question, sorted(router_paths), sorted(catalog_paths), agreement,
        )
    except Exception:
        logger.exception(
            "catalog_shadow_mode: comparison failed for question %r; this never "
            "affects the actual response.", question,
        )


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