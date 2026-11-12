"""Grade a manual-test run (test_output_all.json) on THREE separate dimensions.

  http_ok      the agent answered HTTP 200
  status_ok    the application status / execution status is the one the question's expectation names
  semantic_ok  the result is the right KIND of answer (pattern, aggregation shape, analysis, rows vs buckets)

"31/31 passed" used to mean only http_ok. A question that is correctly unsupported (`unmapped`) is a PASS
only when the expectation says so; a `validation_failed` or an unexpected `unsupported_metric` is a FAIL even
though HTTP was 200. An empty result is reported as `empty_result` (valid, but flagged for review), never as
a pass-by-default.

Usage:  python scripts/manual_test_report.py test_output_all.json [--strict]
Exit code 1 when --strict and any question fails status or semantics.
This reads a recorded run; it never contacts the agent or OpenSearch.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

# application statuses that mean the pipeline refused or broke, whatever HTTP said
FAILURE_OUTCOMES = {"validation_failed", "error", "endpoint_error"}


@dataclass(frozen=True)
class Expect:
    """What a correct answer to one question looks like. Unset fields are not checked."""
    outcomes: frozenset = frozenset({"ok"})                  # acceptable application statuses
    pattern_ids: frozenset | None = None
    intents: frozenset | None = None
    analysis_kinds: frozenset | None = None
    executed: bool | None = None                             # True: must reach OpenSearch and succeed/empty
    nested_aggregation: bool | None = None                   # True: a bucket hierarchy is expected
    empty_ok: bool = True                                    # a genuinely empty result is acceptable
    note: str = ""


def E(*outcomes, **kw) -> Expect:
    return Expect(outcomes=frozenset(outcomes or {"ok"}), **{k: (frozenset(v) if isinstance(v, (set, list, tuple)) else v)
                                                            for k, v in kw.items()})


# Only the questions whose correct outcome is NOT simply "ok + executed" need an explicit entry.
EXPECTATIONS: dict[str, Expect] = {
    "CL-03": E("ok", pattern_ids={"controller_log"}, intents={"top_n"}, nested_aggregation=True, analysis_kinds=set(),
              note="recurring errors = repeated error ACTIVITY per host -> service, not identical-message recurrence"),
    "CL-07": E("ok", pattern_ids={"controller_log", "fabric_health"}, executed=True,
              note="needs a stated tolerance; without it the correct answer is a clarification (declined)"),
    "ILO-05": E("ok", pattern_ids={"ilo_events"}, intents={"top_n"}, nested_aggregation=True,
               note="login/logout FREQUENCY per BMC; not anomaly detection"),
    "ILO-06": E("unmapped", executed=False, note="failed-login wording is unverified; must stay unmapped, not unsupported_metric"),
    "ILO-09": E("ok", pattern_ids={"ilo_events"}, executed=True, note="bare Oem.Hpe.Repaired, never a fabricated .keyword"),
    "ILO-10": E("ok", pattern_ids={"ilo_events"}, nested_aggregation=True, executed=True,
               note="ONE top-level host terms with nested event-code terms"),
}
DEFAULT = Expect()


def _flat(summary: dict, key: str) -> list:
    v = summary.get(key)
    return list(v) if isinstance(v, list) else []


def _summary_for(record: dict) -> dict:
    s = record.get("summary")
    if isinstance(s, dict) and s:
        return s
    res = (record.get("raw_response") or {}).get("result") or {}
    entries = [e for e in (res.get("results") if isinstance(res.get("results"), list) else ([res] if res else []))
               if isinstance(e, dict)]
    statuses = [e["status"] for e in entries if e.get("status")]
    if not statuses and record.get("application_status") and record.get("application_status") != "none":
        statuses = [s.removeprefix("multi:") for s in str(record["application_status"]).split(",") if s]
    exec_statuses = [
        e["execution"].get("execution_status") or e["execution"].get("status")
        for e in entries
        if isinstance(e.get("execution"), dict) and (e["execution"].get("execution_status") or e["execution"].get("status"))
    ]
    if not exec_statuses and record.get("execution_status") and record.get("execution_status") != "not_executed":
        exec_statuses = [s for s in str(record["execution_status"]).split(",") if s]
    pattern_ids = [e["pattern_id"] for e in entries if e.get("pattern_id")]
    intents = [e["intent"] for e in entries if e.get("intent")]
    analysis_kinds = [e["analysis"]["type"] for e in entries
                      if isinstance(e.get("analysis"), dict) and e["analysis"].get("type")]
    return {
        "result_statuses": statuses,
        "execution_statuses": exec_statuses,
        "pattern_ids": pattern_ids,
        "intents": intents,
        "analysis_kinds": analysis_kinds,
    }


def _expect_for(record: dict) -> Expect:
    q = (record.get("question") or "").lower()
    if "near fabric availability" in q:
        return EXPECTATIONS["CL-07"]
    if "which services generate the most error logs" in q:
        return DEFAULT
    return EXPECTATIONS.get(record.get("id") or record.get("test_id"), DEFAULT)


def grade(record: dict, expect: Expect | None = None) -> dict:
    exp = expect or _expect_for(record)
    s = _summary_for(record)
    statuses = _flat(s, "result_statuses") or ([s["outcome"]] if s.get("outcome") else [])
    exec_statuses = _flat(s, "execution_statuses")
    problems: list[str] = []
    http_ok = record.get("http_status") == 200

    status = statuses[0] if len(statuses) == 1 else ("multi:" + ",".join(statuses) if statuses else None)
    if not http_ok:
        problems.append(f"HTTP {record.get('http_status')}")
    status_ok = http_ok
    if status is None:
        status_ok, _ = False, problems.append("no application status in the response")
    elif any(x in FAILURE_OUTCOMES for x in statuses) or any(x in FAILURE_OUTCOMES for x in exec_statuses):
        status_ok = False
        problems.append(f"application failure: {statuses} / {exec_statuses}")
    elif not all(x in exp.outcomes for x in statuses):
        status_ok = False
        problems.append(f"status {statuses} but expected one of {sorted(exp.outcomes)}")

    sem: list[str] = []
    if exp.pattern_ids is not None and not set(_flat(s, "pattern_ids")) >= exp.pattern_ids:
        sem.append(f"patterns {_flat(s, 'pattern_ids')} lack {sorted(exp.pattern_ids - set(_flat(s, 'pattern_ids')))}")
    if exp.intents is not None and not set(_flat(s, "intents")) & exp.intents:
        sem.append(f"intent {_flat(s, 'intents')} not in {sorted(exp.intents)}")
    if exp.analysis_kinds is not None and set(_flat(s, "analysis_kinds")) != exp.analysis_kinds:
        sem.append(f"analysis {_flat(s, 'analysis_kinds')} expected {sorted(exp.analysis_kinds)}")
    if exp.executed is True and not any(x in ("success", "empty_result") for x in exec_statuses):
        sem.append(f"expected an executed query, got execution {exec_statuses}")
    if exp.executed is False and exec_statuses:
        sem.append(f"expected NO query to run, got execution {exec_statuses}")
    if exp.nested_aggregation and "success" in exec_statuses and not _has_nested(record):
        sem.append("expected a nested bucket hierarchy (host -> second level)")
    empty = bool(exec_statuses) and all(x == "empty_result" for x in exec_statuses)
    if empty and not exp.empty_ok:
        sem.append("empty result where data is expected")
    problems += sem
    return {"id": record.get("id"), "question": record.get("question"), "http_ok": http_ok, "status_ok": status_ok,
            "semantic_ok": not sem and status_ok, "application_status": status, "execution_statuses": exec_statuses,
            "empty": empty, "needs_review": empty, "problems": problems, "expectation_note": exp.note}


def _has_nested(record: dict) -> bool:
    res = (record.get("raw_response") or {}).get("result") or {}
    entries = res.get("results") if isinstance(res.get("results"), list) else [res]
    for e in entries:
        for b in ((e.get("execution") or {}).get("buckets") or []):
            if b.get("sub_buckets"):
                return True
    return False


def grade_run(run: dict) -> dict:
    graded = [grade(r) for r in run.get("results", [])]
    n = len(graded)
    return {"total": n, "http_ok": sum(g["http_ok"] for g in graded), "status_ok": sum(g["status_ok"] for g in graded),
            "semantic_ok": sum(g["semantic_ok"] for g in graded), "empty_needs_review": sum(g["needs_review"] for g in graded),
            "reported_passed_by_old_harness": run.get("passed"), "tests": graded}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path")
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args(argv)
    with open(a.path, encoding="utf-8") as f:
        rep = grade_run(json.load(f))
    print(f"HTTP ok {rep['http_ok']}/{rep['total']} | status ok {rep['status_ok']}/{rep['total']} | "
          f"semantic ok {rep['semantic_ok']}/{rep['total']} | empty (review) {rep['empty_needs_review']} "
          f"| old harness said {rep['reported_passed_by_old_harness']}")
    for g in rep["tests"]:
        if g["problems"] or g["needs_review"]:
            print(f"  {g['id']}: " + ("; ".join(g["problems"]) or "empty result - verify it is a genuine empty"))
    return 1 if a.strict and rep["semantic_ok"] != rep["total"] else 0


if __name__ == "__main__":
    sys.exit(main())
