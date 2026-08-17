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
                assemble the mode/results shape). Given the full content of
                every matched reference, its sibling overview.md (for the
                Metric Directory), the relevant *-fundamentals.md file(s),
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
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from app import field_discovery, label_discovery, llm_client, validator
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


@dataclass
class PipelineContext:
    """Optional prior-turn context for the clarification/follow-up flow
    (session_store.py populates this from a stored session)."""
    previous_question: str | None = None
    previous_result: dict | None = None
    clarification_answer: str | None = None


async def run_pipeline(question: str, skill_index: SkillIndex, settings: Settings,
                        context: PipelineContext | None = None) -> dict:
    context = context or PipelineContext()

    router_output = await _run_router(question, skill_index, settings, context)
    unresolved_topics = _clean_unresolved_topics(router_output.get("unresolved_topics"))

    if router_output.get("gate_stop"):
        contract = _wrap_gate_stop(router_output["gate_stop"])
        return _finalize(contract, unresolved_topics)

    matched = router_output.get("matched_references", [])
    panic_mode = bool(router_output.get("panic_mode", False))

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
    contract = await _run_generator(question, matched, panic_mode, generator_context, skill_index, settings, context)
    contract = _normalize_contract_shape(contract)

    known_references = set(generator_context.reference_texts.keys()) | set(generator_context.overview_texts.keys())
    known_datasources = {(m.get("data_source") or "").strip().lower() for m in matched}

    return _finalize(
        contract, unresolved_topics,
        known_metrics=generator_context.known_prometheus_metrics,
        labels_by_metric=generator_context.labels_by_metric,
        known_references=known_references,
        known_datasources=known_datasources,
    )


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


async def _run_router(question: str, skill_index: SkillIndex, settings: Settings,
                       context: PipelineContext) -> dict:
    sections = "\n\n".join([
        skill_index.section("## 3."),
        skill_index.section("## 4."),
        skill_index.section("## 7."),
    ])
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
        system_instruction=_ROUTER_INSTRUCTIONS,
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

Respond with ONLY the JSON object matching Section 9's contract -- no other
text, no markdown fences.
"""


async def _run_generator(question: str, matched: list[dict], panic_mode: bool,
                          ctx: GeneratorContext, skill_index: SkillIndex, settings: Settings,
                          context: PipelineContext) -> dict:
    sections = "\n\n".join([
        skill_index.section("## 5."),
        skill_index.section("## 6."),
        skill_index.section("## 7."),
        skill_index.section("## 8."),
        skill_index.section("## 9."),
    ])

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
    if context.previous_question:
        prompt_parts.append(
            f"\nThis is a follow-up clarification. Original question: "
            f"{context.previous_question}\nUser's clarifying answer: "
            f"{context.clarification_answer}\nResolve using the FULL combined intent."
        )

    prompt = "\n\n".join(prompt_parts)
    response = llm_client.call_llm_json(
        prompt=prompt,
        system_instruction=_GENERATOR_INSTRUCTIONS,
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
