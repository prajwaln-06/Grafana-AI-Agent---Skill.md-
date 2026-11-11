"""The manual-test grader separates HTTP success, application status and semantic correctness."""
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("manual_test_report", Path(__file__).resolve().parent.parent / "scripts" / "manual_test_report.py")
mtr = importlib.util.module_from_spec(spec)
import sys; sys.modules["manual_test_report"] = mtr
spec.loader.exec_module(mtr)


def rec(id_, outcome="ok", http=200, exec_=("success",), **summary):
    s = {"outcome": outcome, "result_statuses": [outcome], "execution_statuses": list(exec_), "pattern_ids": [], "intents": [],
         "analysis_kinds": [], **summary}
    return {"id": id_, "question": "q", "http_status": http, "summary": s}


def test_http_200_alone_is_not_a_pass():
    g = mtr.grade(rec("X-1", "validation_failed", exec_=()))
    assert g["http_ok"] and not g["status_ok"] and not g["semantic_ok"]


def test_a_non_200_response_fails_the_http_dimension():
    assert not mtr.grade(rec("X-1", http=500))["http_ok"]


def test_unmapped_passes_only_when_the_expectation_says_so():
    ok = mtr.grade(rec("ILO-06", "unmapped", exec_=()))
    assert ok["status_ok"] and ok["semantic_ok"]
    assert not mtr.grade(rec("ILO-06", "unsupported_metric", exec_=()))["status_ok"]
    assert not mtr.grade(rec("X-1", "unmapped", exec_=()))["status_ok"]          # default expects ok


def test_an_empty_result_is_valid_but_flagged_for_review():
    g = mtr.grade(rec("FH-02", exec_=("empty_result",)))
    assert g["status_ok"] and g["empty"] and g["needs_review"]


def test_semantic_expectations_are_checked_separately_from_status():
    e = mtr.Expect(pattern_ids=frozenset({"ilo_events"}), intents=frozenset({"top_n"}), analysis_kinds=frozenset())
    g = mtr.grade(rec("X", pattern_ids=["fabric_health"], intents=["rows"]), e)
    assert g["status_ok"] and not g["semantic_ok"] and len(g["problems"]) == 2


def test_nested_aggregation_expectation_needs_a_bucket_hierarchy():
    r = rec("ILO-10", pattern_ids=["ilo_events"], intents=["top_n"])
    assert not mtr.grade(r)["semantic_ok"]
    r["raw_response"] = {"result": {"execution": {"buckets": [{"key": "h", "sub_buckets": [{"key": "1"}]}]}}}
    assert mtr.grade(r)["semantic_ok"]


def test_run_summary_reports_three_counts_and_the_old_claim(tmp_path, capsys):
    run = {"passed": 2, "results": [rec("A", exec_=("success",)), rec("CL-07", "validation_failed", exec_=())]}
    rep = mtr.grade_run(run)
    assert (rep["http_ok"], rep["status_ok"], rep["semantic_ok"], rep["reported_passed_by_old_harness"]) == (2, 1, 1, 2)
    p = tmp_path / "r.json"
    p.write_text(json.dumps(run))
    assert mtr.main([str(p), "--strict"]) == 1 and mtr.main([str(p)]) == 0
    assert "CL-07" in capsys.readouterr().out
