from unittest.mock import MagicMock, patch

import requests

from app import label_discovery


def _fake_response(series: list[dict]):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"status": "success", "data": series}
    return resp


def _patched_get(series):
    return patch.object(label_discovery, "_get_session",
                         return_value=MagicMock(get=MagicMock(return_value=_fake_response(series))))


# ---- discover_labels_for_metric (keys-only) -- unchanged behavior --------------


def test_discover_labels_for_metric_returns_sorted_keys():
    series = [
        {"__name__": "node_cpu_seconds_total", "instance": "node-00:9200", "mode": "idle"},
        {"__name__": "node_cpu_seconds_total", "instance": "node-01:9200", "mode": "user"},
    ]
    with _patched_get(series):
        keys = label_discovery.discover_labels_for_metric("http://prom", "node_cpu_seconds_total")
    assert keys == ["instance", "mode"]


def test_discover_labels_for_metric_returns_none_on_request_failure():
    with patch.object(label_discovery, "_get_session",
                       return_value=MagicMock(get=MagicMock(side_effect=requests.exceptions.ConnectionError("boom")))):
        keys = label_discovery.discover_labels_for_metric("http://prom", "node_cpu_seconds_total")
    assert keys is None


# ---- discover_label_details_for_metric (keys + values) -------------------------


def test_discover_label_details_collects_values_from_same_series_response():
    series = [
        {"__name__": "node_cpu_seconds_total", "instance": "node-00:9200", "mode": "idle"},
        {"__name__": "node_cpu_seconds_total", "instance": "node-01:9200", "mode": "user"},
        {"__name__": "node_cpu_seconds_total", "instance": "node-01:9200", "mode": "idle"},
    ]
    with _patched_get(series):
        details = label_discovery.discover_label_details_for_metric("http://prom", "node_cpu_seconds_total")
    assert details is not None
    assert details.keys == ["instance", "mode"]
    assert details.values["instance"] == ["node-00:9200", "node-01:9200"]
    assert details.values["mode"] == ["idle", "user"]
    assert details.truncated_keys == frozenset()


def test_discover_label_details_returns_none_on_failure():
    with patch.object(label_discovery, "_get_session",
                       return_value=MagicMock(get=MagicMock(side_effect=requests.exceptions.ConnectionError("boom")))):
        details = label_discovery.discover_label_details_for_metric("http://prom", "node_cpu_seconds_total")
    assert details is None


def test_discover_label_details_truncates_high_cardinality_label():
    series = [
        {"__name__": "m", "instance": "node-00:9200", "request_id": f"req-{i}"}
        for i in range(label_discovery.MAX_VALUES_PER_LABEL + 5)
    ]
    with _patched_get(series):
        details = label_discovery.discover_label_details_for_metric("http://prom", "m")
    assert details is not None
    # instance stayed low-cardinality -- fully confirmed.
    assert details.values["instance"] == ["node-00:9200"]
    # request_id exceeded the cap -- key is known to exist (still in `keys`)
    # but deliberately absent from `values`, and flagged in truncated_keys.
    assert "request_id" in details.keys
    assert "request_id" not in details.values
    assert "request_id" in details.truncated_keys


def test_discover_labels_for_metric_matches_discover_label_details_keys():
    """Backward-compatibility guarantee: the keys-only wrapper must return
    exactly the same keys as the richer function's .keys, from a single
    underlying HTTP call -- no behavior drift for existing callers."""
    series = [
        {"__name__": "node_load1", "instance": "node-00:9200", "job": "node-exporter"},
    ]
    with _patched_get(series):
        keys = label_discovery.discover_labels_for_metric("http://prom", "node_load1")
    with _patched_get(series):
        details = label_discovery.discover_label_details_for_metric("http://prom", "node_load1")
    assert keys == details.keys


# ---- plural / batch discovery ---------------------------------------------------


def test_discover_label_details_for_metrics_is_independent_per_metric():
    good_series = [{"__name__": "node_load1", "instance": "node-00:9200"}]

    def fake_get(url, params=None, timeout=None):
        if params["match[]"] == "node_load1":
            return _fake_response(good_series)
        raise requests.exceptions.ConnectionError("simulated Prometheus hiccup for this metric only")

    with patch.object(label_discovery, "_get_session", return_value=MagicMock(get=fake_get)):
        result = label_discovery.discover_label_details_for_metrics(
            "http://prom", ["node_load1", "node_intr_total"])

    assert result["node_load1"] is not None
    assert result["node_load1"].values["instance"] == ["node-00:9200"]
    assert result["node_intr_total"] is None


# ---- prompt formatting -----------------------------------------------------------


def test_format_label_values_for_prompt_lists_confirmed_values():
    details_by_metric = {
        "node_cpu_seconds_total": label_discovery.LabelDetails(
            keys=["instance"], values={"instance": ["node-00:9200", "node-01:9200"]},
        ),
    }
    block = label_discovery.format_label_values_for_prompt(details_by_metric)
    assert "node-00:9200" in block
    assert "node-01:9200" in block
    assert "node_cpu_seconds_total" in block


def test_format_label_values_for_prompt_flags_truncated_key_by_name():
    details_by_metric = {
        "m": label_discovery.LabelDetails(keys=["request_id"], values={}, truncated_keys=frozenset({"request_id"})),
    }
    block = label_discovery.format_label_values_for_prompt(details_by_metric)
    assert "request_id" in block
    assert "too many distinct values" in block


def test_format_label_values_for_prompt_empty_when_nothing_to_report():
    assert label_discovery.format_label_values_for_prompt({}) == ""
    assert label_discovery.format_label_values_for_prompt({"m": None}) == ""
