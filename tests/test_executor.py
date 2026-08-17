from unittest.mock import patch

from app import executor, opensearch_client, prometheus_client


def test_single_mode_prometheus_range_success(settings):
    entry = {"status": "ok", "data_source": "prometheus", "query": "node_load1",
              "time_range": {"from": "now-1h", "to": "now", "step": "60s"}}
    fake_outcome = prometheus_client.ExecutionOutcome(
        status="success",
        raw_data={"resultType": "matrix", "result": [{"metric": {"instance": "node-1"}, "values": [[1735689600, "1.2"]]}]},
    )
    with patch.object(prometheus_client, "query_range", return_value=fake_outcome):
        result = executor.execute_contract(entry, settings)
    assert result["execution"]["execution_status"] == "success"
    assert result["execution"]["result_type"] == "series"


def test_prometheus_instant_query_dispatch(settings):
    entry = {"status": "ok", "data_source": "prometheus", "query": "DCGM_FI_DEV_GPU_TEMP",
              "query_type": "instant", "time_range": {"time": "now"}}
    fake_outcome = prometheus_client.ExecutionOutcome(
        status="success", raw_data={"resultType": "vector", "result": [{"metric": {"gpu": "0"}, "value": [1735689600, "78.5"]}]})
    with patch.object(prometheus_client, "query_instant", return_value=fake_outcome) as mock_instant, \
         patch.object(prometheus_client, "query_range") as mock_range:
        result = executor.execute_contract(entry, settings)
    assert mock_instant.called
    assert not mock_range.called
    assert result["execution"]["series"][0]["points"][0]["value"] == 78.5


def test_multi_mode_one_bad_entry_does_not_poison_others(settings):
    contract = {
        "mode": "multi",
        "results": [
            {"status": "ok", "data_source": "prometheus", "query": "node_load1",
             "time_range": {"from": "now-1h", "to": "now", "step": "60s"}},
            {"status": "ok", "data_source": "prometheus"},  # malformed: missing 'query'
            {"status": "ambiguous_metric", "candidates": [{"name": "x", "purpose": "y"}]},
        ],
    }
    fake_outcome = prometheus_client.ExecutionOutcome(
        status="success",
        raw_data={"resultType": "matrix", "result": [{"metric": {"instance": "node-1"}, "values": [[1735689600, "1.2"]]}]},
    )
    with patch.object(prometheus_client, "query_range", return_value=fake_outcome):
        result = executor.execute_contract(contract, settings)

    assert result["results"][0]["execution"]["execution_status"] == "success"
    assert result["results"][1]["execution"]["execution_status"] == "endpoint_error"
    assert "query" in result["results"][1]["execution"]["error"]
    assert "execution" not in result["results"][2] or result["results"][2].get("execution") is None
    assert result["results"][2]["status"] == "ambiguous_metric"


def test_non_executable_status_passes_through_untouched(settings):
    entry = {"status": "declined", "reason": "nonsensical_input", "explanation": "..."}
    result = executor.execute_contract(entry, settings)
    assert result == entry
    assert "execution" not in result


def test_unhandled_exception_in_one_entry_becomes_endpoint_error_not_a_crash(settings):
    entry = {"status": "ok", "data_source": "prometheus", "query": "node_load1",
              "time_range": {"from": "now-1h", "to": "now", "step": "60s"}}
    with patch.object(prometheus_client, "query_range", side_effect=RuntimeError("boom")):
        result = executor.execute_contract(entry, settings)
    assert result["execution"]["execution_status"] == "endpoint_error"
    assert "boom" in result["execution"]["error"]


def test_opensearch_entry_missing_index_is_endpoint_error(settings):
    entry = {"status": "ok", "data_source": "opensearch", "query": {"query": {"match_all": {}}}}
    result = executor.execute_contract(entry, settings)
    assert result["execution"]["execution_status"] == "endpoint_error"
    assert "index" in result["execution"]["error"]


def test_opensearch_entry_with_list_index_joins_as_comma_pattern(settings):
    entry = {"status": "ok", "data_source": "opensearch",
              "query": {"query": {"match_all": {}}}, "index": ["syslog-*", "consolelog-*"]}
    fake_outcome = opensearch_client.ExecutionOutcome(
        status="success", raw_body={"hits": {"total": {"value": 1}, "hits": [{"_source": {"Body": "x"}}]}})
    with patch.object(opensearch_client, "search", return_value=fake_outcome) as mock_search:
        executor.execute_contract(entry, settings)
    assert mock_search.call_args[0][1] == "syslog-*,consolelog-*"


def test_unrecognized_data_source_is_not_executed(settings):
    entry = {"status": "ok", "data_source": "carrier_pigeon", "query": "whatever"}
    result = executor.execute_contract(entry, settings)
    assert result["execution"]["execution_status"] == "not_executed"


def test_multi_mode_unmapped_entry_alongside_ok_entry_survives_untouched(settings):
    """End-to-end proof of the partial-datasource-coverage behavior
    (pipeline.py's unresolved_topics merge): a `mode: "multi"` contract
    with one executable Prometheus entry and one synthetic `unmapped` entry
    (produced when part of a compound question has no OpenSearch domain
    reference yet) must execute the first and leave the second completely
    untouched -- no execution block, no attempt to contact any backend."""
    contract = {
        "mode": "multi",
        "results": [
            {"status": "ok", "data_source": "prometheus", "query": "node_load1",
             "time_range": {"from": "now-1h", "to": "now", "step": "60s"}},
            {"status": "unmapped", "explanation": "No reference covers: recent error logs for node-1."},
        ],
        "synthesis": None,
    }
    fake_outcome = prometheus_client.ExecutionOutcome(
        status="success",
        raw_data={"resultType": "matrix", "result": [{"metric": {"instance": "node-1"}, "values": [[1735689600, "1.2"]]}]},
    )
    with patch.object(prometheus_client, "query_range", return_value=fake_outcome) as mock_range, \
         patch.object(opensearch_client, "search") as mock_search:
        result = executor.execute_contract(contract, settings)

    assert result["results"][0]["execution"]["execution_status"] == "success"
    assert result["results"][1] == contract["results"][1]  # byte-for-byte untouched
    assert "execution" not in result["results"][1]
    assert mock_range.called
    assert not mock_search.called  # never even attempted to reach OpenSearch for the unmapped half


def test_prometheus_endpoint_unreachable_surfaces_correctly(settings):
    entry = {"status": "ok", "data_source": "prometheus", "query": "node_load1",
              "time_range": {"from": "now-1h", "to": "now", "step": "60s"}}
    fake_outcome = prometheus_client.ExecutionOutcome(status="endpoint_unreachable", error="Could not connect")
    with patch.object(prometheus_client, "query_range", return_value=fake_outcome):
        result = executor.execute_contract(entry, settings)
    assert result["execution"]["execution_status"] == "endpoint_unreachable"
    assert result["execution"]["error"] == "Could not connect"
