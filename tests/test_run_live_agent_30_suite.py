"""Unit tests for scripts/run_live_agent_30_suite.py. All HTTP is mocked; no agent or OpenSearch is needed."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import requests

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


live = _load("run_live_agent_30_suite")
mtr = _load("manual_test_report")


class FakeResp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("Expecting value")
        return self._body


class FakeSession:
    """Plays back one item per call: a FakeResp is returned, an Exception is raised."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item


def cases(*ids):
    return [{"test_id": i, "category": "c", "question": f"question {i}", "expected_behavior": "x"} for i in ids]


def ok_body(status="ok", exec_status="success", pattern="fabric_health", intent="top_n"):
    return {"result": {"mode": "single", "status": status, "pattern_id": pattern, "intent": intent,
                       "query": {"size": 0}, "execution": {"execution_status": exec_status, "buckets": []}},
            "session_id": None}


def run(script, ids=("A-1",), tmp_path=None, **kw):
    out = tmp_path / "r.json"
    sess = FakeSession(script)
    doc, code = live.run_suite("http://agent:8000", out, cases=cases(*ids), session=sess, log=lambda *_: None, **kw)
    return doc, code, sess, out


def test_loads_the_real_30_questions():
    c = live.load_cases()
    assert len(c) == 30 and len({x["test_id"] for x in c}) == 30
    assert all(set(x) == {"test_id", "category", "question", "expected_behavior"} for x in c)  # no canned payloads


def test_success_records_real_values(tmp_path):
    doc, code, sess, out = run([FakeResp(200, ok_body())], tmp_path=tmp_path, api_key="s3cret")
    r = doc["results"][0]
    assert code == 0 and r["http_status"] == 200 and r["request_sent"] is True
    assert (r["application_status"], r["execution_status"]) == ("ok", "success")
    assert r["generated_opensearch_dsl"] == {"size": 0} and r["opensearch_execution_results"]["buckets"] == []
    assert r["raw_response"] == ok_body() and r["elapsed_seconds"] is not None
    assert sess.calls[0]["url"] == "http://agent:8000/api/v1/query"
    assert sess.calls[0]["json"] == {"question": "question A-1"}
    assert sess.calls[0]["headers"] == {"X-API-Key": "s3cret"}
    assert "s3cret" not in out.read_text()


def test_multi_result_statuses(tmp_path):
    body = {"result": {"mode": "multi", "results": [
        {"status": "ok", "query": {"a": 1}, "execution": {"execution_status": "success"}},
        {"status": "unmapped"}]}}
    r = run([FakeResp(200, body)], tmp_path=tmp_path)[0]["results"][0]
    assert r["application_status"] == "multi:ok,unmapped" and r["execution_status"] == "success"
    assert r["generated_opensearch_dsl"] == [{"a": 1}, None]


@pytest.mark.parametrize("status", [410, 500, 502, 504])
def test_http_errors_are_recorded_as_is(tmp_path, status):
    doc, code, *_ = run([FakeResp(status, {"detail": "boom"})], tmp_path=tmp_path)
    r = doc["results"][0]
    assert r["http_status"] == status and r["application_status"] == "none" and r["execution_status"] == "not_executed"
    assert "boom" in r["request_error"] and r["request_sent"] is True
    assert code == 0  # a real response was received; grading, not the runner, fails it
    assert mtr.grade(r)["http_ok"] is False


def test_connection_error_is_not_a_sent_request_and_has_no_status(tmp_path):
    doc, code, *_ = run([requests.exceptions.ConnectionError("refused")], tmp_path=tmp_path)
    r = doc["results"][0]
    assert r["http_status"] is None and r["request_sent"] is False and "connection error" in r["request_error"]
    assert code == 1
    g = mtr.grade(r)
    assert not g["http_ok"] and not g["status_ok"] and not g["semantic_ok"]


def test_connect_timeout_counts_as_connection_failure(tmp_path):
    r = run([requests.exceptions.ConnectTimeout("t")], tmp_path=tmp_path)[0]["results"][0]
    assert r["http_status"] is None and r["request_sent"] is False and "connect timeout" in r["request_error"]


def test_read_timeout_is_sent_without_status(tmp_path):
    doc, code, *_ = run([requests.exceptions.ReadTimeout("slow")], tmp_path=tmp_path, timeout=7)
    r = doc["results"][0]
    assert r["http_status"] is None and r["request_sent"] is True and "7s" in r["request_error"] and code == 1


def test_non_json_body(tmp_path):
    doc, code, *_ = run([FakeResp(200, None, text="<html>oops</html>")], tmp_path=tmp_path)
    r = doc["results"][0]
    assert r["http_status"] == 200 and r["response_parse_error"] and r["raw_response"] is None
    assert r["raw_response_text"] == "<html>oops</html>" and r["application_status"] == "none" and code == 1


def test_200_without_result_field_is_malformed(tmp_path):
    doc, code, *_ = run([FakeResp(200, {"hello": 1})], tmp_path=tmp_path)
    r = doc["results"][0]
    assert r["response_parse_error"] and r["application_status"] == "none" and code == 1
    assert mtr.grade(r)["status_ok"] is False


def test_partial_failure_keeps_earlier_results_and_lists_the_rest(tmp_path):
    script = [FakeResp(200, ok_body()), requests.exceptions.ConnectionError("down")]
    doc, code, sess, out = run(script, ids=("A-1", "A-2", "A-3", "A-4", "A-5"), tmp_path=tmp_path,
                               max_connect_failures=2)
    assert len(sess.calls) == 3  # A-1 ok, A-2 and A-3 refused, then stop
    rs = doc["results"]
    assert [r["id"] for r in rs] == ["A-1", "A-2", "A-3", "A-4", "A-5"]
    assert rs[0]["http_status"] == 200 and rs[0]["request_sent"] is True
    assert all(r["http_status"] is None and r["request_sent"] is False for r in rs[1:])
    assert "not sent" in rs[4]["request_error"]
    assert doc["complete"] is False and "consecutive connection failures" in doc["aborted_reason"]
    assert doc["requests_sent"] == 1 and doc["responses_received"] == 1 and code == 1
    assert json.loads(out.read_text())["results"][0]["http_status"] == 200


def test_file_is_updated_after_each_question(tmp_path):
    out = tmp_path / "r.json"
    seen = []

    class Spy(FakeSession):
        def post(self, *a, **k):
            if out.exists():
                seen.append(len(json.loads(out.read_text())["results"]))
            return super().post(*a, **k)

    live.run_suite("http://a", out, cases=cases("A-1", "A-2", "A-3"), session=Spy([FakeResp(200, ok_body())]),
                   log=lambda *_: None)
    assert seen == [0, 1, 2]


def test_keyboard_interrupt_preserves_results(tmp_path):
    doc, code, *_ = run([FakeResp(200, ok_body()), KeyboardInterrupt()], ids=("A-1", "A-2", "A-3"), tmp_path=tmp_path)
    assert code == 130 and doc["aborted_reason"] == "interrupted by the user"
    assert [r["http_status"] for r in doc["results"]] == [200, None, None]


def test_output_is_compatible_with_manual_test_report(tmp_path):
    ids = ("FH-01", "ILO-06", "ILO-09")
    script = [FakeResp(200, ok_body()),
              FakeResp(200, {"result": {"status": "unmapped"}}),
              FakeResp(200, ok_body(pattern="ilo_events"))]
    doc, code, _, out = run(script, ids=ids, tmp_path=tmp_path)
    data = json.loads(out.read_text())
    for r in data["results"]:
        assert {"id", "question", "http_status", "application_status", "execution_status"} <= set(r)
    rep = mtr.grade_run(data)
    assert rep["total"] == 3 and rep["http_ok"] == 3
    by_id = {t["id"]: t for t in rep["tests"]}
    assert by_id["FH-01"]["semantic_ok"] and by_id["ILO-06"]["semantic_ok"] and by_id["ILO-09"]["semantic_ok"]


def test_credentials_are_stripped_from_urls(tmp_path):
    assert live.strip_url_credentials("http://u:p@host:8000/x") == "http://host:8000/x"
    assert live.strip_url_credentials("http://host:8000") == "http://host:8000"
    out = tmp_path / "r.json"
    live.run_suite("http://u:p@agent:8000", out, cases=cases("A-1"), session=FakeSession([FakeResp(200, ok_body())]),
                   log=lambda *_: None)
    assert "u:p@" not in out.read_text()


def test_cli_help_and_args(tmp_path, capsys, monkeypatch):
    with pytest.raises(SystemExit) as e:
        live.main(["--help"])
    assert e.value.code == 0 and "--agent-url" in capsys.readouterr().out
    monkeypatch.delenv("VALIDATION_RESULTS_FILE", raising=False)
    with pytest.raises(SystemExit) as e:
        live.main([])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        live.main(["--out", str(tmp_path / "x.json"), "--ids", "NOPE-1"])
    assert e.value.code == 2


def test_main_with_ids_uses_http_and_exit_code(tmp_path, monkeypatch):
    sess = FakeSession([FakeResp(200, ok_body())])
    monkeypatch.setattr(live.requests, "Session", lambda: sess)
    out = tmp_path / "o.json"
    code = live.main(["--out", str(out), "--agent-url", "http://agent:9", "--ids", "FH-01,CL-01"])
    assert code == 0 and len(sess.calls) == 2 and sess.calls[0]["url"] == "http://agent:9/api/v1/query"
    assert [r["id"] for r in json.loads(out.read_text())["results"]] == ["FH-01", "CL-01"]


def test_master_runner_placeholders_expand_to_this_runners_cli(tmp_path):
    vf = _load("run_full_validation")
    cmd = "{python} scripts/run_live_agent_30_suite.py --out {results_file} --agent-url {agent_url}"
    run_ = vf.ValidationRun(stages=[5], run_dir=tmp_path, live_eval_cmd=cmd, agent_url="http://agent:8000")
    argv = run_._live_argv("http://agent:8000")
    assert argv[0] == sys.executable and "{" not in " ".join(argv)
    args = live.build_parser().parse_args(argv[2:])
    assert args.out == str(tmp_path / "live_eval" / "results.json") and args.agent_url == "http://agent:8000"
