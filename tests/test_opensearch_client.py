from unittest.mock import MagicMock, patch

import pytest
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


def test_the_session_reuses_keep_alive_adapters(monkeypatch):
    monkeypatch.setattr(osc, "_session", None)
    session = osc._get_session()
    try:
        assert osc._get_session() is session
        assert all(session.adapters[scheme]._pool_maxsize == 20 for scheme in ("http://", "https://"))
    finally:
        session.close()


@pytest.mark.parametrize("body, projected", [
    ({"size": 2, "_source": ["Body"]}, True),
    ({"size": 0, "aggs": {"by_kind": {}}}, False),
])
def test_filtered_search_preserves_open_search_envelope_and_leaves_dsl_intact(body, projected):
    returned = {"took": 5, "timed_out": True, "terminated_early": True,
                "_shards": {"failed": 1, "failures": [{"reason": {"type": "shard_error"}}]},
                "hits": {"total": {"value": 101, "relation": "gte"}},
                "aggregations": {"by_kind": {"buckets": []}}}
    if projected:
        returned["hits"]["hits"] = [{"_index": "log", "_id": "1", "_source": {"Body": "message"}}]
    with patch.object(osc, "_get_session") as mock_session:
        mock_session.return_value.post.return_value = _fake_response(200, returned)
        result = osc.search("http://localhost:9600", "log-*", body, timeout=12)
    sent = mock_session.return_value.post.call_args.kwargs
    assert sent["json"] is body and sent["timeout"] == 12
    fields = set(sent["params"]["filter_path"].split(","))
    assert {"hits.total", "aggregations", "_shards", "took", "timed_out", "terminated_early", "error"} <= fields
    assert ("hits.hits._source" in fields) is projected
    assert result.raw_body == returned and result.status == "success"


def test_unprojected_search_does_not_filter_response_and_rejects_non_object():
    with patch.object(osc, "_get_session") as mock_session:
        mock_session.return_value.post.return_value = _fake_response(200, [1])
        result = osc.search("http://localhost:9600", "log-*", {})
    assert "params" not in mock_session.return_value.post.call_args.kwargs
    assert result.status == "endpoint_error"


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
