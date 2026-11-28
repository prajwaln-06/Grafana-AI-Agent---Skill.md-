#!/usr/bin/env python3
"""Live HTTP evaluation runner for the 30 OpenSearch mentor questions (validation Stage 5).

Unlike ``run_opensearch_30_suite.py`` (which calls the pipeline in-process, uses canned router/generator
payloads and records a fixed ``http_status`` of 200), this runner sends a real ``POST /api/v1/query`` to a
running agent for every question and records exactly what came back.

* The 30 questions, ids, categories and expected-behavior text are imported from ``SUITE_CASES`` in
  ``run_opensearch_30_suite.py``. Its canned ``router`` / ``generator`` / ``direct_opensearch`` fields are
  never used. Grading expectations live in ``manual_test_report.py`` and are not touched here.
* ``http_status`` is the status code the server actually returned. It is ``None`` when no response was
  received (connection refused, timeout). Nothing is ever defaulted to 200.
* There are no retries and no fallbacks: a failed request is recorded as failed.
* The output file is rewritten after every question, so results survive a crash, Ctrl-C or an agent that
  goes away part-way through. Questions that were never sent are still listed, with ``request_sent: false``.
* The output has the shape ``manual_test_report.py`` grades: a ``results`` list whose records carry ``id``,
  ``question``, ``http_status``, ``application_status``, ``execution_status`` (plus elapsed time, generated
  DSL, execution results and the raw response).

Auth: if the agent has ``API_KEY`` set, export the same value as ``API_KEY`` here; it is sent as the
``X-API-Key`` header and is never written to the output file.

Exit codes: 0 every question received an HTTP response and a parseable body (grading is separate);
1 some question had no usable response, or the run was aborted; 2 usage error; 130 interrupted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DEFAULT_AGENT_URL = "http://localhost:8000"
QUERY_PATH = "/api/v1/query"
DEFAULT_TIMEOUT = 300.0
DEFAULT_MAX_CONNECT_FAILURES = 3
RUNNER_NAME = "run_live_agent_30_suite"


def load_cases() -> list[dict[str, Any]]:
    """The 30 question definitions from the pipeline-level suite (ids, text, expected behavior only)."""
    from scripts.run_opensearch_30_suite import SUITE_CASES  # heavy import (app package), so done lazily

    return [
        {"test_id": c["test_id"], "category": c.get("category"), "question": c["question"],
         "expected_behavior": c.get("expected_behavior")}
        for c in SUITE_CASES
    ]


def strip_url_credentials(url: str) -> str:
    """Drop ``user:pass@`` from a URL before it is stored anywhere."""
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def query_endpoint(agent_url: str) -> str:
    return agent_url.rstrip("/") + QUERY_PATH


# ---- response interpretation (mirrors the shapes the server and manual_test_report.py use) -------------

def _entries(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    if result.get("mode") == "multi" and isinstance(result.get("results"), list):
        return [e for e in result["results"] if isinstance(e, dict)]
    return [result]


def interpret_result(result: Any) -> dict[str, Any]:
    """application_status / execution_status / generated DSL / execution details from ``response["result"]``."""
    entries = _entries(result)
    is_multi = isinstance(result, dict) and result.get("mode") == "multi"
    if is_multi:
        app_status = "multi:" + ",".join(str(e.get("status", "none")) for e in entries)
    elif isinstance(result, dict):
        app_status = str(result.get("status", "none"))
    else:
        app_status = "none"
    exec_statuses = []
    for e in entries:
        ex = e.get("execution")
        if isinstance(ex, dict):
            s = ex.get("execution_status") or ex.get("status")
            if s:
                exec_statuses.append(str(s))
    if is_multi:
        dsl: Any = [e.get("query") for e in entries]
        details: Any = [e.get("execution") for e in entries]
    elif isinstance(result, dict):
        dsl, details = result.get("query"), result.get("execution")
    else:
        dsl = details = None
    return {
        "application_status": app_status,
        "execution_status": ",".join(exec_statuses) if exec_statuses else "not_executed",
        "generated_opensearch_dsl": dsl,
        "opensearch_execution_results": details,
    }


def _record(case: dict[str, Any], url: str, *, request_sent: bool, elapsed: float | None, http_status: int | None,
            raw_response: Any = None, raw_text: str | None = None, request_error: str | None = None,
            parse_error: str | None = None, unavailable: str | None = None) -> dict[str, Any]:
    info = {"application_status": "none", "execution_status": "not_executed",
            "generated_opensearch_dsl": None, "opensearch_execution_results": None}
    if isinstance(raw_response, dict) and "result" in raw_response and parse_error is None:
        info = interpret_result(raw_response.get("result"))
    return {
        "id": case["test_id"],
        "test_id": case["test_id"],
        "category": case.get("category"),
        "question": case["question"],
        "expected_behavior": case.get("expected_behavior"),
        "request": {"method": "POST", "url": strip_url_credentials(url), "json": {"question": case["question"]}},
        "request_sent": request_sent,
        "elapsed_seconds": elapsed,
        "request_execution_time_seconds": elapsed,
        "http_status": http_status,
        "application_status": info["application_status"],
        "execution_status": info["execution_status"],
        "generated_opensearch_dsl": info["generated_opensearch_dsl"],
        "opensearch_execution_results": info["opensearch_execution_results"],
        "response_parse_error": parse_error,
        "request_error": request_error,
        "response_unavailable_reason": unavailable,
        "raw_response": raw_response,
        "raw_response_text": raw_text,
    }


def run_question(case: dict[str, Any], agent_url: str, session: Any, timeout: float,
                 headers: dict[str, str] | None = None) -> tuple[dict[str, Any], bool]:
    """Send one question. Returns (record, connection_failed). Never raises for network or parse problems."""
    url = query_endpoint(agent_url)
    t0 = time.perf_counter()
    try:
        resp = session.post(url, json={"question": case["question"]}, headers=headers or {}, timeout=timeout)
    except requests.exceptions.ConnectTimeout as exc:  # must precede Timeout: it subclasses both
        return _record(case, url, request_sent=False, elapsed=time.perf_counter() - t0, http_status=None,
                       request_error=f"connect timeout: {type(exc).__name__}",
                       unavailable="could not connect to the agent; no response"), True
    except requests.exceptions.Timeout as exc:
        return _record(case, url, request_sent=True, elapsed=time.perf_counter() - t0, http_status=None,
                       request_error=f"read timeout after {timeout:g}s: {type(exc).__name__}",
                       unavailable="the agent did not answer within the timeout"), False
    except requests.exceptions.ConnectionError as exc:
        return _record(case, url, request_sent=False, elapsed=time.perf_counter() - t0, http_status=None,
                       request_error=f"connection error: {type(exc).__name__}",
                       unavailable="could not connect to the agent; no response"), True
    except requests.exceptions.RequestException as exc:
        return _record(case, url, request_sent=True, elapsed=time.perf_counter() - t0, http_status=None,
                       request_error=f"request failed: {type(exc).__name__}",
                       unavailable="request failed before a response was received"), False

    elapsed = time.perf_counter() - t0
    status = int(resp.status_code)
    text = resp.text
    try:
        body = resp.json()
    except ValueError as exc:
        return _record(case, url, request_sent=True, elapsed=elapsed, http_status=status, raw_text=text,
                       parse_error=f"response body is not valid JSON: {exc}",
                       unavailable="response body was not JSON"), False

    if status == 200 and not (isinstance(body, dict) and "result" in body):
        return _record(case, url, request_sent=True, elapsed=elapsed, http_status=status, raw_response=body,
                       raw_text=text, parse_error="HTTP 200 body has no 'result' field (not a QueryResponse)",
                       unavailable="response did not match the /api/v1/query schema"), False
    err = None
    if status != 200:
        detail = body.get("detail") if isinstance(body, dict) else None
        err = f"HTTP {status}" + (f": {detail}" if isinstance(detail, str) else "")
    return _record(case, url, request_sent=True, elapsed=elapsed, http_status=status, raw_response=body,
                   raw_text=text, request_error=err), False


def _not_attempted(case: dict[str, Any], url: str, reason: str) -> dict[str, Any]:
    return _record(case, url, request_sent=False, elapsed=None, http_status=None, request_error=reason,
                   unavailable=reason)


def build_document(agent_url: str, records: list[dict[str, Any]], total: int, *, started: str,
                   aborted: str | None, complete: bool) -> dict[str, Any]:
    received = sum(1 for r in records if r["http_status"] is not None)
    return {
        "runner": RUNNER_NAME,
        "live_http": True,
        "started_at_utc": started,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent_url": strip_url_credentials(agent_url),
        "endpoint": QUERY_PATH,
        "total": total,
        "requests_sent": sum(1 for r in records if r["request_sent"]),
        "responses_received": received,
        "complete": complete,
        "aborted_reason": aborted,
        "results": records,
    }


def write_document(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def run_suite(agent_url: str, out: Path, *, cases: list[dict[str, Any]] | None = None, session: Any = None,
              timeout: float = DEFAULT_TIMEOUT, api_key: str | None = None,
              max_connect_failures: int = DEFAULT_MAX_CONNECT_FAILURES, log=print) -> tuple[dict[str, Any], int]:
    """Run the questions in order, rewriting ``out`` after each one. Returns (document, exit_code)."""
    cases = load_cases() if cases is None else cases
    sess = session if session is not None else requests.Session()
    headers = {"X-API-Key": api_key} if api_key else {}
    url = query_endpoint(agent_url)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    records: list[dict[str, Any]] = []
    aborted: str | None = None
    consecutive = 0
    interrupted = False

    write_document(out, build_document(agent_url, records, len(cases), started=started, aborted=None, complete=False))
    for i, case in enumerate(cases):
        try:
            rec, conn_failed = run_question(case, agent_url, sess, timeout, headers)
        except KeyboardInterrupt:
            interrupted = True
            aborted = "interrupted by the user"
            break
        records.append(rec)
        consecutive = consecutive + 1 if conn_failed else 0
        log(f"[{case['test_id']:6s}] http={rec['http_status']!s:5s} app={rec['application_status']:22s} "
            f"exec={rec['execution_status']:14s} {rec['elapsed_seconds'] or 0:.1f}s"
            + (f"  ERROR: {rec['request_error']}" if rec["request_error"] else ""))
        write_document(out, build_document(agent_url, records, len(cases), started=started, aborted=None,
                                           complete=False))
        if max_connect_failures and consecutive >= max_connect_failures and i + 1 < len(cases):
            aborted = (f"aborted after {consecutive} consecutive connection failures; "
                       f"remaining questions were not sent")
            break

    for case in cases[len(records):]:
        records.append(_not_attempted(case, url, f"not sent: {aborted or 'run ended early'}"))
    doc = build_document(agent_url, records, len(cases), started=started, aborted=aborted,
                         complete=aborted is None)
    write_document(out, doc)
    usable = all(r["http_status"] is not None and r["response_parse_error"] is None for r in records)
    code = 130 if interrupted else (0 if usable and aborted is None else 1)
    return doc, code


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Send the 30 OpenSearch mentor questions to a running agent over HTTP "
                    "(POST /api/v1/query) and write manual_test_report-compatible JSON.")
    ap.add_argument("--out", default=os.environ.get("VALIDATION_RESULTS_FILE"), metavar="FILE",
                    help="Results JSON to write (default: $VALIDATION_RESULTS_FILE).")
    ap.add_argument("--agent-url", default=os.environ.get("VALIDATION_AGENT_URL") or os.environ.get("AGENT_URL")
                    or DEFAULT_AGENT_URL, metavar="URL",
                    help=f"Agent base URL (default: $VALIDATION_AGENT_URL, $AGENT_URL, else {DEFAULT_AGENT_URL}).")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SEC",
                    help=f"Per-request read timeout in seconds (default {DEFAULT_TIMEOUT:g}).")
    ap.add_argument("--ids", metavar="ID[,ID...]", help="Only run these question ids (e.g. FH-01,CL-03). "
                    "A partial run is recorded as such; it is not the full 30.")
    ap.add_argument("--max-connect-failures", type=int, default=DEFAULT_MAX_CONNECT_FAILURES, metavar="N",
                    help="Stop sending after N consecutive connection failures (0 = never stop). "
                         f"Default {DEFAULT_MAX_CONNECT_FAILURES}.")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.out:
        ap.error("--out is required (or set VALIDATION_RESULTS_FILE)")
    if args.timeout <= 0:
        ap.error("--timeout must be positive")
    cases = load_cases()
    if args.ids:
        wanted = [s.strip().upper() for s in args.ids.split(",") if s.strip()]
        unknown = [w for w in wanted if w not in {c["test_id"].upper() for c in cases}]
        if unknown:
            ap.error(f"unknown question id(s): {', '.join(unknown)}")
        cases = [c for c in cases if c["test_id"].upper() in wanted]
    print(f"Live HTTP evaluation: {len(cases)} question(s) -> {query_endpoint(strip_url_credentials(args.agent_url))}")
    doc, code = run_suite(args.agent_url, Path(args.out), cases=cases, timeout=args.timeout,
                          api_key=os.environ.get("API_KEY") or None, max_connect_failures=args.max_connect_failures)
    print(f"{doc['responses_received']}/{doc['total']} responses received; wrote {args.out}")
    if doc["aborted_reason"]:
        print(f"WARNING: {doc['aborted_reason']}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
