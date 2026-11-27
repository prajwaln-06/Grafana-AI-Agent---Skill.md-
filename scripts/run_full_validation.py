#!/usr/bin/env python3
"""
scripts/run_full_validation.py

Master runner for the seven-stage (0-6) validation workflow of the Grafana AI Agent's OpenSearch
integration. It is an ORCHESTRATION layer: every stage that already has a script runs that script
as a subprocess, unchanged, and the runner only records what happened.

  0  Preparation and prerequisite checks   env/config, dependencies, OpenSearch, agent server, (Gemini)
  1  Offline consistency                   smoke_test_opensearch.py --check-skills
  2  Live mapping verification             smoke_test_opensearch.py --queries --rows
  3  Mapping snapshots                     smoke_test_opensearch.py --save-mappings <run dir>/mappings
  4  Examples and analyses                 smoke_test_opensearch.py --examples --analyses
  5  Live question evaluation              an operator-supplied live runner (--live-eval-cmd); BLOCKED without one
  6  Findings and consolidated report      manual_test_report.py on stage 5 results + report.json / report.md

Stage 5 is never substituted. scripts/run_opensearch_30_suite.py is NOT a live Gemini evaluation (see
docs/VALIDATION_AUDIT.md) and is deliberately not wired in.

Statuses: PASS, FAIL, BLOCKED (a prerequisite failed, stage not run), SKIPPED (not selected or not
applicable), INCONCLUSIVE (ran, but the result cannot be called pass or fail).

Exit codes: 0 every selected stage PASS | 1 at least one FAIL | 3 no FAIL but something BLOCKED or
INCONCLUSIVE (the run proved less than it was asked to) | 2 usage error | 130 interrupted.

Usage:
    python scripts/run_full_validation.py                       # all stages
    python scripts/run_full_validation.py --stages 1-4          # a range
    python scripts/run_full_validation.py --stages 1,3          # a list
    python scripts/run_full_validation.py --dry-run             # print the plan, run nothing
    python scripts/run_full_validation.py --live-eval-cmd "python my_runner.py --out {results_file} --agent-url {agent_url}"

Reports go to <output-dir>/<UTC timestamp>/ (default artifacts/validation_runs/), never overwritten.
See docs/VALIDATION_RUNNER.md.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
DEFAULT_OUTPUT_BASE = REPO / "artifacts" / "validation_runs"

PASS, FAIL, BLOCKED, SKIPPED, INCONCLUSIVE = "PASS", "FAIL", "BLOCKED", "SKIPPED", "INCONCLUSIVE"
EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_INCOMPLETE, EXIT_INTERRUPTED = 0, 1, 2, 3, 130

STAGE_NAMES = {
    0: "Preparation and prerequisite checks",
    1: "Offline consistency",
    2: "Live mapping verification",
    3: "Mapping snapshots",
    4: "Examples and analyses",
    5: "Live question evaluation",
    6: "Findings and consolidated report",
}
# What each stage touches, shown in the plan and the report so nobody mistakes an offline check for a live one.
STAGE_USES = {
    0: "environment; HTTP probes of OpenSearch, agent server, optionally Prometheus/Grafana; Gemini only if stage 5 will run",
    1: "nothing external (offline)",
    2: "live OpenSearch (read-only); no Gemini",
    3: "live OpenSearch (read-only); no Gemini",
    4: "live OpenSearch (read-only); no Gemini",
    5: "live agent server + live Gemini + live OpenSearch, through the operator's runner",
    6: "nothing external (grades recorded stage 5 results)",
}
# Capabilities (established by stage 0) each stage needs. A missing one makes the stage BLOCKED.
REQUIRES = {
    1: ("config",),
    2: ("config", "opensearch"),
    3: ("config", "opensearch"),
    4: ("config", "opensearch"),
    5: ("config", "opensearch", "agent", "gemini", "live_runner"),
}


# --------------------------------------------------------------------------------------------------
# Secret redaction
# --------------------------------------------------------------------------------------------------

_SECRET_NAME = re.compile(r"(?i)(api[_-]?key|passw|token|secret|credential)")
_PATTERNS = [
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bglsa_[A-Za-z0-9_]{8,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{6,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)(://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
]
_KV = re.compile(r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|passw(?:or)?d|token|secret|authorization)[A-Za-z0-9_]*)"
                 r"(\"?\s*[=:]\s*)(?:(?:bearer|basic)\s+)?(\"[^\"]*\"|'[^']*'|\S+)")


class Redactor:
    """Masks known secret values (exact match) and credential-looking text. Applied to everything the
    runner prints or writes, including captured subprocess output."""

    def __init__(self, secrets: Sequence[str] = ()):
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def add(self, *secrets: str | None) -> None:
        self.__init__([*self._secrets, *[s for s in secrets if s]])

    def __call__(self, text: Any, *, kv: bool = True) -> str:
        """kv=False skips the 'name=value' masking (used on text that was already redacted piece by piece,
        such as the rendered Markdown, where it would also mask harmless lines like 'api_key_set: False')."""
        out = "" if text is None else str(text)
        for secret in self._secrets:
            out = out.replace(secret, "[REDACTED]")
        for pattern, repl in _PATTERNS:
            out = pattern.sub(repl, out)
        return _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out) if kv else out

    def obj(self, value: Any) -> Any:
        """Redact every string inside a JSON-like structure (keys and non-strings are left alone), so the
        document can be serialised afterwards without the masking corrupting it."""
        if isinstance(value, str):
            return self(value)
        if isinstance(value, dict):
            return {k: self.obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value


def collect_secrets(settings: Any = None, environ: dict[str, str] | None = None) -> list[str]:
    """Secret VALUES to mask: credential-named environment variables plus the credential settings."""
    found: list[str] = []
    for name, value in (environ if environ is not None else os.environ).items():
        if _SECRET_NAME.search(name) and value:
            found.append(value)
    for attr in ("gemini_api_key", "opensearch_auth_username", "opensearch_auth_password",
                 "grafana_service_account_token", "api_key"):
        value = getattr(settings, attr, None) if settings is not None else None
        if isinstance(value, str) and value:
            found.append(value)
    return found


# --------------------------------------------------------------------------------------------------
# Seams: subprocess runner, HTTP probes, settings loader (all injectable for tests)
# --------------------------------------------------------------------------------------------------

@dataclass
class CommandResult:
    argv: list[str]
    exit_code: int | None          # None: never produced one (timeout, could not start)
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    error: str | None = None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def run_command(argv: Sequence[str], *, timeout: float, cwd: Path | None = None,
                env: dict[str, str] | None = None) -> CommandResult:
    """Run one command, capturing stdout/stderr. Never raises: timeouts and start failures come back
    as a CommandResult with exit_code None."""
    child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **(env or {})}
    start = time.monotonic()
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, cwd=str(cwd or REPO), env=child_env, stdin=subprocess.DEVNULL)
        return CommandResult(list(argv), proc.returncode, proc.stdout or "", proc.stderr or "",
                             time.monotonic() - start)
    except subprocess.TimeoutExpired as exc:
        return CommandResult(list(argv), None, _as_text(exc.stdout), _as_text(exc.stderr),
                             time.monotonic() - start, timed_out=True, error=f"timed out after {timeout:g}s")
    except OSError as exc:
        return CommandResult(list(argv), None, "", "", time.monotonic() - start,
                             error=f"could not start: {type(exc).__name__}")


@dataclass
class ProbeResult:
    ok: bool
    status_code: int | None = None
    error: str | None = None


class Probes:
    """HTTP reachability probes. Only the exception TYPE is reported, never its message (which can
    carry a URL with credentials)."""

    def http_get(self, url: str, *, auth: tuple[str, str] | None = None, timeout: float = 5.0) -> ProbeResult:
        import requests
        try:
            resp = requests.get(url, auth=auth, timeout=timeout, allow_redirects=False)
        except requests.RequestException as exc:
            return ProbeResult(False, None, type(exc).__name__)
        return ProbeResult(200 <= resp.status_code < 300, resp.status_code)


def load_settings() -> tuple[Any, str | None]:
    """The app's own settings object (so URLs, ports and credentials come from the existing config
    system). Returns (settings, None) or (None, why-not). The reason lists missing field NAMES only:
    a pydantic error message would echo input values."""
    try:
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from app.config import Settings
        return Settings(_env_file=str(REPO / ".env")), None
    except Exception as exc:  # noqa: BLE001
        errors = getattr(exc, "errors", None)
        if callable(errors):
            try:
                fields = ", ".join(f"{'.'.join(str(p) for p in e['loc']) or '?'} ({e['type']})" for e in errors())
                return None, f"settings invalid: {fields}"
            except Exception:  # noqa: BLE001
                pass
        return None, f"settings could not be loaded: {type(exc).__name__}"


# --------------------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------------------

def _zero_counts() -> dict[str, int]:
    return {"passed": 0, "failed": 0, "blocked": 0, "skipped": 0, "inconclusive": 0}


@dataclass
class Check:
    name: str
    status: str                    # PASS, FAIL or SKIPPED
    detail: str = ""
    required: bool = True


@dataclass
class StageResult:
    stage: int
    name: str
    status: str = SKIPPED
    uses: str = ""
    reasons: list[str] = field(default_factory=list)
    started_at: str | None = None
    duration_s: float = 0.0
    commands: list[dict[str, Any]] = field(default_factory=list)   # argv, exit_code, timed_out, stdout/stderr paths
    counts: dict[str, int] | None = None                           # individual test counts, where available
    checks: list[Check] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    tests: list[dict[str, Any]] = field(default_factory=list)      # per-question rows (stage 5)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage, "name": self.name, "status": self.status, "uses": self.uses,
            "reasons": self.reasons, "started_at": self.started_at, "duration_s": round(self.duration_s, 2),
            "commands": self.commands, "counts": self.counts,
            "checks": [c.__dict__ for c in self.checks], "warnings": self.warnings,
            "artifacts": self.artifacts, "tests": self.tests,
        }


def overall_status(results: Sequence[StageResult]) -> tuple[str, int]:
    """FAIL beats everything; BLOCKED/INCONCLUSIVE mean the run proved less than asked; SKIPPED (stage
    not selected or not applicable) is neutral."""
    statuses = [r.status for r in results]
    if FAIL in statuses:
        return FAIL, EXIT_FAIL
    if BLOCKED in statuses or INCONCLUSIVE in statuses:
        return INCONCLUSIVE, EXIT_INCOMPLETE
    if PASS in statuses:
        return PASS, EXIT_PASS
    return SKIPPED, EXIT_PASS


def parse_stage_spec(spec: str) -> list[int]:
    """'all', '3', '1-4', '1,3,5-6' -> sorted unique stage numbers within 0..6."""
    spec = (spec or "all").strip().lower()
    if spec == "all":
        return list(range(7))
    chosen: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"invalid stage selection {part!r} (use e.g. 3, 1-4, 1,3,5-6 or all)")
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) is not None else lo
        if lo > hi:
            raise ValueError(f"invalid range {part!r}: start is after end")
        if lo < 0 or hi > 6:
            raise ValueError(f"stage numbers must be 0..6, got {part!r}")
        chosen.update(range(lo, hi + 1))
    return sorted(chosen)


_OK_LINE = re.compile(r"^\s*\[[^\]]+\]\s+ok=(True|False)\b", re.M)
_FAILURE_HINT = re.compile(r"(?i)PROBLEM|FAILED|ok=False|NOT in the live|NOT SAVED|unavailable|REGISTRY FAILED|Error")


def _counts_from_ok_lines(stdout: str) -> dict[str, int] | None:
    """Best-effort: the smoke script prints '[family ...] ok=True|False' per query/example. Parsed from
    its text output, so None when the script printed none."""
    found = _OK_LINE.findall(stdout)
    if not found:
        return None
    return {**_zero_counts(), "passed": found.count("True"), "failed": found.count("False")}


# --------------------------------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------------------------------

class ValidationRun:
    def __init__(self, *, stages: Sequence[int], run_dir: Path, runner: Callable[..., CommandResult] = run_command,
                 probes: Probes | None = None, settings_loader: Callable[[], tuple[Any, str | None]] = load_settings,
                 agent_url: str | None = None, start_server: bool = False, server_start_timeout: float = 60.0,
                 live_eval_cmd: str | None = None, live_eval_timeout: float = 3600.0, timeout: float = 600.0,
                 probe_timeout: float = 5.0, require_prometheus: bool = False,
                 popen: Callable[..., Any] = subprocess.Popen, out: Callable[[str], None] = print,
                 now: Callable[[], datetime] | None = None, environ: dict[str, str] | None = None):
        self.selected = sorted(set(stages))
        self.run_dir = run_dir
        self.runner = runner
        self.probes = probes or Probes()
        self.settings_loader = settings_loader
        self.agent_url_opt = agent_url
        self.start_server = start_server
        self.server_start_timeout = server_start_timeout
        self.live_eval_cmd = live_eval_cmd
        self.live_eval_timeout = live_eval_timeout
        self.timeout = timeout
        self.probe_timeout = probe_timeout
        self.require_prometheus = require_prometheus
        self.popen = popen
        self.out = out
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.environ = environ if environ is not None else os.environ

        self.redact = Redactor()
        self.settings: Any = None
        self.caps: dict[str, bool | None] = {}
        self.cap_reasons: dict[str, str] = {}
        self.agent_url: str | None = None
        self.results: dict[int, StageResult] = {}
        self._server_proc: Any = None
        self._server_log: Path | None = None
        self.config_summary: dict[str, Any] = {}

    # ---- plan (dry run) ------------------------------------------------------------------------

    def _results_file(self) -> Path:
        return self.run_dir / "live_eval" / "results.json"

    def _live_argv(self, agent_url: str | None = None) -> list[str] | None:
        cmd = self.live_eval_cmd
        if not cmd:
            return None
        subs = {"{results_file}": str(self._results_file()), "{output_dir}": str(self.run_dir),
                "{agent_url}": agent_url or self.agent_url or "<agent-url>", "{python}": sys.executable}
        for key, value in subs.items():
            cmd = cmd.replace(key, value)
        return shlex.split(cmd, posix=(os.name != "nt"))

    def _smoke(self, *flags: str) -> list[str]:
        return [sys.executable, str(SCRIPTS / "smoke_test_opensearch.py"), *flags]

    def plan(self) -> list[tuple[int, list[str]]]:
        """(stage, [human-readable command lines]) for every stage that would run."""
        run_stages = self._with_prereqs()
        plan: list[tuple[int, list[str]]] = []
        for n in run_stages:
            if n == 0:
                lines = ["(in-process) check python version, dependencies, scripts, settings",
                         "(in-process) GET OpenSearch URL (if stages 2-5 or the live runner need it)",
                         "(in-process) GET <agent>/readyz (only if the live runner is configured)",
                         "(in-process) GET Prometheus / Grafana (informational; gating only with --require-prometheus / alert creation enabled)"]
                if 5 in self.selected and self.live_eval_cmd:
                    lines.append(self._fmt([sys.executable, str(SCRIPTS / "smoke_test_gemini.py")]) + "   # one real Gemini call")
            elif n == 1:
                lines = [self._fmt(self._smoke("--check-skills"))]
            elif n == 2:
                lines = [self._fmt(self._smoke("--queries", "--rows"))]
            elif n == 3:
                lines = [self._fmt(self._smoke("--save-mappings", str(self.run_dir / "mappings")))]
            elif n == 4:
                lines = [self._fmt(self._smoke("--examples", "--analyses"))]
            elif n == 5:
                argv = self._live_argv()
                lines = [self._fmt(argv) if argv else
                         "BLOCKED: no live evaluation runner configured (--live-eval-cmd or VALIDATION_LIVE_EVAL_CMD); nothing is substituted"]
            else:
                lines = [self._fmt([sys.executable, str(SCRIPTS / "manual_test_report.py"), str(self._results_file()), "--strict"])
                         + "   # only if stage 5 produced results",
                         f"(in-process) write {self.run_dir / 'report.json'} and report.md"]
            plan.append((n, lines))
        return plan

    def _fmt(self, argv: Sequence[str]) -> str:
        return self.redact(" ".join(shlex.quote(a) if os.name != "nt" else (f'"{a}"' if " " in a else a) for a in argv))

    def _with_prereqs(self) -> list[int]:
        run = list(self.selected)
        if 0 not in run and any(n in run for n in range(1, 6)):
            run.insert(0, 0)
        return run

    # ---- execution -----------------------------------------------------------------------------

    def execute(self) -> list[StageResult]:
        try:
            to_run = self._with_prereqs()
            if to_run and to_run[0] == 0 and 0 not in self.selected:
                self.out("Note: stage 0 (prerequisite checks) added because the selected stages depend on it.")
            for n in range(7):
                if n not in to_run:
                    self.results[n] = StageResult(n, STAGE_NAMES[n], SKIPPED, STAGE_USES[n], ["not selected"])
            for n in to_run:
                self._run_stage(n, total=len(to_run))
        finally:
            self._stop_server()
        return [self.results[n] for n in range(7)]

    def _run_stage(self, n: int, total: int) -> None:
        res = StageResult(n, STAGE_NAMES[n], uses=STAGE_USES[n])
        self.results[n] = res
        self.out(f"[stage {n}] {res.name} ...")
        res.started_at = self.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        t0 = time.monotonic()
        blocked = self._unmet(n)
        try:
            if blocked:
                res.status, res.reasons = BLOCKED, blocked
            else:
                getattr(self, f"_stage{n}")(res)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 -- a bug in one stage must not stop the run
            res.status = FAIL
            res.reasons.append(f"runner error in stage {n}: {type(exc).__name__}")
            self._save_text(f"stage{n}_internal_error.txt", traceback.format_exc(), res)
        res.duration_s = time.monotonic() - t0
        extra = f" - {self.redact(res.reasons[0])[:160]}" if res.reasons and res.status != PASS else ""
        self.out(f"[stage {n}] {res.status} ({res.duration_s:.1f}s){extra}")

    def _unmet(self, n: int) -> list[str]:
        """Why stage n cannot run. A capability that was never probed (for example the agent server when no
        live runner is configured) is only mentioned when nothing more specific explains the block."""
        unmet = [cap for cap in REQUIRES.get(n, ()) if self.caps.get(cap) is not True]
        unmet.sort(key=lambda cap: cap != "live_runner")      # "no live runner" is the one reason that never goes away
        explained = [self.cap_reasons[cap] for cap in unmet if self.cap_reasons.get(cap)]
        if explained:
            return explained
        return [f"prerequisite '{cap}' was not established by stage 0" for cap in unmet]

    # ---- helpers -------------------------------------------------------------------------------

    def _save_text(self, rel: str, text: str, res: StageResult | None = None) -> str:
        path = self.run_dir / "stages" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.redact(text), encoding="utf-8")
        relpath = str(path.relative_to(self.run_dir)).replace("\\", "/")
        if res is not None:
            res.artifacts[rel] = relpath
        return relpath

    def _run(self, res: StageResult, label: str, argv: Sequence[str], timeout: float,
             env: dict[str, str] | None = None) -> CommandResult:
        cr = self.runner(list(argv), timeout=timeout, cwd=REPO, env=env)
        base = f"stage{res.stage}_{label}"
        out_p = self._save_text(f"{base}.stdout.txt", cr.stdout, res)
        err_p = self._save_text(f"{base}.stderr.txt", cr.stderr, res)
        res.commands.append({"label": label, "argv": [self.redact(a) for a in argv], "exit_code": cr.exit_code,
                             "timed_out": cr.timed_out, "error": cr.error, "duration_s": round(cr.duration_s, 2),
                             "stdout": out_p, "stderr": err_p})
        return cr

    def _failure_lines(self, cr: CommandResult, limit: int = 6) -> list[str]:
        lines = [ln.strip() for ln in (cr.stdout + "\n" + cr.stderr).splitlines() if _FAILURE_HINT.search(ln)]
        if not lines:
            lines = [ln.strip() for ln in cr.stderr.splitlines() if ln.strip()][-2:]
        return [self.redact(ln)[:240] for ln in lines[:limit]]

    def _script_stage(self, res: StageResult, label: str, argv: Sequence[str]) -> CommandResult:
        """Common handling for a stage that is exactly one existing script: exit code decides."""
        cr = self._run(res, label, argv, self.timeout)
        res.counts = _counts_from_ok_lines(cr.stdout)
        if cr.timed_out or cr.exit_code is None:
            res.status = FAIL
            res.reasons.append(cr.error or "no exit code")
        elif cr.exit_code == 0:
            res.status = PASS
            if "NOT SEEN" in cr.stdout:
                res.warnings.append("some projected fields were NOT SEEN in the sampled rows (unverified, not a failure)")
        else:
            res.status = FAIL
            res.reasons.append(f"exit code {cr.exit_code}")
            res.reasons.extend(self._failure_lines(cr))
        return cr

    # ---- stage 0 -------------------------------------------------------------------------------

    def _stage0(self, res: StageResult) -> None:
        sel = set(self.selected)
        # The agent server and Gemini are only checked when a live runner exists and will use them
        # (stage 5 selected), or when stage 0 was asked for on its own.
        live_cfg = bool(self.live_eval_cmd) and (5 in sel or sel == {0})
        needs_os = bool(sel & {0, 2, 3, 4}) or live_cfg
        checks = res.checks

        def add(name: str, status: str, detail: str = "", required: bool = True) -> Check:
            c = Check(name, status, self.redact(detail), required)
            checks.append(c)
            return c

        py_ok = sys.version_info >= (3, 10)
        add("python", PASS if py_ok else FAIL, f"{sys.version.split()[0]} (project supports 3.10+; Dockerfile uses 3.12)")
        missing = [m for m in ("requests", "pydantic_settings", "dotenv") if importlib.util.find_spec(m) is None]
        add("dependencies", FAIL if missing else PASS,
            ("missing: " + ", ".join(missing) + " (pip install -r requirements.txt)") if missing else "requests, pydantic-settings, python-dotenv importable")
        wanted = ["smoke_test_opensearch.py", "manual_test_report.py"] + (["smoke_test_gemini.py"] if live_cfg else [])
        absent = [s for s in wanted if not (SCRIPTS / s).is_file()]
        add("scripts", FAIL if absent else PASS, ("missing: " + ", ".join(absent)) if absent else "required scripts present")

        settings, why = self.settings_loader()
        self.settings = settings
        if settings is None:
            add("settings", FAIL, why or "settings unavailable (is GEMINI_API_KEY set? see .env.example)")
        else:
            add("settings", PASS, "loaded via app.config (GEMINI_API_KEY is set; value not shown)")
            self.redact.add(*collect_secrets(settings, self.environ))
        if settings is None:
            self.redact.add(*collect_secrets(None, self.environ))

        core_ok = all(c.status == PASS for c in checks)
        self.caps["config"] = core_ok
        if not core_ok:
            self.cap_reasons["config"] = "stage 0 config check failed: " + "; ".join(
                f"{c.name}: {c.detail}" for c in checks if c.status != PASS)

        os_ok = False
        if settings is None or not core_ok:
            if needs_os:
                add("opensearch", SKIPPED, "not probed: configuration unavailable")
                self.cap_reasons["opensearch"] = "OpenSearch not probed because the configuration check failed"
        elif needs_os:
            r = self.probes.http_get(str(settings.opensearch_url), auth=getattr(settings, "opensearch_auth", None),
                                     timeout=self.probe_timeout)
            os_ok = r.ok
            if r.ok:
                add("opensearch", PASS, f"reachable (HTTP {r.status_code}) at {self.redact(settings.opensearch_url)}")
            elif r.status_code in (401, 403):
                add("opensearch", FAIL, f"reachable but not authorised (HTTP {r.status_code}); check OPENSEARCH_AUTH_USERNAME/PASSWORD")
            elif r.status_code is not None:
                add("opensearch", FAIL, f"unexpected HTTP {r.status_code} from {self.redact(settings.opensearch_url)} (9200 is the REST port; 9600 is not)")
            else:
                add("opensearch", FAIL, f"not reachable at {self.redact(settings.opensearch_url)} ({r.error})")
            if not r.ok:
                self.cap_reasons["opensearch"] = "OpenSearch unreachable: " + checks[-1].detail
        else:
            add("opensearch", SKIPPED, "not needed by the selected stages", required=False)
        self.caps["opensearch"] = os_ok

        if settings is not None:
            self.config_summary = {
                "opensearch_url": self.redact(settings.opensearch_url),
                "opensearch_auth_configured": bool(getattr(settings, "opensearch_auth", None)),
                "prometheus_url": self.redact(getattr(settings, "prometheus_url", None)),
                "grafana_url": self.redact(getattr(settings, "grafana_url", None)),
                "alert_rule_creation_enabled": bool(getattr(settings, "alert_rule_creation_enabled", False)),
                "gemini_model": getattr(settings, "gemini_model", None),
                "gemini_api_key_set": bool(getattr(settings, "gemini_api_key", None)),
                "agent_api_key_set": bool(getattr(settings, "api_key", None)),
            }
            self._stage0_optional_services(settings, add)
        self._stage0_agent_and_gemini(res, settings, live_cfg, add)

        failed = [c for c in checks if c.required and c.status == FAIL]
        res.status = FAIL if failed else PASS
        for c in failed:
            res.reasons.append(f"{c.name}: {c.detail}")

    def _stage0_optional_services(self, settings: Any, add: Callable[..., Check]) -> None:
        prom = self.probes.http_get(str(settings.prometheus_url).rstrip("/") + "/-/ready", timeout=self.probe_timeout)
        add("prometheus", PASS if prom.ok else FAIL,
            f"reachable (HTTP {prom.status_code})" if prom.ok else f"not reachable ({prom.status_code or prom.error}); not needed by any OpenSearch stage",
            required=self.require_prometheus)
        if getattr(settings, "alert_rule_creation_enabled", False):
            g = self.probes.http_get(str(settings.grafana_url).rstrip("/") + "/api/health", timeout=self.probe_timeout)
            add("grafana", PASS if g.ok else FAIL,
                f"reachable (HTTP {g.status_code})" if g.ok else f"not reachable ({g.status_code or g.error}); ALERT_RULE_CREATION_ENABLED is true")
        else:
            add("grafana", SKIPPED, "ALERT_RULE_CREATION_ENABLED is false", required=False)

    def _stage0_agent_and_gemini(self, res: StageResult, settings: Any, live_cfg: bool, add: Callable[..., Check]) -> None:
        if 5 in self.selected and not self.live_eval_cmd:
            self.caps["live_runner"] = False
            self.cap_reasons["live_runner"] = ("no live evaluation runner configured: pass --live-eval-cmd or set "
                                               "VALIDATION_LIVE_EVAL_CMD (see docs/VALIDATION_RUNNER.md). "
                                               "run_opensearch_30_suite.py is not a live Gemini evaluation and is not substituted")
        else:
            self.caps["live_runner"] = bool(self.live_eval_cmd)
        if not live_cfg:
            add("agent_server", SKIPPED, "not needed: no live runner configured or stage 5 not selected", required=False)
            add("gemini", SKIPPED, "not needed: no live runner configured or stage 5 not selected", required=False)
            return
        if settings is None:
            self.cap_reasons["agent"] = self.cap_reasons["gemini"] = "configuration unavailable"
            add("agent_server", SKIPPED, "not probed: configuration unavailable")
            add("gemini", SKIPPED, "not probed: configuration unavailable")
            return

        self.agent_url = (self.agent_url_opt or self.environ.get("AGENT_URL")
                          or f"http://localhost:{getattr(settings, 'api_port', 8000)}").rstrip("/")
        probe = self.probes.http_get(self.agent_url + "/readyz", timeout=self.probe_timeout)
        if probe.ok:
            add("agent_server", PASS, f"already running at {self.redact(self.agent_url)} (reused, will not be stopped)")
            self.caps["agent"] = True
        elif self.start_server:
            started, detail = self._start_server()
            add("agent_server", PASS if started else FAIL, detail)
            self.caps["agent"] = started
        else:
            add("agent_server", FAIL, f"not reachable at {self.redact(self.agent_url)} ({probe.status_code or probe.error}); "
                                      "start it with 'python run_server.py' or pass --start-server")
            self.caps["agent"] = False
        if not self.caps["agent"]:
            self.cap_reasons["agent"] = "agent server unavailable: " + res.checks[-1].detail

        key = getattr(settings, "gemini_api_key", "") or ""
        if not key or key.startswith("replace-with"):
            add("gemini", FAIL, "GEMINI_API_KEY is empty or still the .env.example placeholder")
            self.caps["gemini"] = False
        else:
            cr = self._run(res, "gemini_smoke", [sys.executable, str(SCRIPTS / "smoke_test_gemini.py")], min(self.timeout, 180.0))
            ok = cr.exit_code == 0 and not cr.timed_out
            add("gemini", PASS if ok else FAIL,
                "smoke_test_gemini.py: one real Gemini call returned valid JSON" if ok else
                "smoke_test_gemini.py failed: " + ("; ".join(self._failure_lines(cr, 2)) or cr.error or f"exit {cr.exit_code}"))
            self.caps["gemini"] = ok
        if not self.caps["gemini"]:
            self.cap_reasons["gemini"] = "Gemini unavailable: " + res.checks[-1].detail

    def _start_server(self) -> tuple[bool, str]:
        """Start run_server.py (the documented procedure) and wait for /readyz. Only a server started here
        is ever stopped by this runner."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._server_log = self.run_dir / "stages" / "agent_server.log"
        self._server_log.parent.mkdir(parents=True, exist_ok=True)
        try:
            log = open(self._server_log, "wb")
            self._server_proc = self.popen([sys.executable, str(REPO / "run_server.py")], cwd=str(REPO),
                                           stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except OSError as exc:
            return False, f"could not start run_server.py: {type(exc).__name__}"
        deadline = time.monotonic() + self.server_start_timeout
        while time.monotonic() < deadline:
            if self._server_proc.poll() is not None:
                return False, f"run_server.py exited with code {self._server_proc.poll()} (see stages/agent_server.log)"
            if self.probes.http_get(self.agent_url + "/readyz", timeout=self.probe_timeout).ok:
                return True, f"started run_server.py (pid {getattr(self._server_proc, 'pid', '?')}); will be stopped at the end"
            time.sleep(1.0)
        return False, f"run_server.py did not answer /readyz within {self.server_start_timeout:g}s"

    def _stop_server(self) -> None:
        proc = self._server_proc
        self._server_proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    proc.kill()
        except Exception:  # noqa: BLE001
            pass
        if self._server_log and self._server_log.is_file():
            self._server_log.write_text(self.redact(self._server_log.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")

    # ---- stages 1-4 ----------------------------------------------------------------------------

    def _stage1(self, res: StageResult) -> None:
        cr = self._script_stage(res, "check_skills", self._smoke("--check-skills"))
        problems = len(re.findall(r"^\s*PROBLEM:", cr.stdout, re.M))
        unverified = len(re.findall(r"still unverified:", cr.stdout))
        res.counts = {**_zero_counts(), "failed": problems, "passed": 0 if problems else 1}
        if unverified:
            res.warnings.append(f"{unverified} registry fact(s) still unverified (informational; confirmed by stages 2-4 and the mentor)")

    def _stage2(self, res: StageResult) -> None:
        cr = self._script_stage(res, "queries_rows", self._smoke("--queries", "--rows"))
        unusable = len(re.findall(r"usable=False", cr.stdout))
        if unusable:
            res.warnings.append(f"{unusable} family/families reported usable=False (mapping-related failure; see stdout)")
        missing = [ln.strip() for ln in cr.stdout.splitlines() if "NOT in the live mapping" in ln or "role bindings dropped" in ln]
        res.warnings.extend(self.redact(m)[:240] for m in missing)

    def _stage3(self, res: StageResult) -> None:
        target = self.run_dir / "mappings"
        cr = self._script_stage(res, "save_mappings", self._smoke("--save-mappings", str(target)))
        saved = sorted(p.name for p in target.glob("mapping_*.json")) if target.is_dir() else []
        res.artifacts["mappings_dir"] = "mappings"
        res.counts = {**_zero_counts(), "passed": len(saved)}
        if not saved:
            # The script's exit code alone does not prove a snapshot exists.
            res.status = FAIL
            res.reasons.append("no mapping_*.json snapshot was written")
        else:
            res.warnings.append("snapshots (field names and types only): " + ", ".join(saved))

    def _stage4(self, res: StageResult) -> None:
        self._script_stage(res, "examples_analyses", self._smoke("--examples", "--analyses"))

    # ---- stage 5 -------------------------------------------------------------------------------

    def _stage5(self, res: StageResult) -> None:
        argv = self._live_argv()
        results_file = self._results_file()
        results_file.parent.mkdir(parents=True, exist_ok=True)
        env = {"VALIDATION_AGENT_URL": self.agent_url or "", "VALIDATION_RESULTS_FILE": str(results_file),
               "VALIDATION_OUTPUT_DIR": str(self.run_dir)}
        cr = self._run(res, "live_eval", argv or [], self.live_eval_timeout, env=env)
        run_failed = cr.timed_out or cr.exit_code is None or cr.exit_code != 0
        if cr.timed_out or cr.exit_code is None:
            res.reasons.append(cr.error or "live runner produced no exit code")
        elif cr.exit_code != 0:
            res.reasons.append(f"live runner exited with code {cr.exit_code}")
            res.reasons.extend(self._failure_lines(cr, 3))

        data, why = self._load_results(results_file)
        if data is None:
            res.status = FAIL
            res.reasons.append(why)
            return
        res.artifacts["results"] = str(results_file.relative_to(self.run_dir)).replace("\\", "/")
        # The runner's file is written verbatim by operator code; mask any secret in it (values only) so the
        # run directory never holds one, then grade the masked copy.
        data = self.redact.obj(data)
        results_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        graded = self._grade(data)
        res.tests = graded
        counts = _zero_counts()
        for t in graded:
            counts[{PASS: "passed", FAIL: "failed", INCONCLUSIVE: "inconclusive"}[t["status"]]] += 1
        res.counts = counts
        if counts["failed"] or run_failed:
            res.status = FAIL
            if counts["failed"]:
                res.reasons.append(f"{counts['failed']} of {len(graded)} question(s) failed grading")
        elif counts["inconclusive"]:
            res.status = INCONCLUSIVE
            res.reasons.append(f"{counts['inconclusive']} question(s) returned an empty result that needs human review")
        else:
            res.status = PASS

    @staticmethod
    def _load_results(path: Path) -> tuple[dict | None, str]:
        if not path.is_file():
            return None, f"the live runner wrote no results file at {path.name}; nothing can be graded"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, "the live runner's results file is not valid JSON"
        if not isinstance(data, dict) or not isinstance(data.get("results"), list) or not data["results"]:
            return None, "the live runner's results file has no 'results' list (expected the test_output_all shape)"
        return data, ""

    def _grade(self, data: dict) -> list[dict[str, Any]]:
        """Per-question verdicts from manual_test_report.grade (reused, not reimplemented). A 200 alone is
        never a pass: status and semantics must also hold. An empty result is INCONCLUSIVE."""
        spec = importlib.util.spec_from_file_location("manual_test_report", SCRIPTS / "manual_test_report.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("manual_test_report", mod)
        spec.loader.exec_module(mod)
        rows = []
        for rec in data["results"]:
            if not isinstance(rec, dict):
                continue
            g = mod.grade(rec)
            if not (g["http_ok"] and g["status_ok"] and g["semantic_ok"]):
                status = FAIL
            elif g["needs_review"]:
                status = INCONCLUSIVE
            else:
                status = PASS
            dsl = rec.get("generated_opensearch_dsl")
            rows.append({
                "id": g["id"], "question": self.redact(g["question"]), "status": status,
                "http_status": rec.get("http_status"), "application_status": g["application_status"],
                "execution_status": g["execution_statuses"], "elapsed_seconds": rec.get("elapsed_seconds"),
                "expected": self.redact(g["expectation_note"]) or "application status 'ok' with an executed query",
                "actual": self.redact("; ".join(g["problems"]) or f"status {g['application_status']}, execution {g['execution_statuses']}"),
                "generated_opensearch_dsl": self.redact.obj(dsl),
                "error": self.redact(rec.get("request_error") or rec.get("response_parse_error") or "") or None,
            })
        return rows

    # ---- stage 6 -------------------------------------------------------------------------------

    def _stage6(self, res: StageResult) -> None:
        r5 = self.results.get(5)
        results_file = self._results_file()
        if r5 is not None and r5.artifacts.get("results") and results_file.is_file():
            cr = self._run(res, "manual_test_report", [sys.executable, str(SCRIPTS / "manual_test_report.py"),
                                                       str(results_file), "--strict"], 120.0)
            if cr.exit_code not in (0, 1):
                res.status = FAIL
                res.reasons.append(cr.error or f"manual_test_report.py exited with {cr.exit_code}")
                return
            res.warnings.append("manual_test_report.py --strict exit code "
                                f"{cr.exit_code} (graded results are counted in stage 5; this stage reports them)")
        else:
            res.warnings.append("no stage 5 results to grade (stage 5 did not produce a results file)")
        res.status = PASS   # downgraded by finalize_report() if the report files cannot be written

    # ---- report --------------------------------------------------------------------------------

    def build_report(self, *, run_id: str, started: datetime, finished: datetime, git: dict[str, Any]) -> dict[str, Any]:
        results = [self.results[n] for n in range(7)]
        status, code = overall_status(results)
        stage_counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, FAIL, BLOCKED, SKIPPED, INCONCLUSIVE)}
        test_totals = _zero_counts()
        for r in results:
            for k, v in (r.counts or {}).items():
                test_totals[k] += v
        not_validated = [f"stage {r.stage} ({r.name}): {r.status}" for r in results if r.status in (BLOCKED, SKIPPED, INCONCLUSIVE, FAIL)]
        return {
            "run_id": run_id,
            "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s": round((finished - started).total_seconds(), 2),
            "git": git,
            "python": sys.version.split()[0],
            "configuration": self.config_summary,
            "live_eval_configured": bool(self.live_eval_cmd),
            "selected_stages": self.selected,
            "complete_run": self.selected == list(range(7)),
            "overall": {"status": status, "exit_code": code, "stage_counts": stage_counts,
                        "individual_test_counts": test_totals, "not_fully_validated": not_validated},
            "stages": [r.to_dict() for r in results],
            "artifacts": {"run_dir": str(self.run_dir), "report_json": "report.json", "report_md": "report.md"},
        }


def render_markdown(report: dict[str, Any]) -> str:
    o = report["overall"]
    git = report["git"]
    lines = [
        f"# Validation run {report['run_id']}", "",
        f"**Overall: {o['status']}** (exit code {o['exit_code']})"
        + ("" if report["complete_run"] else f" - PARTIAL run, stages {report['selected_stages']} only"), "",
        f"- Started {report['started_at']}, finished {report['finished_at']} ({report['duration_s']} s)",
        f"- Git: branch `{git.get('branch')}`, commit `{git.get('commit')}`" + (" (working tree has uncommitted changes)" if git.get("dirty") else ""),
        f"- Python {report['python']}",
        f"- Live evaluation runner configured: {'yes' if report['live_eval_configured'] else 'NO'}", "",
        "## Configuration (no secrets)", "",
    ]
    cfg = report.get("configuration") or {}
    lines += [f"- {k}: `{v}`" for k, v in cfg.items()] or ["- (configuration could not be loaded)"]
    lines += ["", "## Stages", "", "| # | Stage | Status | Duration | Tests (pass/fail/inconclusive) |", "|---|---|---|---|---|"]
    for s in report["stages"]:
        c = s["counts"]
        tests = f"{c['passed']}/{c['failed']}/{c['inconclusive']}" if c else "n/a"
        lines.append(f"| {s['stage']} | {s['name']} | {s['status']} | {s['duration_s']} s | {tests} |")
    sc = o["stage_counts"]
    tc = o["individual_test_counts"]
    lines += ["", f"Stage counts: {sc[PASS]} passed, {sc[FAIL]} failed, {sc[BLOCKED]} blocked, {sc[SKIPPED]} skipped, {sc[INCONCLUSIVE]} inconclusive.",
              f"Individual tests (where a stage reports them): {tc['passed']} passed, {tc['failed']} failed, {tc['inconclusive']} inconclusive.", ""]
    if o["not_fully_validated"]:
        lines += ["## Not validated or not passing", ""] + [f"- {x}" for x in o["not_fully_validated"]] + [""]
    lines += ["## Stage details", ""]
    for s in report["stages"]:
        lines += [f"### Stage {s['stage']} - {s['name']}: {s['status']}", "", f"Uses: {s['uses']}", ""]
        lines += [f"- {r}" for r in s["reasons"]]
        lines += [f"- warning: {w}" for w in s["warnings"]]
        for c in s["checks"]:
            lines.append(f"- check `{c['name']}`: {c['status']}" + (f" - {c['detail']}" if c["detail"] else "") + ("" if c["required"] else " (informational)"))
        for cmd in s["commands"]:
            lines.append(f"- command `{' '.join(cmd['argv'])}` -> exit {cmd['exit_code']}" + (" (TIMED OUT)" if cmd["timed_out"] else "")
                         + f"; output: `{cmd['stdout']}`, `{cmd['stderr']}`")
        bad = [t for t in s["tests"] if t["status"] != PASS]
        if s["tests"]:
            lines.append(f"- {len(s['tests'])} question(s) graded; {len(bad)} not passing")
        for t in bad:
            lines.append(f"  - **{t['id']}** {t['status']}: {t['question']} | expected: {t['expected']} | actual: {t['actual']}")
        lines.append("")
    lines += ["## Artifacts", "", f"- Run directory: `{report['artifacts']['run_dir']}`", "- Per-command output: `stages/`",
              "- Mapping snapshots: `mappings/` (field names and types only)", "- Live evaluation results: `live_eval/results.json`", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------

def make_run_dir(base: Path, now: datetime) -> tuple[str, Path]:
    """A new, empty timestamped directory. An existing one is never reused (suffix -1, -2, ...)."""
    base.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    for i in range(1000):
        name = stamp if i == 0 else f"{stamp}-{i}"
        try:
            (base / name).mkdir()
            return name, base / name
        except FileExistsError:
            continue
    raise RuntimeError("could not create a unique run directory")


def git_info(runner: Callable[..., CommandResult]) -> dict[str, Any]:
    def one(*args: str) -> str | None:
        cr = runner(["git", *args], timeout=15, cwd=REPO, env=None)
        return cr.stdout.strip() if cr.exit_code == 0 and cr.stdout.strip() else None
    status = one("status", "--porcelain")
    return {"branch": one("rev-parse", "--abbrev-ref", "HEAD"), "commit": one("rev-parse", "HEAD"), "dirty": bool(status)}


def build_parser(environ: dict[str, str] | None = None) -> argparse.ArgumentParser:
    env = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(description="Run the seven-stage (0-6) OpenSearch validation workflow and write one consolidated report.",
                                 epilog="Exit codes: 0 all selected stages PASS; 1 a stage FAILED; 2 usage error; "
                                        "3 no failure but a stage was BLOCKED or INCONCLUSIVE; 130 interrupted.")
    ap.add_argument("--stages", default="all", metavar="SPEC", help="all (default), N, N-M or a comma list such as 1,3,5-6")
    ap.add_argument("--output-dir", metavar="DIR", default=None,
                    help=f"base directory; the run writes to DIR/<UTC timestamp>/ (default {DEFAULT_OUTPUT_BASE.relative_to(REPO)})")
    ap.add_argument("--dry-run", action="store_true", help="print the commands that would run; execute and write nothing")
    ap.add_argument("--agent-url", default=None, help="agent server base URL (default: $AGENT_URL, else http://localhost:<API_PORT>)")
    ap.add_argument("--start-server", action="store_true",
                    help="if no server answers at the agent URL, start 'python run_server.py' and stop it afterwards; "
                         "an already-running server is always reused and never stopped")
    ap.add_argument("--server-start-timeout", type=float, default=60.0, metavar="SEC")
    ap.add_argument("--live-eval-cmd", default=env.get("VALIDATION_LIVE_EVAL_CMD"), metavar="CMD",
                    help="stage 5 integration point: a command that asks the live agent the questions and writes a "
                         "test_output_all-shaped JSON file. Placeholders: {results_file} {agent_url} {output_dir} {python}. "
                         "Default: $VALIDATION_LIVE_EVAL_CMD. Without it stage 5 is BLOCKED.")
    ap.add_argument("--live-eval-timeout", type=float, default=3600.0, metavar="SEC")
    ap.add_argument("--timeout", type=float, default=600.0, metavar="SEC", help="per-command timeout for stages 1-4 (default 600)")
    ap.add_argument("--probe-timeout", type=float, default=5.0, metavar="SEC")
    ap.add_argument("--require-prometheus", action="store_true", help="make an unreachable Prometheus fail stage 0")
    return ap


def main(argv: Sequence[str] | None = None, *, runner: Callable[..., CommandResult] = run_command,
         probes: Probes | None = None, settings_loader: Callable[[], tuple[Any, str | None]] = load_settings,
         popen: Callable[..., Any] = subprocess.Popen, now: Callable[[], datetime] | None = None,
         out: Callable[[str], None] = print, environ: dict[str, str] | None = None) -> int:
    ap = build_parser(environ)
    args = ap.parse_args(argv)
    try:
        stages = parse_stage_spec(args.stages)
    except ValueError as exc:
        ap.error(str(exc))
    clock = now or (lambda: datetime.now(timezone.utc))
    base = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_BASE
    kwargs = dict(runner=runner, probes=probes, settings_loader=settings_loader, agent_url=args.agent_url,
                  start_server=args.start_server, server_start_timeout=args.server_start_timeout,
                  live_eval_cmd=args.live_eval_cmd, live_eval_timeout=args.live_eval_timeout, timeout=args.timeout,
                  probe_timeout=args.probe_timeout, require_prometheus=args.require_prometheus, popen=popen,
                  out=out, now=clock, environ=environ)

    if args.dry_run:
        planned = base / (clock().strftime("%Y%m%dT%H%M%SZ"))
        run = ValidationRun(stages=stages, run_dir=planned, **kwargs)
        run.redact.add(*collect_secrets(None, environ))
        out(f"DRY RUN: nothing will be executed or written. Planned run directory: {planned}")
        for n, lines in run.plan():
            out(f"[stage {n}] {STAGE_NAMES[n]}")
            out(f"    uses: {STAGE_USES[n]}")
            for ln in lines:
                out(f"    {ln}")
        return EXIT_PASS

    started = clock()
    run_id, run_dir = make_run_dir(base, started)
    run = ValidationRun(stages=stages, run_dir=run_dir, **kwargs)
    run.redact.add(*collect_secrets(None, environ))
    out(f"Validation run {run_id}; stages {stages}; output: {run_dir}")
    interrupted = False
    try:
        run.execute()
    except KeyboardInterrupt:
        interrupted = True
        out("Interrupted: remaining stages are marked SKIPPED and the report is still written.")
        run._stop_server()
        for n in range(7):
            run.results.setdefault(n, StageResult(n, STAGE_NAMES[n], SKIPPED, STAGE_USES[n], ["interrupted before it started"]))

    git = git_info(runner)
    report_error = None
    report = run.build_report(run_id=run_id, started=started, finished=clock(), git=git)
    try:
        report = run.redact.obj(report)
        (run_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (run_dir / "report.md").write_text(run.redact(render_markdown(report), kv=False), encoding="utf-8")
    except OSError as exc:
        report_error = type(exc).__name__
        if 6 in stages:
            run.results[6].status = FAIL
            run.results[6].reasons.append(f"could not write the report: {report_error}")
            report = run.build_report(run_id=run_id, started=started, finished=clock(), git=git)

    o = report["overall"]
    out("")
    out("=" * 78)
    for s in report["stages"]:
        out(f"  stage {s['stage']}  {s['status']:<12} {s['name']}")
        if s["status"] in (FAIL, BLOCKED, INCONCLUSIVE) and s["reasons"]:
            out(f"             {run.redact(s['reasons'][0])[:150]}")
    out("=" * 78)
    out(f"OVERALL: {o['status']}" + ("" if report["complete_run"] else f" (partial run: stages {stages})"))
    out("Not fully validated: " + ("; ".join(o["not_fully_validated"]) if o["not_fully_validated"] else "nothing"))
    out(f"Report: {run_dir / 'report.md'}" + (f"  (WRITE FAILED: {report_error})" if report_error else ""))
    if interrupted:
        return EXIT_INTERRUPTED
    return o["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
