from unittest.mock import MagicMock, patch

from app import label_discovery


def test_discovery_includes_bounded_runtime_value_samples():
    response = MagicMock()
    response.json.return_value = {
        "status": "success",
        "data": [
            {"__name__": "node_cpu_seconds_total", "node_id": "node-02", "instance": "node-02:9200"},
            {"__name__": "node_cpu_seconds_total", "node_id": "node-01", "instance": "node-01:9200"},
        ],
    }
    with patch.object(label_discovery, "_get_session") as session:
        session.return_value.get.return_value = response
        keys = label_discovery.discover_labels_for_metric("http://prometheus", "node_cpu_seconds_total")

    assert keys == ["instance", "node_id"]
    assert keys.sample_values == {
        "instance": ["node-01:9200", "node-02:9200"],
        "node_id": ["node-01", "node-02"],
    }
    rendered = label_discovery.format_labels_for_prompt({"node_cpu_seconds_total": keys})
    assert "node_id=['node-01', 'node-02']" in rendered
