from unittest.mock import MagicMock, patch

import requests

from app import grafana_client as gc


def _fake_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}
    return resp


def _kwargs(**overrides):
    base = dict(
        grafana_url="http://localhost:3000",
        service_account_token="glsa_test_token",
        folder_uid="alerts-folder",
        datasource_uid="prom-uid",
        title="High CPU utilization on node-1",
        condition_query='100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100)',
        comparison_operator=">",
        threshold=90,
        for_duration="5m",
    )
    base.update(overrides)
    return base


def test_create_alert_rule_success_extracts_uid_and_builds_deeplink():
    fake = _fake_response(201, {"uid": "abc123"})
    with patch.object(gc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        outcome = gc.create_alert_rule(**_kwargs())
    assert outcome.status == "success"
    assert outcome.rule_uid == "abc123"
    assert outcome.deeplink == "http://localhost:3000/alerting/grafana/abc123/view"


def test_create_alert_rule_missing_config_fails_closed_without_any_http_call():
    with patch.object(gc, "_get_session") as mock_sess:
        outcome = gc.create_alert_rule(**_kwargs(service_account_token=None))
    assert outcome.status == "configuration_error"
    assert not mock_sess.called  # never even tries to reach Grafana


def test_create_alert_rule_missing_folder_uid_fails_closed():
    with patch.object(gc, "_get_session") as mock_sess:
        outcome = gc.create_alert_rule(**_kwargs(folder_uid=None))
    assert outcome.status == "configuration_error"
    assert not mock_sess.called


def test_create_alert_rule_connection_error():
    with patch.object(gc, "_get_session") as mock_sess:
        mock_sess.return_value.post.side_effect = requests.exceptions.ConnectionError("refused")
        outcome = gc.create_alert_rule(**_kwargs())
    assert outcome.status == "endpoint_unreachable"


def test_create_alert_rule_timeout():
    with patch.object(gc, "_get_session") as mock_sess:
        mock_sess.return_value.post.side_effect = requests.exceptions.Timeout()
        outcome = gc.create_alert_rule(**_kwargs())
    assert outcome.status == "timeout"


def test_create_alert_rule_conflict():
    fake = _fake_response(409, {"message": "alert rule with this title already exists"})
    with patch.object(gc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        outcome = gc.create_alert_rule(**_kwargs())
    assert outcome.status == "conflict"
    assert "already exists" in outcome.error


def test_create_alert_rule_non_2xx_is_endpoint_error():
    fake = _fake_response(400, {"message": "invalid folderUID"})
    with patch.object(gc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        outcome = gc.create_alert_rule(**_kwargs())
    assert outcome.status == "endpoint_error"
    assert "invalid folderUID" in outcome.error


def test_create_alert_rule_non_json_response_does_not_crash():
    fake = MagicMock()
    fake.status_code = 201
    fake.json.side_effect = ValueError("not json")
    with patch.object(gc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        outcome = gc.create_alert_rule(**_kwargs())
    assert outcome.status == "success"
    assert outcome.rule_uid is None  # nothing to extract, but this must not raise


def test_condition_uses_math_expression_with_exact_operator_no_approximation():
    """Regression guard: a classic_conditions evaluator only natively
    supports gt/lt/eq, which would force an approximation for >=, <=, and
    != -- that's exactly the kind of invented condition SKILL.md Section
    12.4 forbids. Confirms the request body instead uses a Math expression
    carrying the operator through verbatim."""
    fake = _fake_response(201, {"uid": "xyz"})
    with patch.object(gc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        gc.create_alert_rule(**_kwargs(comparison_operator=">=", threshold=92.5))

    sent_body = mock_sess.return_value.post.call_args.kwargs["json"]
    expr_data = next(d for d in sent_body["data"] if d["refId"] == "C")
    assert expr_data["model"]["type"] == "math"
    assert expr_data["model"]["expression"] == "$B >= 92.5"
    assert expr_data["datasourceUid"] == gc.EXPRESSION_DATASOURCE_UID


def test_reduce_step_sits_between_query_and_math():
    """Grafana's alerting evaluator requires reduced (scalar) data, not raw
    time series. Confirms refId B is a Reduce step that collapses A before
    the Math expression in C compares it against the threshold."""
    fake = _fake_response(201, {"uid": "xyz"})
    with patch.object(gc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        gc.create_alert_rule(**_kwargs())

    sent_body = mock_sess.return_value.post.call_args.kwargs["json"]
    reduce_data = next(d for d in sent_body["data"] if d["refId"] == "B")
    assert reduce_data["model"]["type"] == "reduce"
    assert reduce_data["model"]["expression"] == "A"
    assert reduce_data["model"]["reducer"] == "last"
    assert sent_body["condition"] == "C"


def test_threshold_formatting_drops_trailing_zero_for_whole_numbers():
    fake = _fake_response(201, {"uid": "xyz"})
    with patch.object(gc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        gc.create_alert_rule(**_kwargs(comparison_operator=">", threshold=90))

    sent_body = mock_sess.return_value.post.call_args.kwargs["json"]
    expr_data = next(d for d in sent_body["data"] if d["refId"] == "C")
    assert expr_data["model"]["expression"] == "$B > 90"  # not "90.0"


def test_condition_query_passed_through_verbatim_in_refid_a():
    fake = _fake_response(201, {"uid": "xyz"})
    query = '100 - (avg(rate(node_cpu_seconds_total{mode="idle",instance="node-1"}[1m])) * 100)'
    with patch.object(gc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        gc.create_alert_rule(**_kwargs(condition_query=query))

    sent_body = mock_sess.return_value.post.call_args.kwargs["json"]
    query_data = next(d for d in sent_body["data"] if d["refId"] == "A")
    assert query_data["model"]["expr"] == query
    assert query_data["datasourceUid"] == "prom-uid"


def test_authorization_header_carries_bearer_token():
    fake = _fake_response(201, {"uid": "xyz"})
    with patch.object(gc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        gc.create_alert_rule(**_kwargs(service_account_token="glsa_secret"))

    sent_headers = mock_sess.return_value.post.call_args.kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer glsa_secret"