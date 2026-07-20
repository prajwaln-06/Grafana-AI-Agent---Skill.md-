#!/usr/bin/env python3
"""Evaluate local models on Grafana ADK + Wrapper tool-calling tasks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from grafana_ai.agent import (
    AGENT_INSTRUCTION,
    _before_model,
)
from grafana_ai.tools.grafana_wrapper import (
    list_dashboards,
    search_dashboards,
    get_dashboard_panels,
    query_prometheus_metric,
)


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "eval_results"


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    prompt: str
    expected_tools: tuple[str, ...]
    required_tools: tuple[str, ...] = ()
    required_any_tool_groups: tuple[tuple[str, ...], ...] = ()
    forbidden_tools: tuple[str, ...] = (
        "google:search",
        "web_search",
        "browser",
    )
    notes: str = ""


@dataclass
class EvalRun:
    case_id: str
    category: str
    model: str
    trial: int
    prompt: str
    latency_sec: float
    score: int
    max_score: int
    function_calls: list[dict[str, Any]] = field(default_factory=list)
    function_responses: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    error: str | None = None
    scoring_notes: list[str] = field(default_factory=list)


EVAL_CASES = [
    EvalCase(
        id="dashboard_discovery",
        category="chat_with_data",
        prompt="List dashboards. Use a tool.",
        expected_tools=("list_dashboards",),
        notes="Tests basic list_dashboards wrapper.",
    ),
    EvalCase(
        id="dashboard_search",
        category="chat_with_data",
        prompt="Find dashboards related to cpu. Use a tool.",
        expected_tools=("search_dashboards",),
        notes="Tests search_dashboards wrapper.",
    ),
    EvalCase(
        id="dashboard_panels",
        category="chat_with_data",
        prompt="What panels are in dashboard UID xyz? Use a tool.",
        expected_tools=("get_dashboard_panels",),
        notes="Tests get_dashboard_panels wrapper.",
    ),
    EvalCase(
        id="prometheus_query_metric",
        category="quick_glance_graphing",
        prompt="What is the CPU usage? Use a tool.",
        expected_tools=("query_prometheus_metric",),
        notes="Tests query_prometheus_metric with natural language metric.",
    ),
    EvalCase(
        id="prometheus_query_expr",
        category="quick_glance_graphing",
        prompt="Query the PromQL 'up' for 5 minutes. Use a tool.",
        expected_tools=("query_prometheus_metric",),
        notes="Tests query_prometheus_metric with explicit PromQL expr.",
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate local Ollama models on Grafana ADK wrapper."
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=[],
        help=(
            "Space-separated LiteLLM model names to evaluate (e.g. qwen2.5:14b gemma4:12b)."
        ),
    )
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--timeout-sec", type=float, default=90)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--cases",
        default="",
        help="Comma-separated case ids to run. Defaults to all cases.",
    )
    return parser.parse_args()


def discover_ollama_models() -> list[str]:
    try:
        proc = subprocess.run(
            ["ollama", "list"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ["ollama_chat/gemma4:latest"]

    models: list[str] = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if parts:
            models.append(f"ollama_chat/{parts[0]}")
    return models or ["ollama_chat/gemma4:latest"]


def selected_models(raw_models: list[str]) -> list[str]:
    if raw_models:
        return [
            m if m.startswith("ollama") else f"ollama_chat/{m}"
            for m in raw_models
        ]
    return discover_ollama_models()


def selected_cases(raw_cases: str) -> list[EvalCase]:
    if not raw_cases.strip():
        return EVAL_CASES
    wanted = {case_id.strip() for case_id in raw_cases.split(",") if case_id.strip()}
    cases = [case for case in EVAL_CASES if case.id in wanted]
    missing = wanted - {case.id for case in cases}
    if missing:
        raise SystemExit(f"Unknown case id(s): {', '.join(sorted(missing))}")
    return cases


def build_agent(model: str) -> Agent:
    return Agent(
        name="grafana_eval_agent",
        model=LiteLlm(model=model, max_tokens=1536),
        instruction=(
            AGENT_INSTRUCTION
            + "\nEvaluation mode: prefer actually calling a relevant tool over "
            "describing how a tool could be called. Keep final answers concise."
        ),
        before_model_callback=_before_model,
        tools=[
            list_dashboards,
            search_dashboards,
            get_dashboard_panels,
            query_prometheus_metric,
        ],
    )


def part_to_call(part: Any) -> dict[str, Any] | None:
    call = getattr(part, "function_call", None)
    if not call:
        return None
    return {
        "id": getattr(call, "id", None),
        "name": getattr(call, "name", None),
        "args": dict(getattr(call, "args", {}) or {}),
    }


def part_to_response(part: Any) -> dict[str, Any] | None:
    response = getattr(part, "function_response", None)
    if not response:
        return None
    return {
        "id": getattr(response, "id", None),
        "name": getattr(response, "name", None),
        "response": getattr(response, "response", None),
    }


def score_run(case: EvalCase, run: EvalRun) -> None:
    notes: list[str] = []
    names = [call["name"] for call in run.function_calls]

    if run.error:
        notes.append("Run errored before completion.")
    if names:
        run.score += 2
        notes.append("Model emitted at least one tool call.")
    else:
        notes.append("No tool call emitted.")

    if any(name in case.expected_tools for name in names):
        run.score += 3
        notes.append("Model called an expected tool.")
    else:
        notes.append("No expected tool was called.")

    forbidden = [name for name in names if name in case.forbidden_tools]
    if not forbidden:
        run.score += 1
        notes.append("No known hallucinated/forbidden tool names.")
    else:
        notes.append(f"Forbidden tool name(s): {', '.join(forbidden)}.")

    if run.function_responses:
        run.score += 2
        notes.append("At least one tool response was returned.")
    else:
        notes.append("No tool response returned.")

    if run.final_text.strip() and run.function_responses:
        run.score += 1
        notes.append("Final answer was produced after tool output.")
    elif run.final_text.strip():
        notes.append("Final answer was produced without tool output.")

    run.scoring_notes = notes


async def run_case(
    *,
    runner: Runner,
    model: str,
    case: EvalCase,
    trial: int,
    timeout_sec: float,
) -> EvalRun:
    session_id = f"{case.id}-{trial}-{uuid.uuid4().hex[:8]}"
    user_id = "eval-user"
    await runner.session_service.create_session(
        app_name="grafana_eval",
        user_id=user_id,
        session_id=session_id,
    )
    message = types.Content(
        role="user",
        parts=[types.Part(text=case.prompt)],
    )

    result = EvalRun(
        case_id=case.id,
        category=case.category,
        model=model,
        trial=trial,
        prompt=case.prompt,
        latency_sec=0.0,
        score=0,
        max_score=9,
    )
    started = time.perf_counter()

    async def collect_events() -> None:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=message,
        ):
            if getattr(event, "error_code", None) or getattr(event, "error_message", None):
                result.error = (
                    f"{getattr(event, 'error_code', '')}: "
                    f"{getattr(event, 'error_message', '')}"
                ).strip()
            content = getattr(event, "content", None)
            if not content:
                continue
            for part in content.parts or []:
                call = part_to_call(part)
                if call:
                    result.function_calls.append(call)
                response = part_to_response(part)
                if response:
                    result.function_responses.append(response)
                text = getattr(part, "text", None)
                if text and not getattr(part, "thought", False):
                    result.final_text += text

    try:
        await asyncio.wait_for(collect_events(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        result.error = f"Timed out after {timeout_sec:.0f}s"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.latency_sec = round(time.perf_counter() - started, 3)

    score_run(case, result)
    return result


async def run_model(model: str, cases: list[EvalCase], trials: int, timeout_sec: float) -> list[EvalRun]:
    print(f"\n=== Evaluating {model} ===", flush=True)
    session_service = InMemorySessionService()
    agent = build_agent(model)
    runner = Runner(
        app_name="grafana_eval",
        agent=agent,
        session_service=session_service,
    )
    runs: list[EvalRun] = []
    try:
        for case in cases:
            for trial in range(1, trials + 1):
                print(f"- {case.id} trial {trial}", flush=True)
                runs.append(
                    await run_case(
                        runner=runner,
                        model=model,
                        case=case,
                        trial=trial,
                        timeout_sec=timeout_sec,
                    )
                )
    finally:
        close = getattr(runner, "close", None)
        if close:
            maybe = close()
            if asyncio.iscoroutine(maybe):
                await maybe
    return runs


def summarize_model(runs: list[EvalRun]) -> dict[str, Any]:
    total_score = sum(run.score for run in runs)
    total_possible = sum(run.max_score for run in runs)
    tool_successes = sum(1 for run in runs if run.function_responses)
    errored = sum(1 for run in runs if run.error)
    avg_latency = sum(run.latency_sec for run in runs) / max(len(runs), 1)
    return {
        "model": runs[0].model if runs else "",
        "score": total_score,
        "max_score": total_possible,
        "percent": round(100 * total_score / max(total_possible, 1), 1),
        "tool_successes": tool_successes,
        "runs": len(runs),
        "errors": errored,
        "avg_latency_sec": round(avg_latency, 2),
    }


def write_reports(output_dir: Path, runs: list[EvalRun], cases: list[EvalCase]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cases": [asdict(case) for case in cases],
        "runs": [asdict(run) for run in runs],
    }
    (output_dir / "grafana_model_eval.json").write_text(
        json.dumps(payload, indent=2, default=str),
        encoding="utf-8",
    )

    by_model: dict[str, list[EvalRun]] = {}
    for run in runs:
        by_model.setdefault(run.model, []).append(run)
    summary = sorted(
        [summarize_model(model_runs) for model_runs in by_model.values()],
        key=lambda item: (item["percent"], item["tool_successes"], -item["errors"]),
        reverse=True,
    )

    lines = [
        "# Grafana Model Evaluation",
        "",
        "## Leaderboard",
        "",
        "| Rank | Model | Score | Tool successes | Errors | Avg latency |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for idx, item in enumerate(summary, start=1):
        lines.append(
            f"| {idx} | `{item['model']}` | {item['score']}/{item['max_score']} "
            f"({item['percent']}%) | {item['tool_successes']}/{item['runs']} | "
            f"{item['errors']} | {item['avg_latency_sec']}s |"
        )

    (output_dir / "grafana_model_eval.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


async def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    models = selected_models(args.models)
    cases = selected_cases(args.cases)
    all_runs: list[EvalRun] = []
    for model in models:
        all_runs.extend(await run_model(model, cases, args.trials, args.timeout_sec))
    write_reports(args.output_dir, all_runs, cases)
    print(f"\nWrote {args.output_dir / 'grafana_model_eval.json'}")
    print(f"Wrote {args.output_dir / 'grafana_model_eval.md'}")


if __name__ == "__main__":
    asyncio.run(main())
