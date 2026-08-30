"""Grafana Alert Rule management — propose, review, approve, execute.

Pipeline:
    User natural language
      → parse_alert_request()      (regex-based, deterministic)
      → propose_alert_rule()       (assembles AlertRuleIR, stores in ALERT_PROPOSALS)
      → [human reviews via API]
      → execute_approved_alert()   (compiles IR → Grafana JSON, calls MCP)
      → Grafana alert rule is live

No LLM is involved between parsing and MCP write; all logic is deterministic.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .datasource import resolve_datasource
from .prometheus import list_prometheus_label_names, list_prometheus_metric_metadata, list_prometheus_metric_names
from .promql import build_promql, infer_unit, TARGET_LABEL_CANDIDATES
from app.mcp.session import mcp_call, run_sync
from .utils import logger

# ---------------------------------------------------------------------------
# Natural-language alert request parser
# ---------------------------------------------------------------------------

def _extract_metric_term(lower: str) -> str:
    """Extract a free-form metric search term from a lower-cased alert request.

    Looks for a noun phrase immediately before the threshold condition.
    Falls back to any capitalised/underscore token if nothing is found.
    Returns an empty string if no candidate is found (caller raises).
    """
    # Match: "when <term> <operator>" or "if <term> <operator>"
    m = re.search(
        r'\b(?:when|if|alert\s+on)\s+([\w][\w\s]*?)\s+'
        r'(?:is\s+)?'
        r'(?:above|below|exceeds?|greater|less|higher|lower|over|under|[><]=?)',
        lower,
    )
    if m:
        return m.group(1).strip()
    # Fallback: grab first word-sequence before a numeric threshold
    m = re.search(r'\b([a-z][a-z0-9_ ]{1,30}?)\s+[><=]+?\s*\d', lower)
    if m:
        return m.group(1).strip()
    return ""


def _find_metric_in_schema(term: str, discovered: list[str]) -> str:
    """Return the best matching Prometheus metric for a free-form search term.

    Resolution order:
    1. Exact match on the term itself (user typed the real metric name).
    2. Any discovered metric whose name *contains* every significant token.
    3. Any discovered metric that contains any token (shortest name wins).

    Raises ValueError with a suggestion list if nothing matches.
    """
    normalised = term.lower().replace(" ", "_").replace("-", "_")

    # 1. Exact
    if normalised in discovered:
        return normalised
    if term in discovered:
        return term

    tokens = [t for t in re.split(r'[^a-z0-9]+', normalised) if len(t) > 1]

    # 2. All-token match
    all_token = sorted(
        [m for m in discovered if all(tok in m for tok in tokens)],
        key=len,
    )
    if all_token:
        return all_token[0]

    # 3. Any-token match
    any_token = sorted(
        [m for m in discovered if any(tok in m for tok in tokens)],
        key=len,
    )
    if any_token:
        suggestions = any_token[:10]
        raise ValueError(
            f"Clarification required: '{term}' matches multiple metrics. "
            f"Please specify one: {', '.join(suggestions)}."
        )

    raise ValueError(
        f"No Prometheus metric matching '{term}' was found. "
        f"Use 'list metrics' or check your Prometheus instance for available metric names."
    )


def parse_alert_request(request: str) -> dict[str, Any]:
    """Parse a natural-language alert request into structured parameters.

    Returns a dict with keys:
        metric_term, condition, threshold, for_duration, severity, target

    ``metric_term`` is a free-form search string resolved against the live
    Prometheus schema in ``_propose_alert_rule_async``.
    Raises ValueError for unresolvable inputs.
    """
    text = request.strip()
    lower = text.lower()

    # --- Metric search term ---
    metric_term = _extract_metric_term(lower)
    if not metric_term:
        raise ValueError(
            f"Could not identify a metric in: '{request}'. "
            "Try: 'alert when node_cpu_seconds_total > 0.9 for 5 minutes'."
        )

    # --- Operator / condition ---
    condition = "gt"  # sensible default
    for alias, op in sorted(_CONDITION_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(alias)}\b", lower):
            condition = op
            break

    # --- Threshold ---
    threshold: float | None = None
    threshold_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(%|percent|gb|mb|kb|bps|°c|c|celsius)?",
        lower,
    )
    if threshold_match:
        raw_val = float(threshold_match.group(1))
        unit_hint = (threshold_match.group(2) or "").strip()
        if unit_hint in ("gb",):
            raw_val = raw_val * 1e9
        elif unit_hint in ("mb",):
            raw_val = raw_val * 1e6
        elif unit_hint in ("kb",):
            raw_val = raw_val * 1e3
        threshold = raw_val
    if threshold is None:
        raise ValueError(
            f"Could not find a threshold value in: '{request}'. "
            "Please specify a number, e.g. '90%', '8GB', '75'."
        )

    # --- For duration ---
    for_duration = "5m"  # default: fire after 5 minutes
    duration_match = re.search(
        r"for\s+(?:more\s+than\s+|at\s+least\s+)?(\d+(?:\.\d+)?)\s*(second|seconds|sec|s|minute|minutes|min|m|hour|hours|hr|h|day|days|d)",
        lower,
    )
    if duration_match:
        val = float(duration_match.group(1))
        unit = duration_match.group(2).lower()
        if unit in ("second", "seconds", "sec", "s"):
            for_duration = f"{int(val)}s"
        elif unit in ("minute", "minutes", "min", "m"):
            for_duration = f"{int(val)}m"
        elif unit in ("hour", "hours", "hr", "h"):
            for_duration = f"{int(val)}h"
        elif unit in ("day", "days", "d"):
            for_duration = f"{int(val * 24)}h"

    # --- Severity ---
    severity = "critical"  # default
    for alias, sev in sorted(_SEVERITY_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(alias)}\b", lower):
            severity = sev
            break

    # --- Target (optional host/node filter) ---
    target: str | None = None
    # Determine the span of the duration clause so we don't accidentally
    # match "for 1 hour" and capture "1" as a node name.
    duration_span = duration_match.span() if duration_match else (len(lower), len(lower))
    target_patterns = [
        r"(?:on|targeting?|instance)\s+([A-Za-z0-9_.-]+(?:-\d+)?)",
        r"host\s+([A-Za-z0-9_.-]+(?:-\d+)?)",
        r"node\s+([A-Za-z0-9_.-]+(?:-\d+)?)",
        # "for <target>" only when not inside the duration clause
        r"for\s+([A-Za-z][A-Za-z0-9_.-]*(?:-\d+)?)",
    ]
    _STOP_WORDS = {
        "more", "than", "least", "all", "any", "cpu", "memory",
        "disk", "network", "gpu", "this", "the", "each", "every",
        "critical", "warning", "warn", "info",
    }
    for pat in target_patterns:
        m = re.search(pat, lower)
        if not m:
            continue
        # Skip if the match overlaps with the duration clause
        if m.start() >= duration_span[0] and m.start() < duration_span[1]:
            continue
        candidate = m.group(1).strip()
        # Reject pure-numeric values (those come from durations like "1 hour")
        if re.fullmatch(r'\d+(\.\d+)?', candidate):
            continue
        if candidate.lower() not in _STOP_WORDS:
            target = candidate
            break

    # --- Title ---
    title_match = re.search(r'(?:named?|called|title)\s+["\']?([A-Za-z0-9 _-]+)["\']?', text, re.I)
    title = title_match.group(1).strip() if title_match else f"High {metric_term.title()}"

    return {
        "metric_term": metric_term,
        "condition": condition,
        "threshold": threshold,
        "for_duration": for_duration,
        "severity": severity,
        "target": target,
        "title": title,
    }


def resolve_alert_intent(request: str) -> dict:
    """Classify a request as CREATE, LIST, UPDATE, or DELETE for alert rules."""
    lower = request.lower()
    # Delete / remove
    if any(k in lower for k in ("delete", "remove", "disable", "silence")):
        return {"intent": "DELETE", "confidence": 0.9}
    # List / show
    if any(k in lower for k in ("list", "show", "get", "what alerts", "all alerts", "existing alerts")):
        return {"intent": "LIST", "confidence": 0.9}
    # Update / modify
    if any(k in lower for k in ("update", "modify", "change", "edit", "adjust")):
        return {"intent": "UPDATE", "confidence": 0.85}
    # Create (default for "alert when ..." phrasing)
    return {"intent": "CREATE", "confidence": 0.8}


# ---------------------------------------------------------------------------
# AlertProposalStore — mirrors ProposalStore from dashboard_writing.py
# ---------------------------------------------------------------------------

class AlertProposalStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    @staticmethod
    def _digest(ir: dict) -> str:
        return hashlib.sha256(
            json.dumps(ir, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def create(self, ir: dict, proposal_id: str | None = None) -> dict:
        with self._lock:
            pid = proposal_id or secrets.token_urlsafe(12)
            previous = self._items.get(pid)
            version = (previous or {}).get("version", 0) + 1
            item = {
                "proposalId": pid,
                "version": version,
                "status": "proposed",
                "approvalToken": None,
                "approvedAt": None,
                "ir": copy.deepcopy(ir),
            }
            item["digest"] = self._digest(item["ir"])
            self._items[pid] = item
            return copy.deepcopy(item)

    def get(self, pid: str) -> dict:
        with self._lock:
            if pid not in self._items:
                raise KeyError("Alert proposal not found.")
            return copy.deepcopy(self._items[pid])

    def list_all(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(v) for v in self._items.values()]

    def modify(self, pid: str, ir: dict) -> dict:
        return self.create(ir, pid)

    def approve(self, pid: str, version: int) -> dict:
        with self._lock:
            item = self._items.get(pid)
            if not item or item["version"] != version:
                raise ValueError("Approval version is stale or alert proposal does not exist.")
            item["status"] = "approved"
            item["approvalToken"] = secrets.token_urlsafe(24)
            item["approvedAt"] = datetime.now(timezone.utc).isoformat()
            return copy.deepcopy(item)

    def reject(self, pid: str, version: int) -> dict:
        with self._lock:
            item = self._items.get(pid)
            if not item or item["version"] != version:
                raise ValueError("Rejection version is stale or alert proposal does not exist.")
            item["status"] = "rejected"
            item["approvalToken"] = None
            item["approvedAt"] = None
            return copy.deepcopy(item)

    def set_status(self, pid: str, status: str) -> dict:
        with self._lock:
            item = self._items[pid]
            item["status"] = status
            return copy.deepcopy(item)

    def verified(self, pid: str, version: int, token: str) -> dict:
        item = self.get(pid)
        if (
            item["status"] != "approved"
            or item["version"] != version
            or not secrets.compare_digest(item.get("approvalToken") or "", token or "")
        ):
            raise PermissionError(
                "A valid approval for this exact alert proposal version is required."
            )
        if item["digest"] != self._digest(item["ir"]):
            raise PermissionError(
                "Approved alert proposal content no longer matches its approval digest."
            )
        return item


ALERT_PROPOSALS = AlertProposalStore()


# ---------------------------------------------------------------------------
# IR → Grafana alert rule JSON compiler
# ---------------------------------------------------------------------------

def _compile_alert_ir_to_grafana_json(ir: dict) -> dict:
    """Compile an AlertRuleIR dict to the Grafana provisioning API payload.

    Raises ValueError on missing required fields.
    """
    required = ("title", "folderUID", "ruleGroup", "query", "datasourceUID",
                 "condition", "threshold", "for_duration")
    missing = [f for f in required if not ir.get(f)]
    if missing:
        raise ValueError(f"Alert IR is missing required fields: {', '.join(missing)}")

    condition_map = {
        "gt": "gt", "gte": "gt",   # Grafana uses "gt" for >=  with evaluator params
        "lt": "lt", "lte": "lt",
        "eq": "eq", "ne": "ne",
    }
    grafana_condition = condition_map.get(ir["condition"], "gt")

    # Build the two-query data array:
    # [0] = Prometheus range query
    # [1] = Classic conditions expression (threshold evaluation)
    data = [
        {
            "refId": "A",
            "queryType": "range",
            "relativeTimeRange": {"from": 600, "to": 0},  # last 10 min window
            "datasourceUid": ir["datasourceUID"],
            "model": {
                "datasource": {
                    "type": "prometheus",
                    "uid": ir["datasourceUID"],
                },
                "expr": ir["query"],
                "intervalMs": 1000,
                "maxDataPoints": 43200,
                "refId": "A",
                "instant": False,
                "range": True,
            },
        },
        {
            "refId": "B",
            "queryType": "",
            "relativeTimeRange": {"from": 0, "to": 0},
            "datasourceUid": "-100",  # __expr__ — Grafana built-in
            "model": {
                "conditions": [
                    {
                        "evaluator": {
                            "params": [float(ir["threshold"])],
                            "type": grafana_condition,
                        },
                        "operator": {"type": "and"},
                        "query": {"params": ["A"]},
                        "reducer": {"type": "last", "params": []},
                        "type": "query",
                    }
                ],
                "datasource": {"type": "__expr__", "uid": "-100"},
                "expression": "A",
                "hide": False,
                "refId": "B",
                "type": "classic_conditions",
            },
        },
    ]

    labels: dict[str, str] = {"severity": ir.get("severity", "critical")}
    labels.update(ir.get("extraLabels", {}))

    unit = infer_unit(ir.get("metric", ""))
    summary = (
        ir.get("summary")
        or f"{ir['title']}: {ir.get('metric', 'metric').replace('_', ' ').title()} "
        f"{'above' if ir['condition'] in ('gt','gte') else 'below'} "
        f"{ir['threshold']}{' %' if unit == 'percent' else ' ' + unit}"
        f" for {ir['for_duration']}"
    )

    alert_json = {
        "title": ir["title"],
        "ruleGroup": ir["ruleGroup"],
        "folderUID": ir["folderUID"],
        "noDataState": ir.get("noDataState", "NoData"),
        "execErrState": ir.get("execErrState", "Alerting"),
        "for": ir["for_duration"],
        "labels": labels,
        "annotations": {
            "summary": summary,
            "description": ir.get(
                "description",
                f"Alert fired on {{ $labels.instance }}. "
                f"Value: {{ $values.A.Value }}",
            ),
            "runbook_url": ir.get("runbookUrl", ""),
        },
        "data": data,
        "condition": "B",  # the refId of the threshold expression
    }

    # Attach uid only when updating an existing rule
    if ir.get("uid"):
        alert_json["uid"] = ir["uid"]

    return alert_json


# ---------------------------------------------------------------------------
# Folder discovery / creation
# ---------------------------------------------------------------------------

async def _ensure_alert_folder(folder_name: str) -> str:
    """Return folderUID for an existing folder, or create it if missing.

    Falls back to the General folder (UID = '') if everything fails.
    """
    # List folders via Grafana API
    try:
        raw = await mcp_call(
            "grafana_api_request",
            {"method": "GET", "endpoint": "/api/folders"},
            raw=True,
            timeout=15.0,
        )
        folders = json.loads(raw) if raw else []
        if isinstance(folders, list):
            for folder in folders:
                if isinstance(folder, dict) and folder.get("title", "").lower() == folder_name.lower():
                    logger.info(f"[alerting] Found existing folder '{folder_name}' uid={folder['uid']}")
                    return folder["uid"]
    except Exception as exc:
        logger.warning(f"[alerting] Could not list folders: {exc}")

    # Attempt to create the folder
    try:
        raw = await mcp_call(
            "grafana_api_request",
            {
                "method": "POST",
                "endpoint": "/api/folders",
                "body": json.dumps({"title": folder_name}),
            },
            raw=True,
            timeout=15.0,
        )
        created = json.loads(raw) if raw else {}
        if isinstance(created, dict) and created.get("uid"):
            logger.info(f"[alerting] Created folder '{folder_name}' uid={created['uid']}")
            return created["uid"]
    except Exception as exc:
        logger.warning(f"[alerting] Could not create folder '{folder_name}': {exc}. Falling back to General.")

    return ""  # General folder


# ---------------------------------------------------------------------------
# Public tools exposed to the agent
# ---------------------------------------------------------------------------

async def _propose_alert_rule_async(request: str) -> dict:
    """Parse request, discover metric, build AlertRuleIR, store in ALERT_PROPOSALS."""
    logger.info(f"[alerting] propose_alert_rule request='{request[:120]}'")
    start = time.perf_counter()

    # 1. Parse the natural-language request
    params = parse_alert_request(request)

    # 2. Resolve Prometheus datasource
    ds = await resolve_datasource("prometheus")
    if not ds:
        raise RuntimeError(
            "No Prometheus datasource found in Grafana. "
            "Make sure a Prometheus datasource is configured."
        )

    # 3. Discover all metrics + target label
    try:
        raw_names = await list_prometheus_metric_names(ds["uid"])
        discovered_metrics: list[str] = json.loads(raw_names) if raw_names else []
        if isinstance(discovered_metrics, dict):
            discovered_metrics = discovered_metrics.get("data", discovered_metrics.get("result", []))
    except Exception:
        discovered_metrics = []

    try:
        raw_labels = await list_prometheus_label_names(ds["uid"])
        label_names: list[str] = json.loads(raw_labels) if raw_labels else []
        if isinstance(label_names, dict):
            label_names = label_names.get("data", label_names.get("result", []))
    except Exception:
        label_names = []

    target_label = next(
        (lbl for lbl in TARGET_LABEL_CANDIDATES if lbl in label_names),
        "instance",
    )

    # 4. Resolve the metric name from the search term
    metric = _find_metric_in_schema(params["metric_term"], discovered_metrics)

    # 5. Fetch metric metadata to determine type (counter vs gauge)
    try:
        metadata_raw = await list_prometheus_metric_metadata(ds["uid"], metric)
        metadata = json.loads(metadata_raw) if metadata_raw else {}
        entry = metadata.get(metric, metadata)
        if isinstance(entry, list) and entry:
            entry = entry[0]
        metric_type = str(entry.get("type", "")).lower() if isinstance(entry, dict) else ""
    except Exception:
        metric_type = ""

    # 6. Generate PromQL using well-known patterns or generic heuristic
    query = build_promql(
        metric, target_label, params.get("target"),
        wildcard=True, metric_type=metric_type,
    )

    # 7. Resolve / create alert folder
    folder_uid = await _ensure_alert_folder("Infrastructure Alerts")

    # 8. Assemble AlertRuleIR
    ir = {
        "type": "alert_rule",
        "title": params["title"],
        "metric": metric,
        "metric_term": params["metric_term"],
        "query": query,
        "datasourceUID": ds["uid"],
        "datasourceName": ds["name"],
        "targetLabel": target_label,
        "target": params.get("target"),
        "condition": params["condition"],
        "threshold": params["threshold"],
        "for_duration": params["for_duration"],
        "severity": params["severity"],
        "ruleGroup": "ai-agent-alerts",
        "evaluationInterval": "1m",
        "folderUID": folder_uid,
        "folderTitle": "Infrastructure Alerts",
        "noDataState": "NoData",
        "execErrState": "Alerting",
        "extraLabels": {},
        "uid": "",
    }

    proposal = ALERT_PROPOSALS.create(ir)
    elapsed = time.perf_counter() - start
    logger.info(f"[alerting] proposal created id={proposal['proposalId']} metric={metric} in {elapsed:.3f}s")
    return proposal


def propose_alert_rule(request: str) -> dict:
    """Parse a natural-language alert request and return an AlertRuleIR proposal for human review.

    The proposal is stored in ALERT_PROPOSALS and must be explicitly approved before any
    Grafana write occurs. Returns a summary dict (not the raw IR) suitable for the LLM.
    """
    tool_started = time.perf_counter()
    try:
        proposal = run_sync(_propose_alert_rule_async(request))
        proposal = json.loads(json.dumps(proposal, allow_nan=False))
        ir = proposal.get("ir", {})
        unit = infer_unit(ir.get("metric", ""))
        threshold_str = f"{ir.get('threshold')}{' %' if unit == 'percent' else (' ' + unit if unit else '')}"
        cond_human = {
            "gt": "greater than", "gte": "greater than or equal to",
            "lt": "less than", "lte": "less than or equal to",
            "eq": "equal to", "ne": "not equal to",
        }.get(ir.get("condition", "gt"), "exceeds")
        outcome = {
            "status": "success",
            "proposalId": proposal["proposalId"],
            "title": ir.get("title"),
            "metric": ir.get("metric"),
            "query": ir.get("query"),
            "condition": f"{cond_human} {threshold_str}",
            "for_duration": ir.get("for_duration"),
            "severity": ir.get("severity"),
            "target": ir.get("target") or "all instances",
            "folder": ir.get("folderTitle"),
            "ruleGroup": ir.get("ruleGroup"),
            "datasource": ir.get("datasourceName"),
            "message": (
                f"Alert rule proposal created (ID: {proposal['proposalId']}). "
                "Review the proposal and call the /api/alert-proposals/{id}/approve endpoint "
                "followed by /api/alert-proposals/{id}/execute to create it in Grafana. "
                "No Grafana write has occurred yet."
            ),
        }
        return json.loads(json.dumps(outcome, allow_nan=False))
    except Exception as exc:
        msg = str(exc) or "Alert rule proposal failed."
        logger.error(f"[alerting] propose_alert_rule error: {msg}")
        return {"status": "error", "errors": [{"type": type(exc).__name__, "message": msg}]}


async def _list_alert_rules_async() -> str:
    """Fetch all alert rules from Grafana via the provisioning API."""
    logger.info("[alerting] list_alert_rules")
    raw = await mcp_call(
        "grafana_api_request",
        {"method": "GET", "endpoint": "/api/v1/provisioning/alert-rules"},
        raw=True,
        timeout=20.0,
    )
    if not raw:
        return "No alert rules found or Grafana alerting is not configured."
    try:
        data = json.loads(raw)
        # The provisioning endpoint returns a list directly
        rules = data if isinstance(data, list) else data.get("rules", data.get("data", []))
    except json.JSONDecodeError:
        return f"Grafana returned unexpected response: {raw[:200]}"

    if not rules:
        return "No alert rules are currently configured in Grafana."

    lines = [f"Alert rules ({len(rules)} total):\n"]
    for i, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            continue
        title = rule.get("title", "(untitled)")
        uid = rule.get("uid", "")
        group = rule.get("ruleGroup", "")
        folder_uid = rule.get("folderUID", "")
        state = rule.get("state", rule.get("health", ""))
        for_duration = rule.get("for", "")
        labels = rule.get("labels", {})
        severity = labels.get("severity", "")

        lines.append(f"{i}. {title}")
        if uid:
            lines.append(f"   UID: {uid}")
        if group:
            lines.append(f"   Group: {group}")
        if folder_uid:
            lines.append(f"   Folder UID: {folder_uid}")
        if for_duration:
            lines.append(f"   Pending for: {for_duration}")
        if severity:
            lines.append(f"   Severity: {severity}")
        if state:
            lines.append(f"   State: {state}")
        lines.append("")

    return "\n".join(lines).strip()


def list_alert_rules() -> str:
    """List all Grafana alert rules. Returns a human-readable summary."""
    return run_sync(_list_alert_rules_async())


async def execute_approved_alert(proposal_id: str, version: int, approval_token: str) -> dict:
    """Execute an approved alert rule proposal — compile IR → Grafana JSON → MCP write.

    This is the only function that performs a Grafana write for alert rules.
    Requires a valid approval_token issued by ALERT_PROPOSALS.approve().
    """
    if os.environ.get("GRAFANA_MCP_ENABLE_WRITE", "").lower() not in {"1", "true", "yes", "on"}:
        raise PermissionError(
            "Grafana MCP writes are disabled. Set GRAFANA_MCP_ENABLE_WRITE=true."
        )

    item = ALERT_PROPOSALS.verified(proposal_id, version, approval_token)
    ir = item["ir"]
    ALERT_PROPOSALS.set_status(proposal_id, "executing")

    try:
        alert_json = _compile_alert_ir_to_grafana_json(ir)

        if ir.get("uid"):
            # Update existing rule
            method = "PUT"
            endpoint = f"/api/v1/provisioning/alert-rules/{ir['uid']}"
        else:
            # Create new rule
            method = "POST"
            endpoint = "/api/v1/provisioning/alert-rules"

        result_raw = await mcp_call(
            "grafana_api_request",
            {
                "method": method,
                "endpoint": endpoint,
                "body": json.dumps(alert_json),
            },
            raw=True,
            timeout=30.0,
        )
    except Exception:
        ALERT_PROPOSALS.set_status(proposal_id, "failed")
        raise

    if not result_raw:
        ALERT_PROPOSALS.set_status(proposal_id, "failed")
        raise RuntimeError("Grafana MCP returned no result for alert rule creation.")

    final = ALERT_PROPOSALS.set_status(proposal_id, "built")

    try:
        parsed = json.loads(result_raw)
    except json.JSONDecodeError:
        parsed = result_raw

    logger.info(f"[alerting] Alert rule created/updated for proposal={proposal_id}")
    return {
        "proposal": final,
        "alertPayload": alert_json,
        "grafanaResult": parsed,
    }


def execute_approved_alert_sync(proposal_id: str, version: int, approval_token: str) -> dict:
    """Synchronous wrapper around execute_approved_alert for use in the webapp handler."""
    return run_sync(execute_approved_alert(proposal_id, version, approval_token))
