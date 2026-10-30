# Token & Performance Benchmark Tests

Tests for measuring LLM token consumption (input/output tokens) and per-stage pipeline execution latency.

---

## Prerequisites

1. Set credentials in `.env`:
   ```bash
   LLM_PROVIDER=vertex
   VERTEX_API_KEY=<your-key>
   # or GEMINI_API_KEY if using AI Studio
   ```

2. Start Prometheus and node exporter simulators:
   ```bash
   docker start prom-simulator-prometheus-1 prom-simulator-node-00-1 prom-simulator-node-01-1 prom-simulator-node-02-1 prom-simulator-node-03-1
   ```

---

## How to Run

From the repository root:

```bash
python tests/token_benchmarks/run_catalog_improvements.py
```

Or run with custom arguments:

```bash
python tests/token_benchmarks/benchmark_pipeline.py --runs 3
```
