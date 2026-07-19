
#!/usr/bin/env python3
"""Evaluate local models on Grafana ADK + MCP tool-calling tasks.

The script runs the same prompt set across one or more Ollama/LiteLLM models,
captures ADK function-call events, scores each run, and writes JSON + Markdown
reports under eval_results/.
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
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from grafana_ai.agent import (
    ALLOWED_MCP_TOOLS,
    AGENT_INSTRUCTION,
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
        id="datasource_discovery",
        category="chat_with_data",
        prompt="List the available Grafana datasources. Use a tool.",
        expected_tools=("list_datasources",),
        required_tools=("list_datasources",),
        notes="Low-ambiguity MCP discovery call.",
    ),
    EvalCase(
        id="dashboard_discovery",
        category="chat_with_data",
        prompt="List dashboards. Use a tool.",
        expected_tools=("list_dashboards", "search_dashboard", "search_dashboards"),
        notes="Accepts local aliases or the MCP dashboard search tool.",
    ),
    EvalCase(
        id="dashboard_summary",
        category="chat_with_data",
        prompt=(
            "Find the dashboard named Node Exporter F`ull and summarize its "
            "purpose. Use tools."
        ),
        expected_tools=(
            "list_dashboards",
            "search_dashboard",
            "search_dashboards",
            "get_dashboard_summary",
            "get_dashboard_by_uid",
        ),
        notes="Tests multi-step dashboard lookup and grounded answer synthesis.",
    ),
    EvalCase(
        id="datasource_health",
        category="ops_assistance",
        prompt="Check whether the Prometheus datasource is healthy. Use a tool.",
        expected_tools=("check_datasources_health", "list_datasources", "get_datasource"),
        notes="Tests operational troubleshooting against Grafana datasource APIs.",
    ),
    EvalCase(
        id="prometheus_query_graphable",
        category="quick_glance_graphing",
        prompt=(
            "Using the Prometheus datasource, query the metric up for the last "
            "5 minutes. Use a tool and return values that could be graphed."
        ),
        expected_tools=("query_prometheus", "list_datasources"),
        notes="Tests the graphing path: retrieving time-series data for visualization.",
    ),
]


COMPLEX_EVAL_CASES = [
    EvalCase(
        id="complex_dashboard_details",
        category="chat_with_data",
        prompt=(
            "Find the dashboard named Node Exporter Full, retrieve its details "
            "or summary using tools, and tell me its UID, datasource, and what "
            "kinds of system metrics it monitors."
        ),
        expected_tools=(
            "list_dashboards",
            "search_dashboard",
            "search_dashboards",
            "get_dashboard_summary",
            "get_dashboard_by_uid",
        ),
        required_any_tool_groups=(
            ("list_dashboards", "search_dashboard", "search_dashboards"),
            ("get_dashboard_summary", "get_dashboard_by_uid"),
        ),
        notes="Multi-step dashboard discovery plus detail retrieval.",
    ),
    EvalCase(
        id="complex_datasource_then_prometheus",
        category="quick_glance_graphing",
        prompt=(
            "First discover the available datasources. Then use the Prometheus "
            "datasource to query the 'up' metric as a time series for the last "
            "10 minutes. Return the result in a way that could be graphed."
        ),
        expected_tools=("list_datasources", "query_prometheus"),
        required_tools=("list_datasources", "query_prometheus"),
        notes="Tests chained datasource discovery and graphable Prometheus query.",
    ),
    EvalCase(
        id="complex_health_then_query",
        category="ops_assistance",
        prompt=(
            "Check whether the Prometheus datasource is healthy. If it is "
            "healthy, query 'up' for the last 5 minutes and explain whether "
            "targets appear to be up."
        ),
        expected_tools=(
            "check_datasources_health",
            "list_datasources",
            "query_prometheus",
        ),
        required_any_tool_groups=(
            ("check_datasources_health", "list_datasources"),
            ("query_prometheus",),
        ),
        notes="Tests conditional troubleshooting workflow.",
    ),
    EvalCase(
        id="complex_cpu_graph_query",
        category="quick_glance_graphing",
        prompt=(
            "Using Prometheus, fetch graphable CPU usage data for the node over "
            "the last 15 minutes. Use a PromQL query based on "
            "rate(node_cpu_seconds_total[5m]) and summarize what the graph "
            "would show."
        ),
        expected_tools=("query_prometheus", "list_datasources"),
        required_tools=("query_prometheus",),
        notes="Tests a more realistic graph-generation query.",
    ),
    EvalCase(
        id="complex_dashboard_to_metric",
        category="chat_with_data",
        prompt=(
            "Use tools to inspect the Node Exporter Full dashboard, identify "
            "one Prometheus metric it likely uses for CPU monitoring, then run "
            "a graphable Prometheus query for that kind of CPU data."
        ),
        expected_tools=(
            "list_dashboards",
            "search_dashboard",
            "search_dashboards",
            "get_dashboard_summary",
            "get_dashboard_by_uid",
            "query_prometheus",
        ),
        required_any_tool_groups=(
            ("list_dashboards", "search_dashboard", "search_dashboards"),
            ("get_dashboard_summary", "get_dashboard_by_uid"),
            ("query_prometheus",),
        ),
        notes="End-to-end chat-with-dashboard-data plus graphable metric retrieval.",
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate local Ollama models on Grafana ADK tool use."
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
    all_cases = EVAL_CASES + COMPLEX_EVAL_CASES
    cases = [case for case in all_cases if case.id in wanted]
    missing = wanted - {case.id for case in cases}
    if missing:
        raise SystemExit(f"Unknown case id(s): {', '.join(sorted(missing))}")
    return cases


def build_agent(model: str) -> LlmAgent:
    return LlmAgent(
        name="grafana_eval_agent",
        model=LiteLlm(model=model),
        instruction=(
            AGENT_INSTRUCTION
            + "\nEvaluation mode: prefer actually calling a relevant tool over "
            "describing how a tool could be called. Keep final answers concise."
        ),
        tools=[
            McpToolset(
                connection_params=StdioConnectionParams(
                    server_params=StdioServerParameters(
                        command="uvx",
                        args=["mcp-grafana"],
                        env={
                            "GRAFANA_URL": os.environ.get("GRAFANA_URL", "http://localhost:3000"),
                            "GRAFANA_SERVICE_ACCOUNT_TOKEN": os.environ.get(
                                "GRAFANA_SERVICE_ACCOUNT_TOKEN", ""
                            ),
                        },
                    )
                ),
                tool_filter=ALLOWED_MCP_TOOLS,
            ),
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

    if case.required_any_tool_groups:
        missing_groups = [
            group for group in case.required_any_tool_groups if not any(name in names for name in group)
        ]
        if not missing_groups:
            run.score += 1
            notes.append("All required tool-call groups were satisfied.")
        else:
            notes.append(
                "Missing required tool-call group(s): "
                + "; ".join(" or ".join(group) for group in missing_groups)
                + "."
            )
    else:
        missing_required = [name for name in case.required_tools if name not in names]
        if not missing_required:
            run.score += 1
            notes.append("All required tool calls were present.")
        else:
            notes.append(f"Missing required tool(s): {', '.join(missing_required)}.")

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
        max_score=10,
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
    except Exception as exc:  # ADK raises on invalid tool names.
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
        "project_goals": [
            "Chat with Grafana data through grounded tool calls.",
            "Return graphable metrics/log outputs for quick visual summaries.",
            "Support operator workflows with safe discovery, troubleshooting, and human review points.",
        ],
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
        "## Project Goals",
        "",
        "1. Chat with Grafana data through grounded tool calls.",
        "2. Produce graphable metrics/log outputs for quick visual summaries.",
        "3. Support operator workflows with safe discovery, troubleshooting, and human review points.",
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

    lines.extend(["", "## Detailed Runs", ""])
    for run in runs:
        calls = ", ".join(call["name"] for call in run.function_calls) or "none"
        responses = ", ".join(resp["name"] for resp in run.function_responses) or "none"
        lines.extend(
            [
                f"### {run.model} / {run.case_id} / trial {run.trial}",
                "",
                f"- Score: {run.score}/{run.max_score}",
                f"- Latency: {run.latency_sec}s",
                f"- Function calls: {calls}",
                f"- Function responses: {responses}",
                f"- Error: {run.error or 'none'}",
                f"- Final text: {run.final_text.strip()[:500] or 'none'}",
                f"- Notes: {'; '.join(run.scoring_notes)}",
                "",
            ]
        )

    (output_dir / "grafana_model_eval.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


async def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    missing_env = [
        name
        for name in ("GRAFANA_URL", "GRAFANA_SERVICE_ACCOUNT_TOKEN")
        if not os.environ.get(name)
    ]
    if missing_env:
        raise SystemExit(f"Missing required env vars: {', '.join(missing_env)}")

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
