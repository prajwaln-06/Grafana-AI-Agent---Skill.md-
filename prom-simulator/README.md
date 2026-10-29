# Prometheus Metrics Simulator (node_exporter + DCGM)

Simulates a fleet of nodes, each exposing the exact metric names/types from
your two lists — **node_exporter** (CPU, load, memory, swap, filesystem) and
**DCGM** (GPU utilization, memory, temperature, power, clocks, PCIe/NVLink,
ECC/reliability) — as a real `/metrics` HTTP endpoint that any Prometheus
server can scrape. Every CPU core and GPU is indexed (`cpu="0"`, `gpu="0"`,
`gpu="1"`, ...), and every node is its own scrape target, so this behaves
like a real multi-node/multi-GPU cluster for dashboarding, alerting, and
PromQL practice — without needing real hardware.

Values aren't just random noise: GPU utilization drives power draw,
temperature, and clock speed together; CPU time is split realistically
across `user/system/idle/iowait/...` so `rate()` queries behave correctly;
disks slowly fill; rare ECC/NVLink error events fire occasionally.

## Files

| File | Purpose |
|---|---|
| `exporter.py` | Core simulator — runs **one** node, serves `/metrics` |
| `launch_cluster.py` | Spins up N nodes as local processes + generates a Prometheus scrape config |
| `Dockerfile` / `requirements.txt` | Container build for a single node |

> The simulator no longer ships its own Compose file or Prometheus config.
> The nodes and their scrape job live in the repository root stack
> (`docker-compose.yml` + `prometheus/prometheus.yml`) — see Option C.

## Option A — Quick local run (no Docker), multiple nodes

```bash
pip install -r requirements.txt
python3 launch_cluster.py --num-nodes 4 --base-port 9200 --num-cpus 32 --num-gpus 8
```

This starts 4 node processes on ports 9200-9203 and writes
`prometheus.generated.yml`. Point a real Prometheus at it:

```bash
prometheus --config.file=prometheus.generated.yml
```

Or add its `scrape_configs` block into your team's existing Prometheus config.

Useful flags on `launch_cluster.py`: `--num-nodes`, `--base-port`,
`--num-cpus`, `--num-gpus`, `--mem-total-gb`, `--swap-total-gb`,
`--tick-seconds` (how often values update, default 2s).

## Option B — Single node, direct

```bash
python3 exporter.py --node-id gpu-node-01 --port 9200 --num-cpus 32 --num-gpus 8
curl localhost:9200/metrics
```

## Option C — Docker Compose (recommended for shared team use)

The simulator runs as part of the **root** stack, so there is a single
Prometheus for the whole project:

```bash
# from the repository root
docker compose up -d --build
```

That brings up the 4 simulated nodes (`node-00` … `node-03`, with different
CPU/GPU counts so heterogeneous-fleet queries can be tested) together with
Prometheus at `http://localhost:9090`, which scrapes them under the
`simulated_fleet` job. The node ports (container port `9200`) are **not**
published to the host — Prometheus reaches them by compose service name over
the internal network.

To add/remove nodes or change `NUM_CPUS`/`NUM_GPUS` per node, edit the
`node-00`…`node-03` services in the root `docker-compose.yml` and add the
matching target to the `simulated_fleet` job in `prometheus/prometheus.yml`,
then `docker compose up -d --build` again.

## Example PromQL to try

```promql
# CPU busy % per core
100 - (rate(node_cpu_seconds_total{mode="idle"}[1m]) * 100)

# Memory used %
100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)

# GPU utilization by node and GPU index
DCGM_FI_DEV_GPU_UTIL

# GPU memory used %
100 * DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE)

# PCIe TX bandwidth
rate(DCGM_FI_PROF_PCIE_TX_BYTES[1m])

# Any GPUs that have retired pages (hardware degradation signal)
DCGM_FI_DEV_RETIRED_PENDING > 0
```

## Notes / assumptions

- All metrics from both lists in the source doc are implemented, with the
  Prometheus types (`Gauge`/`Counter`) matching your table.
- `node_id` is a scrape-time label (set in the Prometheus config), matching
  how Prometheus adds `instance`/custom labels in real deployments — it's
  not baked into the metric itself, same as real node_exporter/dcgm-exporter.
- Defaults are 32 CPUs / 8 GPUs / 256GB RAM per node — edit the CLI flags,
  env vars, or `docker-compose.yml` to match whatever fleet shape you want
  to simulate (including nodes with 0 GPUs, as in `node-03`).
- This is a **simulator for testing dashboards/alerts/queries** — it does
  not read real hardware and should not be scraped alongside production
  exporters on the same job name.
