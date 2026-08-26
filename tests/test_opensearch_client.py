from unittest.mock import MagicMock, patch

import requests

from app import opensearch_client as osc


def _fake_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status_code}")
    return resp


def test_search_success_with_aggregations():
    fake = _fake_response(200, {
        "hits": {"total": {"value": 42}, "hits": []},
        "aggregations": {"over_time": {"buckets": []}},
    })
    with patch.object(osc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        outcome = osc.search("http://localhost:9600", "syslog-*", {"size": 0, "aggs": {}})
    assert outcome.status == "success"


def test_search_empty_result_no_aggs():
    fake = _fake_response(200, {"hits": {"total": {"value": 0}, "hits": []}})
    with patch.object(osc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        outcome = osc.search("http://localhost:9600", "syslog-*", {"query": {"match_all": {}}})
    assert outcome.status == "empty_result"


def test_search_connection_error():
    with patch.object(osc, "_get_session") as mock_sess:
        mock_sess.return_value.post.side_effect = requests.exceptions.ConnectionError("refused")
        outcome = osc.search("http://localhost:9600", "syslog-*", {})
    assert outcome.status == "endpoint_unreachable"


def test_search_opensearch_error_body_extracted():
    fake = _fake_response(400, {"error": {"type": "parsing_exception", "reason": "bad query syntax"}})
    with patch.object(osc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        outcome = osc.search("http://localhost:9600", "syslog-*", {"bad": True})
    assert outcome.status == "endpoint_error"
    assert "bad query syntax" in outcome.error


def test_discover_attribute_keys_from_mapping():
    fake = _fake_response(200, {
        "syslog-2026.08.14": {
            "mappings": {
                "properties": {
                    "Attributes": {"properties": {"gpu": {"type": "keyword"}, "priority": {"type": "long"}}},
                }
            }
        }
    })
    with patch.object(osc, "_get_session") as mock_sess:
        mock_sess.return_value.get.return_value = fake
        keys = osc.discover_attribute_keys("http://localhost:9600", "syslog-*")
    assert keys == ["gpu", "priority"]


def test_discover_attribute_keys_returns_none_on_failure():
    with patch.object(osc, "_get_session") as mock_sess:
        mock_sess.return_value.get.side_effect = requests.exceptions.ConnectionError("refused")
        keys = osc.discover_attribute_keys("http://localhost:9600", "syslog-*")
    assert keys is None


def test_discover_attribute_keys_confirmed_empty_is_not_none():
    fake = _fake_response(200, {"syslog-2026.08.14": {"mappings": {"properties": {}}}})
    with patch.object(osc, "_get_session") as mock_sess:
        mock_sess.return_value.get.return_value = fake
        keys = osc.discover_attribute_keys("http://localhost:9600", "syslog-*")
    assert keys == []


def test_list_indices():
    fake = _fake_response(200, [{"index": "syslog-2026.08.14"}, {"index": "heartbeat"}])
    with patch.object(osc, "_get_session") as mock_sess:
        mock_sess.return_value.get.return_value = fake
        idxs = osc.list_indices("http://localhost:9600")
    assert idxs == ["syslog-2026.08.14", "heartbeat"]


def test_list_indices_returns_none_on_failure():
    with patch.object(osc, "_get_session") as mock_sess:
        mock_sess.return_value.get.side_effect = requests.exceptions.ConnectionError("refused")
        idxs = osc.list_indices("http://localhost:9600")
    assert idxs is None


def test_sample_recent_documents():
    fake = _fake_response(200, {
        "hits": {"total": {"value": 1}, "hits": [{"_source": {"Body": "hello", "Attributes": {"gpu": "0"}}}]}
    })
    with patch.object(osc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        docs = osc.sample_recent_documents("http://localhost:9600", "syslog-*", size=5)
    assert docs == [{"Body": "hello", "Attributes": {"gpu": "0"}}]


def test_sample_recent_documents_empty_index():
    fake = _fake_response(200, {"hits": {"total": {"value": 0}, "hits": []}})
    with patch.object(osc, "_get_session") as mock_sess:
        mock_sess.return_value.post.return_value = fake
        docs = osc.sample_recent_documents("http://localhost:9600", "syslog-*")
    assert docs == []
