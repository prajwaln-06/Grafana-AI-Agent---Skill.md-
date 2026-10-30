"""
benchmarks/benchmark_pipeline.py
=================================

Performance baseline benchmark for the 4-stage AI agent pipeline.

PURPOSE
-------
This script establishes a wall-clock performance baseline for the production
pipeline BEFORE any optimizations are applied.  Run it after every significant
change to compare against this baseline and quantify regressions or
improvements.

THE 4 PIPELINE STAGES (as implemented in app/pipeline.py)
----------------------------------------------------------
Stage 1 – ROUTER     (_run_router):
    One LLM call (Gemini) that gates and routes the user question to the
    relevant reference file(s) in the skill package.  Includes SKILL.md
    section reading and prompt assembly.

Stage 2 – GENERATOR CONTEXT (_build_generator_context):
    Deterministic Python: reads matched reference files from disk, runs
    Prometheus label discovery (live network call to /api/v1/series), and
    assembles the full context for the Generator prompt.

Stage 3 – GENERATOR  (_run_generator):
    One LLM call (Gemini) that selects the metric, builds the PromQL/DSL
    query, and emits the Output Contract (SKILL.md §9).

Stage 4 – VALIDATOR + EXECUTOR (validate_contract + execute_contract):
    Deterministic Python: validator.validate_contract checks the contract
    structurally, then executor.execute_contract runs the query against
    Prometheus (or OpenSearch) and attaches the execution block.

End-to-end latency is the sum of all four stages.

TOKEN COUNTS
------------
input_tokens and output_tokens are the SUM of both LLM calls (Router +
Generator) for each run, read from response.usage_metadata.prompt_token_count
and response.usage_metadata.candidates_token_count respectively.  If the SDK
does not expose usage_metadata (older versions), the columns will be "N/A".

HOW TO RUN
----------
Prerequisites:
    • A .env file (or environment variables) with GEMINI_API_KEY set.
    • Prometheus reachable at PROMETHEUS_URL (default http://localhost:9090).
    • The skills/ directory present (same as the production server needs).

Run from the repository root:

    python benchmarks/benchmark_pipeline.py

Optional arguments:
    --runs N          Number of times to run each prompt  (default: 3)
    --output FILE     Path for the CSV output              (default: benchmarks/results/benchmark_YYYYMMDD_HHMMSS.csv)
    --prompts FILE    JSON file with a list of prompt strings to override defaults
    --no-alert        Disable alert_rule_creation_enabled (enabled by default,
                      matching the .env setting)

CSV OUTPUT
----------
The CSV is written to benchmarks/results/ by default and can be opened
directly in Excel.  Columns:

    timestamp           ISO-8601 wall-clock time at the start of the run
    prompt_name         Short label derived from the first 60 chars of the prompt
    run_number          1-based index within the repetitions for this prompt
    stage1_router_ms    Wall-clock ms spent in _run_router (1 LLM call + prompt assembly)
    stage2_context_ms   Wall-clock ms spent in _build_generator_context (disk reads + label discovery)
    stage3_generator_ms Wall-clock ms spent in _run_generator (1 LLM call + prompt assembly)
    stage4_val_exec_ms  Wall-clock ms spent in validate_contract + execute_contract combined
    total_ms            End-to-end wall-clock ms (≈ sum of all 4 stages + any overhead)
    pipeline_status     The final "status" field from the Output Contract (e.g. "ok", "declined")
    pipeline_mode       The final "mode" field ("single" / "multi")
    execution_status    execution.execution_status from the contract, or "N/A"
    input_tokens        Sum of prompt_token_count across Router + Generator LLM calls
    output_tokens       Sum of candidates_token_count across Router + Generator LLM calls
    success             TRUE if the pipeline completed without an exception; FALSE otherwise
    error               Empty on success; exception class + message on failure

IMPORTANT: this script makes LIVE network calls to your configured LLM and
Prometheus endpoints.  It is read-only with respect to Grafana and produces
no side effects beyond the CSV file.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Make sure the repo root is on sys.path so `app.*` imports work when the
# script is executed directly (not via pytest).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Production pipeline imports — exactly the same modules the server uses.
# Nothing is mocked; everything runs against the live LLM and backends.
# ---------------------------------------------------------------------------
from app import executor, llm_client  # noqa: E402
from app.config import Settings  # noqa: E402
from app.pipeline import (  # noqa: E402
    PipelineContext,
    _build_generator_context,
    _run_generator,
    _run_router,
    _normalize_contract_shape,
    _block_disabled_alert_proposals,
    _apply_alert_rule_defaults,
    _finalize,
    _clean_unresolved_topics,
    _wrap_gate_stop,
    _resolve_catalog,
    _catalog_status_lookup,
    INTERNAL_VALIDATION_FAILED_STATUS,
)
from app.skill_index import SkillIndex  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.WARNING,  # keep benchmark output clean; errors still visible
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("benchmark")

# ---------------------------------------------------------------------------
# Default benchmark prompts
# ---------------------------------------------------------------------------
DEFAULT_PROMPTS: list[str] = [
    "Plot CPU utilization for node-00 and node-01 over the last hour.",
    "Which node has the highest CPU utilization right now, and how much memory is available on that node?",
    "Show GPU temperature across all GPUs.",
    "How much disk space is available across all cluster nodes?",
    "Alert me if CPU utilization exceeds 90% on node-00 for 5 minutes.",
]

# ---------------------------------------------------------------------------
# CSV column order
# ---------------------------------------------------------------------------
CSV_FIELDNAMES = [
    "timestamp",
    "prompt_name",
    "run_number",
    "stage1_router_s",
    "stage2_context_s",
    "stage3_generator_s",
    "stage4_val_exec_s",
    "total_s",
    "pipeline_status",
    "pipeline_mode",
    "execution_status",
    "input_tokens",
    "output_tokens",
    "success",
    "error",
]


def _prompt_label(prompt: str) -> str:
    """Short label for the prompt — first 60 chars, whitespace collapsed."""
    collapsed = " ".join(prompt.split())
    return collapsed[:60] + ("…" if len(collapsed) > 60 else "")


def _extract_execution_status(contract: dict) -> str:
    """Best-effort extraction of execution_status from the contract."""
    if not isinstance(contract, dict):
        return "N/A"
    execution = contract.get("execution")
    if isinstance(execution, dict):
        return execution.get("execution_status", "N/A")
    for entry in contract.get("results", []):
        if isinstance(entry, dict):
            ex = entry.get("execution")
            if isinstance(ex, dict):
                return ex.get("execution_status", "N/A")
    return "N/A"


def _extract_status(contract: dict) -> str:
    """Top-level status for single; comma-joined statuses for multi."""
    if not isinstance(contract, dict):
        return "N/A"
    if contract.get("mode") == "multi":
        statuses = [
            e.get("status", "?")
            for e in contract.get("results", [])
            if isinstance(e, dict)
        ]
        return ", ".join(statuses) if statuses else "N/A"
    return contract.get("status", "N/A")


# ---------------------------------------------------------------------------
# Per-run token interceptor
# ---------------------------------------------------------------------------

class _TokenAccumulator:
    """Wraps llm_client.call_llm_json for the duration of one benchmark run,
    accumulating input/output token counts from every LLM call made
    (Router + Generator = 2 calls for a normal request).

    Usage:
        with _TokenAccumulator() as acc:
            ... # run the pipeline
        print(acc.input_tokens, acc.output_tokens)
    """

    def __init__(self) -> None:
        self._orig = llm_client.call_llm_json
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self._any_token_data = False

    def __enter__(self) -> "_TokenAccumulator":
        acc = self

        def _intercepted(prompt, system_instruction, api_key, model,
                         max_retries=llm_client.MAX_RETRIES):
            response = acc._orig(prompt, system_instruction, api_key, model, max_retries)
            if response.input_tokens is not None:
                acc.input_tokens += response.input_tokens
                acc._any_token_data = True
            if response.output_tokens is not None:
                acc.output_tokens += response.output_tokens
                acc._any_token_data = True
            return response

        llm_client.call_llm_json = _intercepted
        return self

    def __exit__(self, *_) -> None:
        llm_client.call_llm_json = self._orig

    def csv_input(self) -> str:
        return str(self.input_tokens) if self._any_token_data else "N/A"

    def csv_output(self) -> str:
        return str(self.output_tokens) if self._any_token_data else "N/A"


# ---------------------------------------------------------------------------
# Core instrumented run
# ---------------------------------------------------------------------------

async def _run_instrumented(
    prompt: str,
    skill_index: SkillIndex,
    settings: Settings,
) -> dict:
    """
    Runs the production pipeline with per-stage timing instrumentation
    at the exact stage boundaries, and accumulates token counts from
    both LLM calls (Router + Generator).

    Returns a dict suitable for writing directly as a CSV row.
    """
    context = PipelineContext()
    catalog = _resolve_catalog(settings, None)
    t_total_start = time.perf_counter()

    # ------------------------------------------------------------------
    # STAGE 1: ROUTER  (1 LLM call)
    # ------------------------------------------------------------------
    t1_start = time.perf_counter()
    router_output = await _run_router(prompt, skill_index, settings, context, catalog=catalog)
    t1_end = time.perf_counter()
    stage1_ms = (t1_end - t1_start) * 1000.0

    # Gate-stop / nothing matched — stages 2-4 never run
    if router_output.get("gate_stop") or not router_output.get("matched_references"):
        t_total_end = time.perf_counter()
        unresolved = _clean_unresolved_topics(router_output.get("unresolved_topics"))
        gate = router_output.get("gate_stop")
        if gate:
            from app.pipeline import _finalize as _pipe_finalize
            contract = _pipe_finalize(_wrap_gate_stop(gate), unresolved)
        else:
            contract = {"mode": "single", "status": "unmapped",
                        "explanation": "No reference matched and no gate_stop."}
        return {
            "stage1_router_s": round(stage1_ms / 1000.0, 3),
            "stage2_context_s": 0.0,
            "stage3_generator_s": 0.0,
            "stage4_val_exec_s": 0.0,
            "total_s": round((t_total_end - t_total_start), 3),
            "pipeline_status": _extract_status(contract),
            "pipeline_mode": contract.get("mode", "N/A"),
            "execution_status": "N/A",
        }

    matched = router_output.get("matched_references", [])
    panic_mode = bool(router_output.get("panic_mode", False))
    action_intent = router_output.get("action_intent") or "read_query"

    # ------------------------------------------------------------------
    # STAGE 2: GENERATOR CONTEXT  (deterministic — disk + Prometheus)
    # ------------------------------------------------------------------
    t2_start = time.perf_counter()
    generator_context = await asyncio.to_thread(
        _build_generator_context, matched, skill_index, settings, prompt, catalog
    )
    t2_end = time.perf_counter()
    stage2_s = t2_end - t2_start

    # ------------------------------------------------------------------
    # STAGE 3: GENERATOR  (1 LLM call)
    # ------------------------------------------------------------------
    t3_start = time.perf_counter()
    contract = await _run_generator(
        prompt, matched, panic_mode, generator_context,
        skill_index, settings, context,
        action_intent=action_intent,
    )
    t3_end = time.perf_counter()
    stage3_s = t3_end - t3_start

    # ------------------------------------------------------------------
    # STAGE 4: VALIDATOR + EXECUTOR  (deterministic — Python + Prometheus)
    # ------------------------------------------------------------------
    t4_start = time.perf_counter()

    contract = _normalize_contract_shape(contract)
    contract = _block_disabled_alert_proposals(contract, settings)
    contract = _apply_alert_rule_defaults(contract, settings)

    known_references = (
        set(generator_context.reference_texts.keys())
        | set(generator_context.overview_texts.keys())
    )
    known_datasources = {
        (m.get("data_source") or "").strip().lower() for m in matched
    }
    catalog_status = _catalog_status_lookup(settings, catalog)
    candidate_metric_scope = (
        set(m["name"] for m in generator_context.metric_metadata)
        if generator_context.metric_metadata
        else None
    )

    final_contract = _finalize(
        contract, [],
        known_metrics=generator_context.known_prometheus_metrics,
        labels_by_metric=generator_context.labels_by_metric,
        known_references=known_references,
        known_datasources=known_datasources,
        catalog_status_by_metric=catalog_status,
        candidate_metric_scope=candidate_metric_scope,
    )
    if final_contract.get("status") == INTERNAL_VALIDATION_FAILED_STATUS and candidate_metric_scope:
        final_contract = _finalize(
            contract, [],
            known_metrics=generator_context.known_prometheus_metrics,
            labels_by_metric=generator_context.labels_by_metric,
            known_references=known_references,
            known_datasources=known_datasources,
            catalog_status_by_metric=catalog_status,
        )
    executed_contract = executor.execute_contract(final_contract, settings)

    t4_end = time.perf_counter()
    t_total_end = time.perf_counter()

    return {
        "stage1_router_s": round(stage1_ms / 1000.0, 3),
        "stage2_context_s": round(stage2_s, 3),
        "stage3_generator_s": round(stage3_s, 3),
        "stage4_val_exec_s": round(t4_end - t4_start, 3),
        "total_s": round(t_total_end - t_total_start, 3),
        "pipeline_status": _extract_status(executed_contract),
        "pipeline_mode": executed_contract.get("mode", "N/A"),
        "execution_status": _extract_execution_status(executed_contract),
    }


# ---------------------------------------------------------------------------
# Benchmark loop
# ---------------------------------------------------------------------------

async def run_benchmarks(
    prompts: list[str],
    skill_index: SkillIndex,
    settings: Settings,
    num_runs: int,
    csv_path: Path,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    successful_runs = 0
    failed_runs = 0

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()

        for prompt in prompts:
            label = _prompt_label(prompt)
            print(f"\nPrompt: {label!r}")

            for run_num in range(1, num_runs + 1):
                ts = datetime.now(timezone.utc).isoformat()
                print(f"  Run {run_num}/{num_runs} ... ", end="", flush=True)

                row: dict = {
                    "timestamp": ts,
                    "prompt_name": label,
                    "run_number": run_num,
                    **{k: "N/A" for k in CSV_FIELDNAMES
                       if k not in ("timestamp", "prompt_name", "run_number")},
                    "success": "FALSE",
                    "error": "",
                }

                try:
                    with _TokenAccumulator() as acc:
                        timing = await _run_instrumented(prompt, skill_index, settings)

                    row.update(timing)
                    row["input_tokens"] = acc.csv_input()
                    row["output_tokens"] = acc.csv_output()
                    row["success"] = "TRUE"
                    successful_runs += 1

                    print(
                        f"OK  total={timing['total_s']:.2f}s  "
                        f"[router={timing['stage1_router_s']:.2f}s "
                        f"ctx={timing['stage2_context_s']:.2f}s "
                        f"gen={timing['stage3_generator_s']:.2f}s "
                        f"val+exec={timing['stage4_val_exec_s']:.3f}s]  "
                        f"status={timing['pipeline_status']}  "
                        f"tokens={acc.csv_input()}in/{acc.csv_output()}out"
                    )
                except Exception as exc:  # noqa: BLE001
                    failed_runs += 1
                    error_msg = f"{type(exc).__name__}: {exc}"
                    row["error"] = error_msg
                    print(f"FAILED  {error_msg}")
                    logger.debug("Full traceback:", exc_info=True)

                writer.writerow(row)
                f.flush()

    print(f"\n{'='*70}")
    print(f"Benchmark complete.")
    print(f"  Successful runs : {successful_runs}")
    print(f"  Failed runs     : {failed_runs}")
    print(f"  CSV output      : {csv_path}")
    print(f"{'='*70}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_settings(alert_enabled: bool) -> Settings:
    return Settings(alert_rule_creation_enabled=alert_enabled)


def _default_output_path() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(__file__).parent / "results" / f"benchmark_{ts}.csv"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark the 4-stage AI agent pipeline (baseline measurement only).",
    )
    parser.add_argument(
        "--runs", type=int, default=3,
        help="Number of times to run each prompt (default: 3).",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Path for the CSV output file.",
    )
    parser.add_argument(
        "--prompts", type=Path, default=None,
        help="JSON file containing a list of prompt strings.",
    )
    parser.add_argument(
        "--no-alert", action="store_true", default=False,
        help="Disable alert_rule_creation_enabled (default: read from .env, typically True).",
    )
    args = parser.parse_args()

    if args.prompts:
        with args.prompts.open(encoding="utf-8") as pf:
            prompts = json.load(pf)
        if not isinstance(prompts, list) or not all(isinstance(p, str) for p in prompts):
            print("ERROR: --prompts file must contain a JSON array of strings.", file=sys.stderr)
            sys.exit(1)
    else:
        prompts = DEFAULT_PROMPTS

    csv_path = args.output or _default_output_path()

    try:
        # Read alert_rule_creation_enabled from .env first, then allow --no-alert to override
        base = Settings()
        alert_enabled = base.alert_rule_creation_enabled and not args.no_alert
        settings = Settings(alert_rule_creation_enabled=alert_enabled)
    except Exception as exc:
        print(f"ERROR: Could not load Settings — {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        skill_index = SkillIndex.load(settings.skills_root)
    except Exception as exc:
        print(f"ERROR: Could not load SkillIndex — {exc}", file=sys.stderr)
        sys.exit(1)

    print("=" * 70)
    print("Grafana AI Agent Pipeline — Performance Baseline Benchmark")
    print("=" * 70)
    print(f"  Prompts       : {len(prompts)}")
    print(f"  Runs each     : {args.runs}")
    print(f"  LLM model     : {settings.gemini_model}")
    print(f"  Vertex project: {settings.vertex_project}")
    print(f"  Prometheus    : {settings.prometheus_url}")
    print(f"  Skills root   : {settings.skills_root}")
    print(f"  Alert flag    : {settings.alert_rule_creation_enabled}")
    print(f"  Output CSV    : {csv_path}")
    print("=" * 70)
    print()
    print("  Timing : time.perf_counter() (monotonic, high-resolution)")
    print("  Tokens : Router + Generator combined (from usage_metadata)")
    print()

    asyncio.run(
        run_benchmarks(
            prompts=prompts,
            skill_index=skill_index,
            settings=settings,
            num_runs=args.runs,
            csv_path=csv_path,
        )
    )


if __name__ == "__main__":
    main()
