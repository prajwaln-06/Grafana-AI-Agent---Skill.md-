# Grafana Model Evaluation

## Project Goals

1. Chat with Grafana data through grounded tool calls.
2. Produce graphable metrics/log outputs for quick visual summaries.
3. Support operator workflows with safe discovery, troubleshooting, and human review points.

## Leaderboard

| Rank | Model | Score | Tool successes | Errors | Avg latency |
|---:|---|---:|---:|---:|---:|
| 1 | `ollama_chat/qwen2.5:14b` | 1/10 (10.0%) | 0/1 | 1 | 20.11s |

## Detailed Runs

### ollama_chat/qwen2.5:14b / datasource_discovery / trial 1

- Score: 1/10
- Latency: 20.108s
- Function calls: none
- Function responses: none
- Error: Timed out after 20s
- Final text: none
- Notes: Run errored before completion.; No tool call emitted.; No expected tool was called.; Missing required tool(s): list_datasources.; No known hallucinated/forbidden tool names.; No tool response returned.
