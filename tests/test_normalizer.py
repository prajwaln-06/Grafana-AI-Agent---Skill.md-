import math

from app.normalizer import (
    _legend_label_for,
    normalize_opensearch_result,
    normalize_prometheus_result,
    safe_float,
)


def test_legend_label_includes_keys_not_just_values():
    """Regression test: a values-only legend ("node-1:9100, idle") is
    ambiguous the moment a series varies along more than one label -- the
    frontend can't tell which value is which dimension without separately
    inspecting the raw label dict, defeating the point of a pre-built
    legend string. Must include 'key=value' pairs."""
    label = _legend_label_for({"instance": "node-1:9100", "mode": "idle"})
    assert label == "instance=node-1:9100, mode=idle"


def test_legend_label_falls_back_to_value_for_no_labels():
    assert _legend_label_for({}) == "value"


def test_safe_float_rejects_nan_and_inf():
    assert safe_float(float("nan")) is None
    assert safe_float(float("inf")) is None
    assert safe_float(float("-inf")) is None
    assert safe_float("34.2") == 34.2
    assert safe_float(None) is None
    assert safe_float("not-a-number") is None


def test_prometheus_matrix_multi_series_is_json_safe_and_comparison_tagged():
    raw = {
        "resultType": "matrix",
        "result": [
            {"metric": {"instance": "node-1:9100"}, "values": [[1735689600, "34.2"], [1735689660, "NaN"]]},
            {"metric": {"instance": "node-2:9100"}, "values": [[1735689600, "55.1"], [1735689660, "60.0"]]},
        ],
    }
    result = normalize_prometheus_result(raw)
    assert result.result_type == "series"
    assert len(result.series) == 2
    assert result.had_invalid_samples is True

    d = result.to_dict()
    assert d["comparison"]["series_count"] == 2
    assert "instance" in d["comparison"]["differentiated_by"]
    # NaN must never survive as a literal float -- always sanitized to None.
    node1_points = [s for s in d["series"] if s["labels"]["instance"] == "node-1:9100"][0]["points"]
    assert node1_points[1]["value"] is None
    for series in d["series"]:
        for point in series["points"]:
            if point["value"] is not None:
                assert not math.isnan(point["value"])
                assert not math.isinf(point["value"])


def test_prometheus_vector_instant_query():
    raw = {"resultType": "vector", "result": [{"metric": {"gpu": "0"}, "value": [1735689600, "78.5"]}]}
    result = normalize_prometheus_result(raw)
    assert result.result_type == "series"
    assert len(result.series) == 1
    assert result.series[0].points[0].value == 78.5


def test_prometheus_empty_result():
    raw = {"resultType": "matrix", "result": []}
    result = normalize_prometheus_result(raw)
    assert result.count == 0


def test_prometheus_series_truncation():
    raw = {
        "resultType": "matrix",
        "result": [{"metric": {"instance": f"node-{i}"}, "values": [[1735689600, "1.0"]]} for i in range(10)],
    }
    result = normalize_prometheus_result(raw, max_series=5)
    assert len(result.series) == 5
    assert result.truncated is True
    assert result.original_count == 10


def test_opensearch_date_histogram_with_terms_subagg_produces_comparison_series():
    raw = {
        "aggregations": {
            "over_time": {
                "buckets": [
                    {"key": 1735689600000, "key_as_string": "2025-01-01T00:00:00.000Z", "doc_count": 5,
                     "by_service": {"buckets": [{"key": "sshd", "doc_count": 3}, {"key": "kernel", "doc_count": 2}]}},
                    {"key": 1735689660000, "key_as_string": "2025-01-01T00:01:00.000Z", "doc_count": 7,
                     "by_service": {"buckets": [{"key": "sshd", "doc_count": 4}, {"key": "kernel", "doc_count": 3}]}},
                ]
            }
        }
    }
    result = normalize_opensearch_result(raw)
    assert result.result_type == "series"
    assert len(result.series) == 2
    labels = sorted(s.labels["by_service"] for s in result.series)
    assert labels == ["kernel", "sshd"]
    sshd_series = [s for s in result.series if s.labels["by_service"] == "sshd"][0]
    assert [p.value for p in sshd_series.points] == [3.0, 4.0]


def test_opensearch_bare_date_histogram_single_series():
    raw = {
        "aggregations": {
            "over_time": {"buckets": [{"key": 1735689600000, "key_as_string": "2025-01-01T00:00:00Z", "doc_count": 5}]}
        }
    }
    result = normalize_opensearch_result(raw)
    assert result.result_type == "series"
    assert len(result.series) == 1
    assert result.series[0].labels == {}


def test_opensearch_terms_only_produces_buckets():
    raw = {"aggregations": {"by_host": {"buckets": [{"key": "node-1", "doc_count": 12}, {"key": "node-2", "doc_count": 3}]}}}
    result = normalize_opensearch_result(raw)
    assert result.result_type == "buckets"
    assert len(result.buckets) == 2
    assert {b.key: b.doc_count for b in result.buckets} == {"node-1": 12, "node-2": 3}


def test_opensearch_metric_agg_no_buckets_produces_single_point_series():
    raw = {"aggregations": {"avg_temp": {"value": 65.4}}}
    result = normalize_opensearch_result(raw)
    assert result.result_type == "series"
    assert len(result.series) == 1
    assert result.series[0].points[0].value == 65.4


def test_opensearch_plain_hits_search():
    raw = {
        "hits": {
            "total": {"value": 2},
            "hits": [
                {"_source": {"@timestamp": "2025-01-01T00:00:00Z", "Severity": "WARN",
                              "Body": "GPU 3 temperature warning on node-02",
                              "Resource": {"host.name": "node-02", "service.name": "dcgm-exporter"},
                              "Attributes": {"gpu": "3", "metric": "GPU_TEMP"}}},
            ],
        }
    }
    result = normalize_opensearch_result(raw)
    assert result.result_type == "hits"
    assert result.total_hits == 2
    assert len(result.hits) == 1
    assert result.hits[0].severity == "WARN"
    assert result.hits[0].resource["host.name"] == "node-02"
    assert result.hits[0].attributes["gpu"] == "3"


def test_opensearch_hits_truncation():
    raw = {"hits": {"total": {"value": 10}, "hits": [{"_source": {"Body": f"event {i}"}} for i in range(10)]}}
    result = normalize_opensearch_result(raw, max_hits=3)
    assert len(result.hits) == 3
    assert result.truncated is True
    assert result.original_count == 10


def test_opensearch_nan_value_in_metric_agg_is_sanitized():
    raw = {"aggregations": {"avg_temp": {"value": None}}}
    result = normalize_opensearch_result(raw)
    assert result.series[0].points[0].value is None
