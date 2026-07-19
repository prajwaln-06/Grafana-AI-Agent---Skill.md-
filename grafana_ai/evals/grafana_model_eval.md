# Grafana Model Evaluation

## Project Goals

1. Chat with Grafana data through grounded tool calls.
2. Produce graphable metrics/log outputs for quick visual summaries.
3. Support operator workflows with safe discovery, troubleshooting, and human review points.

## Leaderboard

| Rank | Model | Score | Tool successes | Errors | Avg latency |
|---:|---|---:|---:|---:|---:|
| 1 | `ollama_chat/qwen2.5:14b` | 50/50 (100.0%) | 5/5 | 0 | 67.7s |
| 2 | `ollama_chat/gemma4:12b` | 48/50 (96.0%) | 5/5 | 2 | 210.78s |

## Detailed Runs

### ollama_chat/qwen2.5:14b / datasource_discovery / trial 1

- Score: 10/10
- Latency: 123.943s
- Function calls: list_datasources
- Function responses: list_datasources
- Error: none
- Final text: The available Grafana datasources are:

- **Name:** prometheus  
  **UID:** efqw8q6prl91cf  
  **Type:** prometheus  
  **Default:** true

This is the only datasource currently configured in your Grafana instance.
- Notes: Model emitted at least one tool call.; Model called an expected tool.; All required tool calls were present.; No known hallucinated/forbidden tool names.; At least one tool response was returned.; Final answer was produced after tool output.

### ollama_chat/qwen2.5:14b / dashboard_discovery / trial 1

- Score: 10/10
- Latency: 57.817s
- Function calls: search_dashboards
- Function responses: search_dashboards
- Error: none
- Final text: limburg
 limburg 
 limburgHere is a list of dashboards found:

- **Title**: Prometheus CPU Usage
- **UID**: ad6j4mh
- **URL**: /d/ad6j4mh/prometheus-cpu-usage

This is the only dashboard that was returned by this query. If you need more details or another search, let me know!
- Notes: Model emitted at least one tool call.; Model called an expected tool.; All required tool calls were present.; No known hallucinated/forbidden tool names.; At least one tool response was returned.; Final answer was produced after tool output.

### ollama_chat/qwen2.5:14b / dashboard_summary / trial 1

- Score: 10/10
- Latency: 58.116s
- Function calls: search_dashboards, search_dashboards
- Function responses: search_dashboards, search_dashboards
- Error: none
- Final text: It seems there's no dashboard named exactly "Node Exporter Full". Let me try with a slightly less specific query to see if it can find something close.

I couldn't find any dashboard with a name similar to "Node Exporter Full". There might be a slight difference in the naming or it may not exist. Could you please provide more details about what metrics or data this dashboard is supposed to display so I can look for an alternative?
- Notes: Model emitted at least one tool call.; Model called an expected tool.; All required tool calls were present.; No known hallucinated/forbidden tool names.; At least one tool response was returned.; Final answer was produced after tool output.

### ollama_chat/qwen2.5:14b / datasource_health / trial 1

- Score: 10/10
- Latency: 41.417s
- Function calls: check_datasources_health
- Function responses: check_datasources_health
- Error: none
- Final text: The Prometheus datasource is unhealthy. The error message indicates that there was a connection refused when trying to query the Prometheus API at `http://host.docker.internal:9090/api/v1/query`. This suggests that the Prometheus server might not be running or accessible on this address.
- Notes: Model emitted at least one tool call.; Model called an expected tool.; All required tool calls were present.; No known hallucinated/forbidden tool names.; At least one tool response was returned.; Final answer was produced after tool output.

### ollama_chat/qwen2.5:14b / prometheus_query_graphable / trial 1

- Score: 10/10
- Latency: 57.197s
- Function calls: query_prometheus
- Function responses: query_prometheus
- Error: none
- Final text: It seems there's no Prometheus datasource configured in Grafana or it can't be accessed from this context. Before proceeding, please ensure a Prometheus data source named 'prometheus' exists and is correctly set up within your Grafana instance.

If you have the correct UID for another Prometheus datasource, we can use that to run our query instead. Would you like to try with another existing datasource? If so, could you provide its UID or name?
- Notes: Model emitted at least one tool call.; Model called an expected tool.; All required tool calls were present.; No known hallucinated/forbidden tool names.; At least one tool response was returned.; Final answer was produced after tool output.

### ollama_chat/gemma4:12b / datasource_discovery / trial 1

- Score: 10/10
- Latency: 184.991s
- Function calls: list_datasources, list_datasources
- Function responses: list_datasources, list_datasources
- Error: none
- Final text: The following Grafana datasource is available:
- **Name**: prometheus
- **Type**: prometheus
- **UID**: efqw8q6prl91cf
- Notes: Model emitted at least one tool call.; Model called an expected tool.; All required tool calls were present.; No known hallucinated/forbidden tool names.; At least one tool response was returned.; Final answer was produced after tool output.

### ollama_chat/gemma4:12b / dashboard_discovery / trial 1

- Score: 10/10
- Latency: 144.271s
- Function calls: search_dashboards
- Function responses: search_dashboards
- Error: none
- Final text: The following dashboard was found:
- **Prometheus CPU Usage** (UID: `ad6j4mh`)
- Notes: Model emitted at least one tool call.; Model called an expected tool.; All required tool calls were present.; No known hallucinated/forbidden tool names.; At least one tool response was returned.; Final answer was produced after tool output.

### ollama_chat/gemma4:12b / dashboard_summary / trial 1

- Score: 9/10
- Latency: 300.028s
- Function calls: search_dashboards, search_dashboards, search_dashboards, search_dashboards, search_dashboards, search_dashboards
- Function responses: search_dashboards, search_dashboards, search_dashboards, search_dashboards, search_dashboards, search_dashboards
- Error: Timed out after 300s
- Final text: none
- Notes: Run errored before completion.; Model emitted at least one tool call.; Model called an expected tool.; All required tool calls were present.; No known hallucinated/forbidden tool names.; At least one tool response was returned.

### ollama_chat/gemma4:12b / datasource_health / trial 1

- Score: 10/10
- Latency: 124.609s
- Function calls: list_datasources, check_datasources_health
- Function responses: list_datasources, check_datasources_health
- Error: none
- Final text: The Prometheus datasource (UID: `efqw8q6prl91cf`) is currently **unhealthy**. 

The check failed with a connection error, indicating that the server at `http://host.docker.internal:9090` could not be reached.
- Notes: Model emitted at least one tool call.; Model called an expected tool.; All required tool calls were present.; No known hallucinated/forbidden tool names.; At least one tool response was returned.; Final answer was produced after tool output.

### ollama_chat/gemma4:12b / prometheus_query_graphable / trial 1

- Score: 9/10
- Latency: 300.023s
- Function calls: list_datasources, query_prometheus, query_prometheus
- Function responses: list_datasources, query_prometheus, query_prometheus
- Error: Timed out after 300s
- Final text: none
- Notes: Run errored before completion.; Model emitted at least one tool call.; Model called an expected tool.; All required tool calls were present.; No known hallucinated/forbidden tool names.; At least one tool response was returned.
