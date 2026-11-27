"""Offline tests for scripts/run_full_validation.py (the master validation runner).

Everything external is faked through the runner's injection points: the subprocess runner, the HTTP probes,
the settings loader and Popen. No OpenSearch, Gemini, Prometheus, Grafana or agent server is contacted, and
no real smoke script is executed."""
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_full_validation.py"
FIXED_NOW = datetime(2026, 11, 28, 1, 2, 3, tzinfo=timezone.utc)

GEMINI_KEY = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE12345"
OS_PASSWORD = "hunter2-os-password"
LIVE_CMD = "liverunner --out {results_file} --agent-url {agent_url}"


@pytest.fixture(scope="module")
def rfv():
    spec = importlib.util.spec_from_file_location("run_full_validation", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_full_validation"] = mod
    spec.loader.exec_module(mod)
    return mod


def make_settings(**over):
    base = dict(gemini_api_key=GEMINI_KEY, opensearch_url="http://os.example:9200", opensearch_auth=("osuser", OS_PASSWORD),
                opensearch_auth_username="osuser", opensearch_auth_password=OS_PASSWORD,
                prometheus_url="http://prom.example:9090", grafana_url="http://grafana.example:3000",
                alert_rule_creation_enabled=False, gemini_model="test-model", api_key=None, api_port=8000,
                grafana_service_account_token=None)
    base.update(over)
    return SimpleNamespace(**base)


def good_record(qid="Q-1", **over):
    rec = {"id": qid, "question": f"question {qid}", "http_status": 200, "application_status": "ok",
           "execution_status": "success", "elapsed_seconds": 1.5, "generated_opensearch_dsl": {"size": 0},
           "summary": {"result_statuses": ["ok"], "execution_statuses": ["success"], "pattern_ids": ["fabric_health"],
                       "intents": ["top_n"], "analysis_kinds": []}}
    rec.update(over)
    return rec


class FakeRunner:
    """Stands in for run_command. `behaviors` maps a key to a CommandResult or a callable(argv)->CommandResult."""

    def __init__(self, behaviors=None, live_records=None):
        self.calls = []
        self.behaviors = behaviors or {}
        self.live_records = [good_record()] if live_records is None else live_records

    @staticmethod
    def key(argv):
        if argv[0] == "git":
            return "git"
        joined = " ".join(argv)
        for flag, key in (("--check-skills", "s1"), ("--queries", "s2"), ("--save-mappings", "s3"), ("--examples", "s4")):
            if "smoke_test_opensearch.py" in joined and flag in joined:
                return key
        if "smoke_test_gemini.py" in joined:
            return "gemini"
        if "manual_test_report.py" in joined:
            return "report"
        return "live"

    def __call__(self, argv, *, timeout, cwd=None, env=None):
        argv = list(argv)
        self.calls.append(argv)
        key = self.key(argv)
        behavior = self.behaviors.get(key)
        if callable(behavior):
            return behavior(argv)
        if behavior is not None:
            return behavior
        return getattr(self, f"_default_{key}")(argv)

    @staticmethod
    def _result(argv, code=0, out="", err=""):
        from_mod = sys.modules["run_full_validation"]
        return from_mod.CommandResult(argv, code, out, err, 0.01)

    def _default_git(self, argv):
        out = {"status": "", "rev-parse": "feat/seven-stage-validation-automation" if "--abbrev-ref" in argv else "abc1234deadbeef"}
        return self._result(argv, 0, out.get(argv[1], ""))

    def _default_s1(self, argv):
        return self._result(argv, 0, "Registry <-> skill package (offline):\n   consistent\n   still unverified: x\nOK: registry and skill package agree.\n")

    def _default_s2(self, argv):
        return self._result(argv, 0, "[fabric_health] x\n   discovery=fresh usable=True\n   [fabric_health] ok=True stage=executor\n   [controller_log] ok=True stage=executor\n")

    def _default_s3(self, argv):
        target = Path(argv[argv.index("--save-mappings") + 1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "mapping_fabric_health.json").write_text("{}", encoding="utf-8")
        return self._result(argv, 0, "   mapping snapshot: x\n")

    def _default_s4(self, argv):
        return self._result(argv, 0, "   [fabric_health example 1] ok=True stage=guard\n   [controller_log example 1 context_window] ok=True stage=executor\n")

    def _default_gemini(self, argv):
        return self._result(argv, 0, "ok\n")

    def _default_report(self, argv):
        return self._result(argv, 0, "HTTP ok 1/1 | status ok 1/1 | semantic ok 1/1 | empty (review) 0 | old harness said None\n")

    def _default_live(self, argv):
        out = Path(argv[argv.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"results": self.live_records}), encoding="utf-8")
        return self._result(argv, 0, "asked questions\n")

    def keys(self):
        return [self.key(c) for c in self.calls]


class FakeProbes:
    def __init__(self, down=(), status=None):
        self.down = tuple(down)
        self.status = status or {}
        self.urls = []

    def http_get(self, url, *, auth=None, timeout=5.0):
        self.urls.append(url)
        sm = sys.modules["run_full_validation"]
        for frag, code in self.status.items():
            if frag in url:
                return sm.ProbeResult(200 <= code < 300, code)
        if any(frag in url for frag in self.down):
            return sm.ProbeResult(False, None, "ConnectionError")
        return sm.ProbeResult(True, 200)


class FakePopen:
    instances = []

    def __init__(self, argv, **kw):
        self.argv, self.terminated, self.killed = argv, False, False
        self.pid = 4242
        FakePopen.instances.append(self)

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def run_main(rfv, tmp_path, args=(), *, runner=None, probes=None, settings=None, settings_error=None, environ=None, popen=FakePopen):
    runner = runner or FakeRunner()
    probes = probes or FakeProbes()
    out = []
    loader = (lambda: (None, settings_error or "settings invalid: gemini_api_key (missing)")) if settings_error else \
        (lambda: (settings or make_settings(), None))
    code = rfv.main(["--output-dir", str(tmp_path / "runs"), *args], runner=runner, probes=probes, settings_loader=loader,
                    popen=popen, now=lambda: FIXED_NOW, out=out.append, environ=environ if environ is not None else {})
    run_dirs = sorted((tmp_path / "runs").glob("*")) if (tmp_path / "runs").exists() else []
    report = json.loads((run_dirs[-1] / "report.json").read_text(encoding="utf-8")) if run_dirs else None
    return code, report, run_dirs[-1] if run_dirs else None, out, runner, probes


def stage_status(report):
    return {s["stage"]: s["status"] for s in report["stages"]}


# ---------------------------------------------------------------------------------------------------------
# selection and ordering
# ---------------------------------------------------------------------------------------------------------

def test_parse_stage_spec(rfv):
    assert rfv.parse_stage_spec("all") == [0, 1, 2, 3, 4, 5, 6]
    assert rfv.parse_stage_spec("3") == [3]
    assert rfv.parse_stage_spec("1-4") == [1, 2, 3, 4]
    assert rfv.parse_stage_spec("5,1,3-4,3") == [1, 3, 4, 5]
    for bad in ("7", "4-2", "x", "1-", "-1"):
        with pytest.raises(ValueError):
            rfv.parse_stage_spec(bad)


def test_bad_stage_selection_is_a_usage_error(rfv, tmp_path):
    with pytest.raises(SystemExit) as exc:
        run_main(rfv, tmp_path, ["--stages", "9"])
    assert exc.value.code == 2


def test_stages_run_in_order_and_reuse_the_existing_scripts(rfv, tmp_path):
    code, report, _, _, runner, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", LIVE_CMD])
    keys = [k for k in runner.keys() if k != "git"]
    # stage 0's Gemini smoke test comes first, then stages 1..6 in numeric order
    assert keys == ["gemini", "s1", "s2", "s3", "s4", "live", "report"]
    assert [s["stage"] for s in report["stages"]] == [0, 1, 2, 3, 4, 5, 6]
    flags = {k: " ".join(c) for c in runner.calls for k in [runner.key(c)]}
    assert "smoke_test_opensearch.py --check-skills" in flags["s1"]
    assert "--queries --rows" in flags["s2"]
    assert "--examples --analyses" in flags["s4"]
    assert "manual_test_report.py" in flags["report"] and "--strict" in flags["report"]


def test_selecting_one_stage_adds_only_the_prerequisite_stage(rfv, tmp_path):
    code, report, _, out, runner, _ = run_main(rfv, tmp_path, ["--stages", "4"])
    assert [k for k in runner.keys() if k != "git"] == ["s4"]
    st = stage_status(report)
    assert st[0] == "PASS" and st[4] == "PASS"
    assert all(st[n] == "SKIPPED" for n in (1, 2, 3, 5, 6))
    assert report["complete_run"] is False and report["selected_stages"] == [4]
    assert any("stage 0" in ln and "added" in ln for ln in out)


def test_stage_6_alone_needs_no_prerequisites(rfv, tmp_path):
    code, report, _, _, runner, probes = run_main(rfv, tmp_path, ["--stages", "6"])
    assert stage_status(report)[0] == "SKIPPED"
    assert probes.urls == []


# ---------------------------------------------------------------------------------------------------------
# success, failure, blocking
# ---------------------------------------------------------------------------------------------------------

def test_full_successful_run_with_a_live_runner_passes(rfv, tmp_path):
    code, report, run_dir, _, runner, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", LIVE_CMD])
    assert code == 0
    assert report["overall"]["status"] == "PASS"
    assert set(stage_status(report).values()) == {"PASS"}
    assert report["complete_run"] is True
    s2 = report["stages"][2]
    assert s2["counts"]["passed"] == 2 and s2["commands"][0]["exit_code"] == 0
    assert report["stages"][3]["artifacts"]["mappings_dir"] == "mappings"
    assert (run_dir / "mappings" / "mapping_fabric_health.json").is_file()
    assert report["stages"][5]["counts"]["passed"] == 1
    assert report["git"] == {"branch": "feat/seven-stage-validation-automation", "commit": "abc1234deadbeef", "dirty": False}


def test_failed_stage_is_recorded_and_exit_code_is_one(rfv, tmp_path):
    runner = FakeRunner()
    runner.behaviors["s2"] = lambda argv: runner._result(argv, 1, "   [fabric_health] ok=False stage=guard detail=unknown_field\nFAILED: see above.\n", "boom")
    code, report, _, _, _, _ = run_main(rfv, tmp_path, runner=runner)
    st = stage_status(report)
    assert st[2] == "FAIL" and code == 1 and report["overall"]["status"] == "FAIL"
    s2 = report["stages"][2]
    assert "exit code 1" in s2["reasons"][0]
    assert any("ok=False" in r or "FAILED" in r for r in s2["reasons"])
    assert s2["counts"]["failed"] == 1


def test_independent_stages_continue_after_a_failure(rfv, tmp_path):
    runner = FakeRunner()
    runner.behaviors["s2"] = lambda argv: runner._result(argv, 1, "FAILED: see above.\n")
    code, report, _, _, _, _ = run_main(rfv, tmp_path, runner=runner)
    st = stage_status(report)
    assert st[2] == "FAIL"
    assert st[3] == "PASS" and st[4] == "PASS"       # snapshots and examples do not depend on stage 2's verdict
    assert "s3" in runner.keys() and "s4" in runner.keys()


def test_unreachable_opensearch_blocks_the_live_stages_but_not_the_offline_one(rfv, tmp_path):
    code, report, _, _, runner, _ = run_main(rfv, tmp_path, probes=FakeProbes(down=["os.example"]))
    st = stage_status(report)
    assert st[0] == "FAIL" and st[1] == "PASS"
    assert st[2] == st[3] == st[4] == st[5] == "BLOCKED"
    assert "s2" not in runner.keys() and "s3" not in runner.keys() and "s4" not in runner.keys()
    assert "not reachable" in report["stages"][2]["reasons"][0]
    assert code == 1                                   # stage 0 itself failed


def test_opensearch_401_is_reported_as_an_authorisation_problem(rfv, tmp_path):
    code, report, _, _, _, _ = run_main(rfv, tmp_path, probes=FakeProbes(status={"os.example": 401}))
    detail = {c["name"]: c["detail"] for c in report["stages"][0]["checks"]}["opensearch"]
    assert "not authorised" in detail and "401" in detail
    assert stage_status(report)[2] == "BLOCKED"


def test_unloadable_settings_block_every_dependent_stage(rfv, tmp_path):
    code, report, _, _, runner, probes = run_main(rfv, tmp_path, settings_error="settings invalid: gemini_api_key (missing)")
    st = stage_status(report)
    assert st[0] == "FAIL"
    assert all(st[n] == "BLOCKED" for n in (1, 2, 3, 4, 5))
    assert probes.urls == [] and [k for k in runner.keys() if k != "git"] == []
    assert "gemini_api_key" in report["stages"][1]["reasons"][0]


def test_prometheus_down_is_informational_unless_required(rfv, tmp_path):
    code, report, _, _, _, _ = run_main(rfv, tmp_path, probes=FakeProbes(down=["prom.example"]))
    checks = {c["name"]: c for c in report["stages"][0]["checks"]}
    assert checks["prometheus"]["status"] == "FAIL" and checks["prometheus"]["required"] is False
    assert stage_status(report)[0] == "PASS"
    code, report, _, _, _, _ = run_main(rfv, tmp_path / "b", ["--require-prometheus"], probes=FakeProbes(down=["prom.example"]))
    assert stage_status(report)[0] == "FAIL"


def test_grafana_is_probed_only_when_alert_creation_is_enabled(rfv, tmp_path):
    _, _, _, _, _, probes = run_main(rfv, tmp_path)
    assert not any("grafana" in u for u in probes.urls)
    _, report, _, _, _, probes = run_main(rfv, tmp_path / "b", settings=make_settings(alert_rule_creation_enabled=True),
                                          probes=FakeProbes(down=["grafana.example"]))
    assert any("grafana.example" in u for u in probes.urls)
    assert stage_status(report)[0] == "FAIL"


def test_stage_3_fails_when_no_snapshot_is_written_even_if_the_script_exits_zero(rfv, tmp_path):
    runner = FakeRunner()
    runner.behaviors["s3"] = lambda argv: runner._result(argv, 0, "   mapping snapshot: NOT SAVED (fetch failed)\n")
    code, report, _, _, _, _ = run_main(rfv, tmp_path, runner=runner)
    assert stage_status(report)[3] == "FAIL"
    assert "no mapping_*.json" in report["stages"][3]["reasons"][-1]


# ---------------------------------------------------------------------------------------------------------
# stage 5: live evaluation is never faked
# ---------------------------------------------------------------------------------------------------------

def test_stage_5_is_blocked_without_a_live_runner_and_nothing_is_substituted(rfv, tmp_path):
    code, report, _, _, runner, _ = run_main(rfv, tmp_path)
    s5 = report["stages"][5]
    assert s5["status"] == "BLOCKED"
    assert "no live evaluation runner configured" in s5["reasons"][0]
    assert "30_suite" in s5["reasons"][0]                      # says why the 30-suite is not substituted
    assert "live" not in runner.keys() and "gemini" not in runner.keys()   # no Gemini call without a runner
    assert code == 3 and report["overall"]["status"] == "INCONCLUSIVE"     # everything else passed, but less was proved
    assert report["live_eval_configured"] is False


def test_live_runner_comes_from_the_environment_variable(rfv, tmp_path):
    code, report, _, _, runner, _ = run_main(rfv, tmp_path, environ={"VALIDATION_LIVE_EVAL_CMD": LIVE_CMD})
    assert stage_status(report)[5] == "PASS" and "live" in runner.keys()


def test_http_200_alone_is_not_a_pass(rfv, tmp_path):
    bad = good_record("Q-2", application_status="validation_failed",
                      summary={"result_statuses": ["validation_failed"], "execution_statuses": [], "pattern_ids": [], "intents": [], "analysis_kinds": []})
    runner = FakeRunner(live_records=[good_record("Q-1"), bad])
    code, report, _, _, _, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", LIVE_CMD], runner=runner)
    s5 = report["stages"][5]
    assert s5["status"] == "FAIL" and code == 1
    assert s5["counts"] == {"passed": 1, "failed": 1, "blocked": 0, "skipped": 0, "inconclusive": 0}
    failed = [t for t in s5["tests"] if t["status"] == "FAIL"]
    assert failed[0]["id"] == "Q-2" and failed[0]["http_status"] == 200
    assert "validation_failed" in failed[0]["actual"] and failed[0]["expected"]


def test_empty_live_result_is_inconclusive_not_pass(rfv, tmp_path):
    empty = good_record("Q-3", summary={"result_statuses": ["ok"], "execution_statuses": ["empty_result"], "pattern_ids": ["fabric_health"], "intents": ["top_n"], "analysis_kinds": []})
    code, report, _, _, _, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", LIVE_CMD], runner=FakeRunner(live_records=[empty]))
    assert stage_status(report)[5] == "INCONCLUSIVE" and code == 3
    assert report["overall"]["individual_test_counts"]["inconclusive"] == 1


def test_live_runner_that_writes_no_results_fails_stage_5(rfv, tmp_path):
    runner = FakeRunner()
    runner.behaviors["live"] = lambda argv: runner._result(argv, 0, "did nothing\n")
    code, report, _, _, _, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", LIVE_CMD], runner=runner)
    assert stage_status(report)[5] == "FAIL"
    assert "no results file" in report["stages"][5]["reasons"][-1]


def test_live_runner_nonzero_exit_fails_even_if_partial_results_pass(rfv, tmp_path):
    runner = FakeRunner()
    inner = runner._default_live
    runner.behaviors["live"] = lambda argv: (inner(argv), runner._result(argv, 7, "", "crashed"))[1]
    code, report, _, _, _, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", LIVE_CMD], runner=runner)
    assert stage_status(report)[5] == "FAIL" and "exited with code 7" in report["stages"][5]["reasons"][0]


def test_live_stage_is_blocked_when_the_agent_server_is_down(rfv, tmp_path):
    code, report, _, _, runner, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", LIVE_CMD], probes=FakeProbes(down=["localhost:8000"]))
    assert stage_status(report)[5] == "BLOCKED" and "agent server unavailable" in report["stages"][5]["reasons"][0]
    assert "live" not in runner.keys()


def test_failed_gemini_smoke_blocks_stage_5(rfv, tmp_path):
    runner = FakeRunner()
    runner.behaviors["gemini"] = lambda argv: runner._result(argv, 1, "", "ImportError: google-genai")
    code, report, _, _, _, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", LIVE_CMD], runner=runner)
    assert stage_status(report)[5] == "BLOCKED" and "Gemini unavailable" in report["stages"][5]["reasons"][0]
    assert "live" not in runner.keys()


def test_existing_server_is_reused_and_never_started_or_stopped(rfv, tmp_path):
    FakePopen.instances.clear()
    code, report, _, _, _, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", LIVE_CMD, "--start-server"])
    assert FakePopen.instances == []
    detail = {c["name"]: c["detail"] for c in report["stages"][0]["checks"]}["agent_server"]
    assert "already running" in detail and "reused" in detail


def test_server_started_by_the_runner_is_stopped_but_only_that_one(rfv, tmp_path):
    FakePopen.instances.clear()
    calls = {"n": 0}

    class Flaky(FakeProbes):
        def http_get(self, url, *, auth=None, timeout=5.0):
            if url.endswith("/readyz"):
                calls["n"] += 1
                sm = sys.modules["run_full_validation"]
                return sm.ProbeResult(calls["n"] > 1, 200 if calls["n"] > 1 else None, None if calls["n"] > 1 else "ConnectionError")
            return super().http_get(url, auth=auth, timeout=timeout)

    code, report, _, _, _, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", LIVE_CMD, "--start-server"], probes=Flaky())
    assert len(FakePopen.instances) == 1 and FakePopen.instances[0].terminated
    assert stage_status(report)[5] == "PASS"


def test_missing_server_without_start_flag_is_not_started(rfv, tmp_path):
    FakePopen.instances.clear()
    code, report, _, _, _, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", LIVE_CMD], probes=FakeProbes(down=["localhost:8000"]))
    assert FakePopen.instances == []
    detail = {c["name"]: c["detail"] for c in report["stages"][0]["checks"]}["agent_server"]
    assert "--start-server" in detail


# ---------------------------------------------------------------------------------------------------------
# timeouts and real subprocess handling
# ---------------------------------------------------------------------------------------------------------

def test_timed_out_stage_fails_cleanly_and_the_run_continues(rfv, tmp_path):
    runner = FakeRunner()
    runner.behaviors["s2"] = lambda argv: rfv_result(rfv, argv, timed_out=True)
    code, report, _, _, _, _ = run_main(rfv, tmp_path, runner=runner)
    s2 = report["stages"][2]
    assert s2["status"] == "FAIL" and "timed out" in s2["reasons"][0]
    assert s2["commands"][0]["timed_out"] is True and s2["commands"][0]["exit_code"] is None
    assert stage_status(report)[3] == "PASS"


def rfv_result(rfv, argv, timed_out=False):
    return rfv.CommandResult(argv, None, "partial output\n", "", 0.5, timed_out=timed_out, error="timed out after 5s" if timed_out else "could not start: OSError")


def test_run_command_times_out_and_returns_partial_output(rfv):
    cr = rfv.run_command([sys.executable, "-c", "import time,sys; print('started', flush=True); time.sleep(30)"], timeout=1.0)
    assert cr.timed_out is True and cr.exit_code is None and "timed out" in cr.error
    assert cr.duration_s < 10


def test_run_command_captures_output_and_exit_code(rfv):
    cr = rfv.run_command([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"], timeout=30)
    assert (cr.exit_code, cr.stdout.strip(), cr.stderr.strip(), cr.timed_out) == (3, "out", "err", False)


def test_run_command_reports_a_missing_executable_without_raising(rfv):
    cr = rfv.run_command(["definitely-not-a-real-command-xyz"], timeout=5)
    assert cr.exit_code is None and "could not start" in cr.error


def test_an_internal_error_in_one_stage_does_not_stop_the_run(rfv, tmp_path):
    runner = FakeRunner()

    def explode(argv):
        raise RuntimeError("bug in fake")
    runner.behaviors["s1"] = explode
    code, report, run_dir, _, _, _ = run_main(rfv, tmp_path, runner=runner)
    st = stage_status(report)
    assert st[1] == "FAIL" and st[2] == "PASS"
    assert "runner error in stage 1" in report["stages"][1]["reasons"][0]
    assert (run_dir / "stages" / "stage1_internal_error.txt").is_file()


# ---------------------------------------------------------------------------------------------------------
# reports and artifacts
# ---------------------------------------------------------------------------------------------------------

def test_reports_and_per_command_output_are_written(rfv, tmp_path):
    code, report, run_dir, _, _, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", LIVE_CMD])
    assert run_dir.name == "20261128T010203Z"
    md = (run_dir / "report.md").read_text(encoding="utf-8")
    for needle in ("Overall: PASS", "feat/seven-stage-validation-automation", "abc1234deadbeef", "Stage 5", "Live question evaluation"):
        assert needle in md
    assert report["run_id"] == "20261128T010203Z" and report["started_at"] == "2026-11-28T01:02:03Z"
    assert (run_dir / "stages" / "stage1_check_skills.stdout.txt").read_text(encoding="utf-8").startswith("Registry")
    assert (run_dir / "live_eval" / "results.json").is_file()
    for key in ("overall", "stages", "git", "configuration", "selected_stages", "complete_run", "duration_s"):
        assert key in report
    assert report["overall"]["stage_counts"]["PASS"] == 7


def test_a_second_run_never_overwrites_the_first(rfv, tmp_path):
    _, _, first, _, _, _ = run_main(rfv, tmp_path)
    marker = first / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    _, _, second, _, _, _ = run_main(rfv, tmp_path)
    assert first != second and marker.read_text(encoding="utf-8") == "keep"
    assert second.name == "20261128T010203Z-1"


def test_partial_report_marks_the_run_as_partial(rfv, tmp_path):
    code, report, run_dir, out, _, _ = run_main(rfv, tmp_path, ["--stages", "1"])
    assert "PARTIAL" in (run_dir / "report.md").read_text(encoding="utf-8")
    assert any("partial run" in ln for ln in out)


def test_terminal_output_has_progress_and_a_final_summary(rfv, tmp_path):
    code, report, run_dir, out, _, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", LIVE_CMD])
    text = "\n".join(out)
    assert "[stage 0]" in text and "[stage 6]" in text and "OVERALL: PASS" in text and str(run_dir / "report.md") in text
    text.encode("ascii")           # the team runs this on Windows consoles: ASCII only


# ---------------------------------------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------------------------------------

def test_dry_run_prints_the_plan_and_runs_and_writes_nothing(rfv, tmp_path):
    code, report, run_dir, out, runner, probes = run_main(rfv, tmp_path, ["--dry-run", "--live-eval-cmd", LIVE_CMD])
    assert code == 0 and report is None
    assert runner.calls == [] and probes.urls == []
    assert not (tmp_path / "runs").exists()
    text = "\n".join(out)
    for needle in ("DRY RUN", "--check-skills", "--queries --rows", "--save-mappings", "--examples --analyses",
                   "liverunner", "manual_test_report.py", "smoke_test_gemini.py"):
        assert needle in text
    assert text.index("--check-skills") < text.index("--queries") < text.index("--save-mappings") < text.index("--examples") < text.index("liverunner")


def test_dry_run_respects_stage_selection_and_shows_blocked_stage_5(rfv, tmp_path):
    code, _, _, out, _, _ = run_main(rfv, tmp_path, ["--dry-run", "--stages", "1,5"])
    text = "\n".join(out)
    assert "[stage 1]" in text and "[stage 5]" in text and "[stage 2]" not in text
    assert "BLOCKED: no live evaluation runner configured" in text


# ---------------------------------------------------------------------------------------------------------
# exit codes
# ---------------------------------------------------------------------------------------------------------

def _res(rfv, *statuses):
    return [rfv.StageResult(i, f"s{i}", s) for i, s in enumerate(statuses)]


def test_overall_status_rules(rfv):
    assert rfv.overall_status(_res(rfv, "PASS", "PASS", "SKIPPED")) == ("PASS", 0)
    assert rfv.overall_status(_res(rfv, "PASS", "BLOCKED")) == ("INCONCLUSIVE", 3)
    assert rfv.overall_status(_res(rfv, "PASS", "INCONCLUSIVE")) == ("INCONCLUSIVE", 3)
    assert rfv.overall_status(_res(rfv, "BLOCKED", "FAIL", "PASS")) == ("FAIL", 1)
    assert rfv.overall_status(_res(rfv, "SKIPPED", "SKIPPED"))[1] == 0


def test_exit_codes_from_main(rfv, tmp_path):
    assert run_main(rfv, tmp_path / "a", ["--live-eval-cmd", LIVE_CMD])[0] == 0
    assert run_main(rfv, tmp_path / "b")[0] == 3                      # stage 5 blocked
    r = FakeRunner()
    r.behaviors["s4"] = lambda argv: r._result(argv, 1, "FAILED\n")
    assert run_main(rfv, tmp_path / "c", ["--live-eval-cmd", LIVE_CMD], runner=r)[0] == 1


# ---------------------------------------------------------------------------------------------------------
# secret redaction
# ---------------------------------------------------------------------------------------------------------

def test_redactor_masks_known_values_and_credential_patterns(rfv):
    red = rfv.Redactor([GEMINI_KEY, OS_PASSWORD])
    text = (f"key {GEMINI_KEY} pw={OS_PASSWORD} Authorization: Bearer abcdef123456 "
            "url http://bob:s3cret@host:9200/x GEMINI_API_KEY=zzzz1234 token: glsa_ABCDEFGH12345678 other AIzaSyABCDEFGHIJKLMNOPQRSTUV")
    out = red(text)
    for leaked in (GEMINI_KEY, OS_PASSWORD, "abcdef123456", "s3cret", "zzzz1234", "glsa_ABCDEFGH12345678", "AIzaSyABCDEFGHIJKLMNOPQRSTUV"):
        assert leaked not in out
    assert "http://[REDACTED]@host:9200/x" in out


def test_redactor_leaves_json_structure_valid(rfv):
    red = rfv.Redactor([OS_PASSWORD])
    doc = {"gemini_api_key_set": True, "n": 3, "note": f"password={OS_PASSWORD}", "list": [f"x {OS_PASSWORD}"]}
    clean = red.obj(doc)
    assert clean["gemini_api_key_set"] is True and clean["n"] == 3
    assert OS_PASSWORD not in json.dumps(clean)
    json.loads(json.dumps(clean))


def test_collect_secrets_reads_credential_env_vars_and_settings(rfv):
    found = rfv.collect_secrets(make_settings(), {"MY_API_KEY": "k-12345678", "PATH": "/usr/bin", "OPENSEARCH_AUTH_PASSWORD": "pw-abcdefgh"})
    assert {"k-12345678", "pw-abcdefgh", GEMINI_KEY, OS_PASSWORD} <= set(found) and "/usr/bin" not in found


def test_no_secret_reaches_any_file_or_the_terminal(rfv, tmp_path):
    leaky = FakeRunner()
    leaky.behaviors["s2"] = lambda argv: leaky._result(
        argv, 1, f"   [fabric_health] ok=False detail=auth failed password={OS_PASSWORD} key={GEMINI_KEY}\nFAILED: see above.\n",
        f"Traceback ... Authorization: Bearer supersecrettoken123 {GEMINI_KEY}")
    live_cmd = LIVE_CMD + " --api-key=cmdline-secret-value"
    environ = {"EXTRA_SERVICE_TOKEN": "env-token-value-9"}
    rec = good_record("Q-9", generated_opensearch_dsl={"note": f"contains {OS_PASSWORD}"}, request_error=f"failed with {GEMINI_KEY}")
    leaky.live_records = [rec]
    code, report, run_dir, out, _, _ = run_main(rfv, tmp_path, ["--live-eval-cmd", live_cmd], runner=leaky, environ=environ)
    blob = "\n".join(out)
    for p in run_dir.rglob("*"):
        if p.is_file():
            blob += "\n" + p.read_text(encoding="utf-8", errors="replace")
    for leaked in (GEMINI_KEY, OS_PASSWORD, "supersecrettoken123", "cmdline-secret-value", "env-token-value-9"):
        assert leaked not in blob, leaked
    assert "[REDACTED]" in blob
    json.loads((run_dir / "report.json").read_text(encoding="utf-8"))     # still valid JSON after redaction
    assert report["configuration"]["gemini_api_key_set"] is True and report["configuration"]["opensearch_auth_configured"] is True


def test_dry_run_redacts_the_live_command(rfv, tmp_path):
    code, _, _, out, _, _ = run_main(rfv, tmp_path, ["--dry-run", "--live-eval-cmd", LIVE_CMD + " --password=hunter2hunter2"])
    assert "hunter2hunter2" not in "\n".join(out)
