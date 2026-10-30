"""
benchmarks/run_catalog_improvements.py
======================================
Runs 3 benchmark runs for each of the 5 default questions on the 4-stage pipeline,
records raw per-run metrics in catalog_improvements_raw.csv, and computes the
per-question averages in catalog_improvements.csv.
"""
from __future__ import annotations

import asyncio
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.config import Settings
from app.skill_index import SkillIndex
from benchmarks.benchmark_pipeline import (
    DEFAULT_PROMPTS,
    CSV_FIELDNAMES,
    _TokenAccumulator,
    _prompt_label,
    _run_instrumented,
)

RAW_CSV_PATH = _REPO_ROOT / "catalog_improvements_raw.csv"
AVG_CSV_PATH = _REPO_ROOT / "catalog_improvements.csv"
PERF_CSV_PATH = _REPO_ROOT / "catalog performance improvements.csv"
CLEAN_CSV_PATH = _REPO_ROOT / "catalog_improvements_clean_runs.csv"

# Schema matching catalog_baseline.csv
AVG_FIELDNAMES = [
    "prompt",
    "status",
    "stage1_router_s",
    "stage2_context_s",
    "stage3_generator_s",
    "stage4_val_exec_s",
    "total_s",
    "input_tokens",
    "output_tokens",
]

CLEAN_FIELDNAMES = [
    "prompt",
    "run_number",
    "status",
    "stage1_router_s",
    "stage2_context_s",
    "stage3_generator_s",
    "stage4_val_exec_s",
    "total_s",
    "input_tokens",
    "output_tokens",
]


async def run_improvements_benchmark(num_runs: int = 3) -> None:
    settings = Settings()
    skill_index = SkillIndex.load(settings.skills_root)

    print("=" * 80)
    print("Starting Catalog Improvements Benchmark")
    print(f"  Prompts       : {len(DEFAULT_PROMPTS)}")
    print(f"  Runs each     : {num_runs}")
    print(f"  Model         : {settings.gemini_model}")
    print(f"  Raw CSV       : {RAW_CSV_PATH}")
    print(f"  Average CSV   : {AVG_CSV_PATH}")
    print("=" * 80)

    raw_rows: list[dict] = []
    results_by_prompt: dict[str, list[dict]] = {p: [] for p in DEFAULT_PROMPTS}

    # Open raw CSV for live appending
    with RAW_CSV_PATH.open("w", newline="", encoding="utf-8") as f_raw:
        raw_writer = csv.DictWriter(f_raw, fieldnames=CSV_FIELDNAMES)
        raw_writer.writeheader()
        f_raw.flush()

        for p_idx, prompt in enumerate(DEFAULT_PROMPTS, 1):
            label = _prompt_label(prompt)
            print(f"\n[{p_idx}/{len(DEFAULT_PROMPTS)}] Prompt: {prompt}")

            completed_runs = 0
            attempt = 0
            while completed_runs < num_runs and attempt < num_runs * 2:
                attempt += 1
                run_num = completed_runs + 1
                ts = datetime.now(timezone.utc).isoformat()
                print(f"  Run {run_num}/{num_runs} ... ", end="", flush=True)

                row: dict = {
                    "timestamp": ts,
                    "prompt_name": label,
                    "run_number": run_num,
                    **{k: "N/A" for k in CSV_FIELDNAMES if k not in ("timestamp", "prompt_name", "run_number")},
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

                    raw_rows.append(row)
                    raw_writer.writerow(row)
                    f_raw.flush()

                    results_by_prompt[prompt].append({
                        "prompt": prompt,
                        "status": timing["pipeline_status"],
                        "stage1_router_s": float(timing["stage1_router_s"]),
                        "stage2_context_s": float(timing["stage2_context_s"]),
                        "stage3_generator_s": float(timing["stage3_generator_s"]),
                        "stage4_val_exec_s": float(timing["stage4_val_exec_s"]),
                        "total_s": float(timing["total_s"]),
                        "input_tokens": int(acc.input_tokens),
                        "output_tokens": int(acc.output_tokens),
                    })
                    completed_runs += 1

                    print(
                        f"OK total={timing['total_s']:.2f}s "
                        f"[router={timing['stage1_router_s']:.2f}s "
                        f"ctx={timing['stage2_context_s']:.2f}s "
                        f"gen={timing['stage3_generator_s']:.2f}s "
                        f"val+exec={timing['stage4_val_exec_s']:.3f}s] "
                        f"status={timing['pipeline_status']} "
                        f"tokens={acc.csv_input()}in/{acc.csv_output()}out"
                    )
                except Exception as exc:
                    error_msg = f"{type(exc).__name__}: {exc}"
                    row["error"] = error_msg
                    raw_rows.append(row)
                    raw_writer.writerow(row)
                    f_raw.flush()
                    print(f"FAILED ({error_msg}) - retrying after delay...")
                    await asyncio.sleep(5)
                    continue

                if completed_runs < num_runs:
                    await asyncio.sleep(2)

    # Compute averages per question
    avg_rows: list[dict] = []
    print("\n" + "=" * 80)
    print("Averages per question:")
    print("=" * 80)

    for prompt, runs in results_by_prompt.items():
        if not runs:
            print(f"Warning: No successful runs for {prompt!r}")
            continue

        n = len(runs)
        avg_stage1 = round(sum(r["stage1_router_s"] for r in runs) / n, 2)
        avg_stage2 = round(sum(r["stage2_context_s"] for r in runs) / n, 3)
        avg_stage3 = round(sum(r["stage3_generator_s"] for r in runs) / n, 2)
        avg_stage4 = round(sum(r["stage4_val_exec_s"] for r in runs) / n, 3)
        avg_total = round(sum(r["total_s"] for r in runs) / n, 2)
        avg_in_tok = int(round(sum(r["input_tokens"] for r in runs) / n))
        avg_out_tok = int(round(sum(r["output_tokens"] for r in runs) / n))
        # Status from the most common status or the latest run
        status = runs[0]["status"]

        avg_row = {
            "prompt": prompt,
            "status": status,
            "stage1_router_s": avg_stage1,
            "stage2_context_s": avg_stage2,
            "stage3_generator_s": avg_stage3,
            "stage4_val_exec_s": avg_stage4,
            "total_s": avg_total,
            "input_tokens": avg_in_tok,
            "output_tokens": avg_out_tok,
        }
        avg_rows.append(avg_row)

        print(f"Question: {prompt}")
        print(f"  Status       : {status}")
        print(f"  Avg Time     : {avg_total:.2f}s (Router: {avg_stage1:.2f}s, Ctx: {avg_stage2:.3f}s, Gen: {avg_stage3:.2f}s, Exec: {avg_stage4:.3f}s)")
        print(f"  Avg Tokens   : In: {avg_in_tok}, Out: {avg_out_tok}, Total: {avg_in_tok + avg_out_tok}")
        print("-" * 80)

    # Write catalog_improvements.csv and catalog performance improvements.csv
    for target_path in (AVG_CSV_PATH, PERF_CSV_PATH):
        with target_path.open("w", newline="", encoding="utf-8") as f_avg:
            writer = csv.DictWriter(f_avg, fieldnames=AVG_FIELDNAMES)
            writer.writeheader()
            for r in avg_rows:
                writer.writerow(r)

    # Write catalog_improvements_clean_runs.csv
    clean_rows: list[dict] = []
    for prompt, runs in results_by_prompt.items():
        for idx, r in enumerate(runs, 1):
            clean_rows.append({
                "prompt": prompt,
                "run_number": idx,
                "status": r["status"],
                "stage1_router_s": r["stage1_router_s"],
                "stage2_context_s": r["stage2_context_s"],
                "stage3_generator_s": r["stage3_generator_s"],
                "stage4_val_exec_s": r["stage4_val_exec_s"],
                "total_s": r["total_s"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
            })

    with CLEAN_CSV_PATH.open("w", newline="", encoding="utf-8") as f_clean:
        clean_writer = csv.DictWriter(f_clean, fieldnames=CLEAN_FIELDNAMES)
        clean_writer.writeheader()
        for cr in clean_rows:
            clean_writer.writerow(cr)

    print(f"\nWrote per-question averages to: {AVG_CSV_PATH}")
    print(f"Wrote performance improvements to: {PERF_CSV_PATH}")
    print(f"Wrote clean runs to: {CLEAN_CSV_PATH}")
    print(f"Wrote raw runs to: {RAW_CSV_PATH}")


if __name__ == "__main__":
    asyncio.run(run_improvements_benchmark(3))
