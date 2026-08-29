"""
grafana_client.py

Thin, hardened wrapper around Grafana's Alerting Provisioning HTTP API
(POST /api/v1/provisioning/alert-rules). Used ONLY by the alert-rule-
creation confirmation step (SKILL.md Section 12; app/api/routes_alerts.py)
-- never by pipeline.py's construction phase, and never by executor.py
(see executor.py's module docstring). Mirrors prometheus_client.py's
conventions exactly: a pooled requests.Session, a small dataclass outcome
type with a closed status vocabulary, and no exceptions raised for ordinary
operational failures -- callers never need to catch anything from this
module for a timeout, a connection failure, a non-2xx response, or missing
configuration. Only programming errors raise.

WHY A DIRECT HTTP CALL AGAINST GRAFANA, RATHER THAN THE grafana/mcp-grafana
MCP SERVER: this is a headless backend service that already authenticates
to Grafana with its own service-account token (app/config.py) and already
knows exactly which single write it wants to make -- there is no broad
toolset to pick from at request time and no multi-turn agent loop deciding
what to do next. mcp-grafana's `alerting_manage_rules` tool is built for an
interactive agent client that discovers and selects from many Grafana
capabilities over the course of a conversation; running (or supervising a
subprocess for) a full MCP server here would add a second process, its own
session/protocol lifecycle, and a re-wrap of the exact same bearer-token
REST call this module already makes directly -- for zero benefit, since
this service isn't choosing between Grafana tools, it's making one
well-documented, stable API call
(https://grafana.com/docs/grafana/latest/alerting/set-up/provision-alerting-resources/http-api-provisioning/).
Direct HTTP, in the same style as prometheus_client.py, is simpler, has
fewer moving parts, and matches this project's existing convention of thin
`requests`-based backend clients. (The MCP ecosystem's own posture on this
class of write -- see mcp-grafana's `create_incident` tool description,
quoted in SKILL.md Section 12's originating design discussion: "should be
used judiciously and sparingly, and only after confirmation from the user,
as it may notify or alarm lots of people" -- is exactly the propose/confirm
boundary Section 12.1 and routes_alerts.py implement; this module performs
the actual write, but only when called by that confirmation step, never
from construction.)

ALERT CONDITION SHAPE: builds a two-refId rule -- a Prometheus range query
in refId "A" (the verified condition_query), and a Grafana Math expression
in refId "B" evaluating "$A <operator> <threshold>" as the alert condition.
A Math expression (rather than the legacy "classic_conditions" evaluator)
is used deliberately: classic conditions only support gt/lt/eq natively,
which would force an approximation for >=, <=, and != (e.g. silently
treating ">=" as ">"). That kind of substitution is exactly the "invented
condition" SKILL.md Section 12.4 forbids -- a Math expression supports all
six comparison operators exactly, verbatim, with no approximation.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

DEFAULT_TIMEOUT_SECONDS = 15
PROVISIONING_ALERT_RULES_PATH = "/api/v1/provisioning/alert-rules"

# Grafana's built-in pseudo-datasource UID for server-side expressions
# (Math/Reduce/Resample/Threshold) -- not a real, queryable datasource.
EXPRESSION_DATASOURCE_UID = "__expr__"

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    """One pooled requests.Session reused across calls, instead of opening
    a fresh TCP connection per call -- see prometheus_client.py's identical
    rationale."""
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


@dataclass
class AlertRuleOutcome:
    status: str  # "success" | "configuration_error" | "endpoint_unreachable"
                 # | "endpoint_error" | "timeout" | "conflict"
    rule_uid: str | None = None
    deeplink: str | None = None
    raw_response: dict | None = None
    error: str | None = None


def create_alert_rule(
    *,
    grafana_url: str,
    service_account_token: str | None,
    folder_uid: str | None,
    datasource_uid: str | None,
    title: str,
    condition_query: str,
    comparison_operator: str,
    threshold: float,
    for_duration: str,
    org_id: int = 1,
    rule_group: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> AlertRuleOutcome:
    """Creates exactly one Grafana-managed alert rule via POST
    /api/v1/provisioning/alert-rules. This is the ONLY function in this
    module that performs a write; every other concern (whether this should
    be called at all, whether the user actually confirmed) belongs to
    app/api/routes_alerts.py, not here.

    Fails closed -- returns "configuration_error" and makes NO HTTP request
    at all -- if any required configuration is missing. This is
    deliberately stricter than prometheus_client.py's read-only calls:
    a misconfigured write (e.g. silently landing in the wrong folder, or
    against the wrong datasource) is a materially worse failure mode than a
    misconfigured read, so this function refuses to guess or fall back to
    an implicit default for any of grafana_url / service_account_token /
    folder_uid / datasource_uid.

    comparison_operator must be one of >, <, >=, <=, ==, != (validator.py's
    _VALID_COMPARISON_OPERATORS) -- passed through verbatim into a Grafana
    Math expression (see module docstring), never approximated.
    """
    missing = [
        name for name, value in [
            ("grafana_url", grafana_url),
            ("service_account_token", service_account_token),
            ("folder_uid", folder_uid),
            ("datasource_uid", datasource_uid),
        ]
        if not value
    ]
    if missing:
        return AlertRuleOutcome(
            status="configuration_error",
            error=(
                f"Cannot create a Grafana alert rule: missing required configuration: "
                f"{missing}. See app/config.py's grafana_* settings (alert_rule_creation_enabled "
                f"must also be True for this endpoint to be reachable at all)."
            ),
        )

    body = _build_provisioning_body(
        folder_uid=folder_uid, datasource_uid=datasource_uid, title=title,
        condition_query=condition_query, comparison_operator=comparison_operator,
        threshold=threshold, for_duration=for_duration, org_id=org_id, rule_group=rule_group,
    )

    url = grafana_url.rstrip("/") + PROVISIONING_ALERT_RULES_PATH
    headers = {
        "Authorization": f"Bearer {service_account_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        resp = _get_session().post(url, json=body, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        return AlertRuleOutcome(status="timeout", error=f"Request to {url} exceeded {timeout}s timeout.")
    except requests.exceptions.ConnectionError as e:
        return AlertRuleOutcome(status="endpoint_unreachable", error=f"Could not connect to {url}: {e}")
    except requests.exceptions.RequestException as e:
        return AlertRuleOutcome(status="endpoint_error", error=f"Request to {url} failed: {e}")

    try:
        response_body = resp.json()
    except ValueError:
        response_body = None

    if resp.status_code == 409:
        return AlertRuleOutcome(
            status="conflict",
            error=(response_body or {}).get("message")
            or "A rule with this title/rule-group/folder already exists.",
            raw_response=response_body,
        )
    if resp.status_code not in (200, 201):
        error_msg = (response_body or {}).get("message") or f"HTTP {resp.status_code}"
        return AlertRuleOutcome(status="endpoint_error", error=error_msg, raw_response=response_body)

    rule_uid = (response_body or {}).get("uid") or None
    deeplink = _build_deeplink(grafana_url, rule_uid) if rule_uid else None
    return AlertRuleOutcome(status="success", rule_uid=rule_uid, deeplink=deeplink, raw_response=response_body)


def _build_provisioning_body(
    *, folder_uid: str, datasource_uid: str, title: str, condition_query: str,
    comparison_operator: str, threshold: float, for_duration: str, org_id: int,
    rule_group: str | None,
) -> dict:
    """Assembles the POST body per Grafana's Alerting Provisioning HTTP API.

    Uses a three-step pipeline, which is the standard pattern Grafana's own
    UI builds when you create an alert rule through the web interface:

      refId A  — Data: the PromQL range query (the verified condition_query).
      refId B  — Reduce: collapses A's time-series output to a single number
                 per series using the `last` reducer. This is REQUIRED because
                 Grafana's alerting evaluator rejects raw time-series data in
                 a Math/Threshold expression with "invalid format of
                 evaluation results ... looks like time series data, only
                 reduced data can be alerted on."
      refId C  — Math: evaluates `$B <operator> <threshold>` against the
                 reduced scalar. Uses a Math expression (not classic_conditions)
                 so all six comparison operators work exactly, verbatim.

    The condition field points at "C" (the Math step) — that's what Grafana
    evaluates to decide whether the rule is firing.
    """
    math_expression = f"$B {comparison_operator} {_format_threshold(threshold)}"
    return {
        "title": title,
        "ruleGroup": rule_group or title,
        "folderUID": folder_uid,
        "orgID": org_id,
        "uid": "",
        "condition": "C",
        "noDataState": "NoData",
        "execErrState": "Error",
        "for": for_duration,
        "data": [
            {
                "refId": "A",
                "queryType": "",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": datasource_uid,
                "model": {
                    "expr": condition_query,
                    "intervalMs": 1000,
                    "maxDataPoints": 43200,
                    "refId": "A",
                },
            },
            {
                "refId": "B",
                "queryType": "",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": EXPRESSION_DATASOURCE_UID,
                "model": {
                    "type": "reduce",
                    "expression": "A",
                    "reducer": "last",
                    "settings": {"mode": ""},
                    "refId": "B",
                },
            },
            {
                "refId": "C",
                "queryType": "",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": EXPRESSION_DATASOURCE_UID,
                "model": {
                    "type": "math",
                    "expression": math_expression,
                    "intervalMs": 1000,
                    "maxDataPoints": 43200,
                    "refId": "C",
                },
            },
        ],
    }


def _format_threshold(threshold: float) -> str:
    """Renders 90 as "90" (not "90.0") but 92.5 as "92.5" -- cosmetic only,
    never changes the numeric value itself."""
    if isinstance(threshold, float) and threshold.is_integer():
        return str(int(threshold))
    return str(threshold)


def _build_deeplink(grafana_url: str, rule_uid: str) -> str:
    """Best-effort link to the created rule's detail page. Purely a
    convenience for the API response (app/api/routes_alerts.py) -- if
    Grafana's URL scheme for this ever changes, a stale deeplink degrades to
    "click through from the alert list" rather than breaking anything
    functionally, since the rule itself was already created successfully by
    the time this is called."""
    return f"{grafana_url.rstrip('/')}/alerting/grafana/{rule_uid}/view"