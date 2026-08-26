from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

from app import prometheus_client as pc


def _fake_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    return resp


def test_query_instant_success():
    fake = _fake_response(200, {"status": "success", "data": {
        "resultType": "vector", "result": [{"metric": {"gpu": "0"}, "value": [1735689600, "78.5"]}]}})
    with patch.object(pc, "_get_session") as mock_sess:
        mock_sess.return_value.get.return_value = fake
        outcome = pc.query_instant("http://localhost:9090", "DCGM_FI_DEV_GPU_TEMP", datetime.now(timezone.utc))
    assert outcome.status == "success"
    assert outcome.raw_data["resultType"] == "vector"


def test_query_range_empty_result():
    fake = _fake_response(200, {"status": "success", "data": {"resultType": "matrix", "result": []}})
    with patch.object(pc, "_get_session") as mock_sess:
        mock_sess.return_value.get.return_value = fake
        outcome = pc.query_range("http://localhost:9090", "node_load1",
                                  datetime(2026, 1, 1, tzinfo=timezone.utc),
                                  datetime(2026, 1, 1, 1, tzinfo=timezone.utc), 60)
    assert outcome.status == "empty_result"


def test_query_range_connection_error():
    with patch.object(pc, "_get_session") as mock_sess:
        mock_sess.return_value.get.side_effect = requests.exceptions.ConnectionError("refused")
        outcome = pc.query_range("http://localhost:9090", "node_load1",
                                  datetime(2026, 1, 1, tzinfo=timezone.utc),
                                  datetime(2026, 1, 1, 1, tzinfo=timezone.utc), 60)
    assert outcome.status == "endpoint_unreachable"


def test_query_range_timeout():
    with patch.object(pc, "_get_session") as mock_sess:
        mock_sess.return_value.get.side_effect = requests.exceptions.Timeout()
        outcome = pc.query_range("http://localhost:9090", "node_load1",
                                  datetime(2026, 1, 1, tzinfo=timezone.utc),
                                  datetime(2026, 1, 1, 1, tzinfo=timezone.utc), 60)
    assert outcome.status == "timeout"


def test_query_range_non_json_response():
    fake = MagicMock()
    fake.status_code = 200
    fake.json.side_effect = ValueError("not json")
    with patch.object(pc, "_get_session") as mock_sess:
        mock_sess.return_value.get.return_value = fake
        outcome = pc.query_range("http://localhost:9090", "node_load1",
                                  datetime(2026, 1, 1, tzinfo=timezone.utc),
                                  datetime(2026, 1, 1, 1, tzinfo=timezone.utc), 60)
    assert outcome.status == "endpoint_error"


def test_query_range_auto_widens_step_on_too_many_samples():
    too_many = _fake_response(422, {"status": "error", "error": "query resulted in too many samples"})
    success = _fake_response(200, {"status": "success", "data": {
        "resultType": "matrix", "result": [{"metric": {}, "values": [[1735689600, "1.0"]]}]}})
    with patch.object(pc, "_get_session") as mock_sess:
        mock_sess.return_value.get.side_effect = [too_many, success]
        outcome = pc.query_range("http://localhost:9090", "node_load1",
                                  datetime(2026, 1, 1, tzinfo=timezone.utc),
                                  datetime(2026, 1, 2, tzinfo=timezone.utc), 1)
    assert outcome.status == "success"
    assert outcome.step_widened is True


def test_query_range_gives_up_after_max_widen_attempts():
    too_many = _fake_response(422, {"status": "error", "error": "too many samples"})
    with patch.object(pc, "_get_session") as mock_sess:
        mock_sess.return_value.get.return_value = too_many
        outcome = pc.query_range("http://localhost:9090", "node_load1",
                                  datetime(2026, 1, 1, tzinfo=timezone.utc),
                                  datetime(2026, 1, 2, tzinfo=timezone.utc), 1)
    assert outcome.status == "endpoint_error"
    assert mock_sess.return_value.get.call_count == pc.MAX_STEP_WIDEN_ATTEMPTS


def test_non_success_status_body_is_endpoint_error():
    fake = _fake_response(400, {"status": "error", "error": "invalid parameter 'query'"})
    with patch.object(pc, "_get_session") as mock_sess:
        mock_sess.return_value.get.return_value = fake
        outcome = pc.query_instant("http://localhost:9090", "not a valid promql (((", datetime.now(timezone.utc))
    assert outcome.status == "endpoint_error"
    assert "invalid parameter" in outcome.error
