# prom-simulator

A **Docker-only, fake observability backend** for testing an agent (or a
human) that answers questions using PromQL and OpenSearch queries. It
simulates a 4-node GPU cluster: realistic Prometheus metrics, realistic
OpenSearch logs, and — the whole point of this project — **injected
incidents that show up correlated in both**, the way a real production
incident would.

Nothing in here is real. There's no actual hardware, no actual log
shipper. It's all generated in-process by small Python simulators running
in Docker containers. But the two systems you (or your agent) talk to
behave exactly like a real Prometheus server and a real OpenSearch
cluster, because they *are* one — just fed synthetic data instead of
scraping real machines.

---

## The only contract that matters

If you're building/testing an agent against this, it needs to know
exactly two URLs:

| | |
|---|---|
| **Prometheus** | `http://localhost:9090` |
| **OpenSearch**  | `http://localhost:9600` |

That's it. Query them with the normal Prometheus HTTP API
(`/api/v1/query`, `/api/v1/query_range`) and the normal OpenSearch REST
API (`/_cat/indices`, `/<index>/_search`, ...) — no custom endpoints, no
simulator-specific query language. Everything else described below
(node-00..03, the incident controller on :9500) is internal plumbing for
*you*, the person setting up and testing the scenario — your agent should
never need to know it exists.

```
                     ┌───────────────────────┐
                     │   YOUR AGENT           │
                     │   NL → query → answer  │
                     └──────────┬─────────────┘
                                │
                   ONLY THESE TWO URLs
                                │
                 ┌──────────────┴───────────────┐
                 ▼                               ▼
          Prometheus                        OpenSearch
          :9090                             :9600
                 ▲                               ▲
                 │                               │
           simulated metrics              simulated logs
                 │                               │
                 └───────────────┬───────────────┘
                                 │
                       Docker simulator (internal)
              incident-controller + node-00..03 + logsim
```

---

## Quick start

**Requires:** Docker Desktop (or Docker Engine + Compose V2), 4+ CPUs and
6-8 GB RAM allocated to Docker. Nothing else — no Python, no pip, no host
setup. `git clone`/unzip this repo, then:

### Windows (PowerShell)

```powershell
cd path\to\prom-simulator
.\start.ps1        # docker compose up -d --build, under the hood
.\health.ps1        # confirm every service is up AND has real data
.\validate.ps1      # full smoke test: injects a real incident, checks it
                     # shows up correlated in both Prometheus and OpenSearch
```

### macOS / Linux

```bash
cd path/to/prom-simulator
docker compose up -d --build

# equivalent of health.ps1:
curl -s http://localhost:9090/-/ready && echo " Prometheus OK"
curl -s http://localhost:9600/ | head -1 && echo " OpenSearch OK"
curl -s http://localhost:9500/health && echo " Controller OK"
```

First boot takes **1-2 minutes** — OpenSearch's JVM is the slow part.
`docker compose ps` should eventually show every service as `Up
(healthy)`.

Once it's up, your agent (or you, manually) can hit `:9090` and `:9600`
freely. There's nothing to break — see [Resetting state](#resetting-state)
below if you want a clean slate.

---

## Is it dynamic? How are the logs actually generated?

Yes, fully dynamic — nothing is pre-baked or replayed from a file.

There are two independent generators running continuously inside the
containers:

1. **Background noise**, on every node, all the time: heartbeats every
   10-20s, console lines every 25-60s, syslog entries (SSH logins, cron,
   kernel messages) every 8-25s — randomized content and timing, so the
   OpenSearch indices are never empty and never look synthetic-uniform.
2. **Incident-triggered events**: when someone (a test harness, you, a
   script) tells the internal **incident controller** "start scenario X
   on node Y," it becomes the single shared *cause* that both the metric
   side and the log side independently react to:
   - each node's Prometheus exporter polls the controller and, if it's
     affected, nudges its own random-walk metric targets (e.g. pushes a
     GPU's utilization target up) — the exporter's existing derived
     relationships (temperature/power/clocks computed from utilization)
     cascade naturally from there, it isn't just overwriting five metrics
     independently
   - the log generator (`logsim`) polls the same controller and schedules
     a handful of realistic log lines at *staggered, non-identical*
     offsets after the incident starts (e.g. "workload threshold
     exceeded" at +5s, "temperature warning" at +24s, "power warning" at
     +62s) into OpenSearch, using the same node identity

Neither side generates an incident on its own — they're both just
reacting to the same underlying state in the controller, the way a real
metric spike and a real log warning would both stem from the same actual
GPU overheating, not from each other. See
[Architecture](#architecture-how-correlation-actually-works) below for
the full picture, and `incidents/scenarios.py` for the exact scenario
definitions if you want to read the source.

---

## Node topology

| Node | CPUs | GPUs |
|---|---|---|
| node-00 | 32 | 8 |
| node-01 | 32 | 8 |
| node-02 | 64 | 4 |
| node-03 | 16 | 0 (GPU-less, on purpose — for negative-case testing) |

Every metric from a node carries a `node_id` label (`"node-00"` etc.) and
a `cluster="simulated"` label, injected by Prometheus at scrape time
(see `prometheus.docker.yml`). GPU metrics additionally carry `gpu`
(index, e.g. `"3"`) and `device` labels. This labeling is consistent
across every metric family, so PromQL like
`DCGM_FI_DEV_GPU_TEMP{node_id="node-02", gpu="3"}` always works the same
way regardless of which metric you're querying.

---

## Querying Prometheus

Standard API, nothing simulator-specific:

```bash
curl "http://localhost:9090/api/v1/query?query=up"
curl "http://localhost:9090/api/v1/query?query=node_load1"
curl "http://localhost:9090/api/v1/query?query=DCGM_FI_DEV_GPU_UTIL"
curl "http://localhost:9090/api/v1/query?query=DCGM_FI_DEV_GPU_TEMP%7Bnode_id%3D%22node-02%22%2Cgpu%3D%223%22%7D"

# range query
curl "http://localhost:9090/api/v1/query_range?query=node_load1&start=$(date -u -d '-10 min' +%s)&end=$(date -u +%s)&step=15s"
```

PowerShell:

```powershell
Invoke-RestMethod "http://localhost:9090/api/v1/query?query=up"
Invoke-RestMethod "http://localhost:9090/api/v1/query?query=DCGM_FI_DEV_GPU_UTIL"
```

**Full metric list** (all exposed with the labels described above):

`node_cpu_seconds_total`, `node_load1/5/15`, `node_memory_{MemTotal,
MemFree, MemAvailable, Buffers, Cached, SwapTotal, SwapFree}_bytes`,
`node_filesystem_{size,avail,free}_bytes`, `node_context_switches_total`,
`node_intr_total`, `node_heartbeat_ok`, and the DCGM/GPU family:
`DCGM_FI_DEV_GPU_UTIL`, `DCGM_FI_DEV_GPU_TEMP`, `DCGM_FI_DEV_MEMORY_TEMP`,
`DCGM_FI_DEV_POWER_USAGE`, `DCGM_FI_DEV_POWER_VIOLATION`,
`DCGM_FI_DEV_MEM_CLOCK`, `DCGM_FI_DEV_SM_CLOCK`,
`DCGM_FI_DEV_MEM_COPY_UTIL`, `DCGM_FI_DEV_FB_{FREE,USED}`,
`DCGM_FI_DEV_ECC_{SBE,DBE}_VOL_TOTAL`, `DCGM_FI_DEV_RETIRED_{SBE,DBE,
PENDING}`, `DCGM_FI_DEV_NVLINK_{CRC_FLIT,RECOVERY}_ERROR_COUNT_TOTAL`,
plus a handful of `DCGM_FI_PROF_*` profiling gauges.

## Querying OpenSearch

Also the standard REST API:

```bash
curl "http://localhost:9600/_cat/indices?v"

curl -X POST "http://localhost:9600/syslog-*/_search" \
  -H "Content-Type: application/json" \
  -d '{"size": 20, "query": {"match": {"Body": "GPU"}}}'

curl -X POST "http://localhost:9600/syslog-*/_search" \
  -H "Content-Type: application/json" \
  -d '{"size": 20, "query": {"bool": {"must": [
        {"term":  {"Resource.host.name": "node-02"}},
        {"match": {"Body": "GPU"}}
      ]}}}'
```

**Indices**: `syslog-YYYY.MM.DD` (kernel/sshd/dcgm-exporter/clmgr
messages — this is where GPU/memory/filesystem/ECC incident evidence
ends up), `consolelog-YYYY.MM.DD` (console/tty stream), `heartbeat`
(liveness pings, not date-rotated). All three are pre-created at startup
with an explicit mapping so they're queryable immediately, even before
the first document lands.

**Document schema** (identical shape across all three indices):

```json
{
  "@timestamp": "2026-08-14T00:10:20.123Z",
  "Severity": "WARN",
  "Body": "GPU 3 temperature warning on node-02",
  "Resource": { "host.name": "node-02", "service.name": "dcgm-exporter" },
  "Attributes": { "...": "..." },
  "@version": "1",
  "Timestamp": "2026-08-14T00:10:20.123Z"
}
```

`Resource.host.name`, `Resource.service.name`, and `Severity` are mapped
as `keyword` (exact match, not tokenized) — a `term` query for
`"node-02"` works correctly rather than silently matching nothing because
the analyzer split it into `"node"`/`"02"`. `Body` is `text`, for full-text
`match` queries.

---

## Scenarios (internal test-control only)

The incident controller at `http://localhost:9500` is **not** one of the
two agent-facing endpoints — it's how *you* (or a test harness) tell the
simulator "make something happen." Mark it clearly as internal in any
docs/harness you build on top of this.

```bash
curl -X POST "http://localhost:9500/scenarios/trigger" \
  -H "Content-Type: application/json" \
  -d '{"scenario_id":"gpu_overheating","node":"node-02","gpu":3,"start_after":10,"duration":120}'
```

```powershell
Invoke-RestMethod -Method POST -Uri "http://localhost:9500/scenarios/trigger" `
  -ContentType "application/json" `
  -Body '{"scenario_id":"gpu_overheating","node":"node-02","gpu":3,"start_after":10,"duration":120}'
```

| Scenario | Required args | Metric side | Log side |
|---|---|---|---|
| `gpu_overheating` | `node`, `gpu` | GPU util pushed to ~97% (temp/power/clocks cascade from it) | workload threshold / temp warning / power warning, staggered 5s/24s/62s after start |
| `gpu_hardware_degradation` | `node`, `gpu` | elevated + one guaranteed ECC/retired-page event | ECC error / page retirement / hardware warning, 6s/19s/41s |
| `memory_pressure` | `node` | MemAvailable & SwapFree pushed down, load up | pressure / allocation / swap warnings, 4s/15s/33s |
| `filesystem_pressure` | `node`, optional `mount` | fill-rate spike on a mount (or all mounts) | low-space / disk warnings, 7s/28s |
| `node_heartbeat_failure` | `node` | `node_heartbeat_ok` flips to 0; heartbeats stop | missed / console-lost / unavailable, 10s/11s/40s |
| `ssh_auth_burst` | `node` | none (intentionally metric-silent) | burst of failed-auth events, 2s/9s/16s/23s |

Every scenario recovers on its own: metric effects only apply for
`duration` seconds, after which the node's random walk drifts back
toward its normal baseline — you'll see `normal → anomaly → recovery`,
not a permanent stuck value.

### Resetting state

```bash
curl -X POST http://localhost:9500/scenarios/reset
```
```powershell
.\reset.ps1
```

Clears every active/future scenario — on **both** sides: exporter.py's
metric effects revert immediately, and any already-scheduled-but-not-yet-
fired OpenSearch log events in `logsim` are dropped too, so nothing stale
fires later. Containers, Prometheus, and OpenSearch data are untouched;
this is for cleanly separating "test A" from "test B" without restarting
Docker.

---

## Fairness / design guarantees (why this is a fair test)

- **No answer leakage**: internal coordination fields like `scenario_id`
  and `instance_id` are never written into a metric label or a log's
  `Body`/`Resource`/`Attributes`. An agent (or you) can only correlate
  using node, component (gpu/mount), time, service, severity, and log
  content — the same signals you'd have against a real system.
- **Signal + noise**: background heartbeat/console/syslog activity keeps
  running on every node the whole time, incidents on other nodes stay
  invisible, and even the affected node's own unrelated GPUs/filesystems
  stay normal. Querying `*` never returns an empty or all-incident
  database.
- **Non-identical timestamps**: an incident's log events fire at
  staggered, realistic offsets from the metric anomaly, never at the
  exact same instant, so time-window reasoning is required rather than
  exact-timestamp matching.

---

## Validation

```powershell
.\validate.ps1
```

Checks, in order: infra reachability (Prometheus/OpenSearch/controller/
all exporters/logsim), that Prometheus actually has node/GPU/memory/
filesystem metrics (not just that it's up), that OpenSearch actually has
documents in all three indices, then **injects a real
`gpu_overheating` incident** and confirms it's independently observable
through both `:9090` and `:9600` before resetting state and printing a
final `SIMULATOR READY` / `SIMULATOR VALIDATION FAILED` banner.

There's also a fast, Docker-free regression test used during development
(`python3 tools/validate_correlation.py`, runs entirely in-process against
an in-memory sink) — see [Developer utilities](#developer-utilities-not-required-for-normal-use).

---

## Architecture (how correlation actually works)

```
                          incident-controller  (:9500, internal)
                        the shared "common cause"
                              /        \
                    poll active        poll active
                    incidents for       incidents
                    my node             (all nodes)
                          /                  \
                 exporter.py (x4)          logsim
              nudges existing random-   schedules + emits
              walk targets so derived   correlated log events
              metrics (temp/power/      + continuous background
              clocks) cascade           noise
              naturally
                    |                          |
                    v                          v
                Prometheus                 OpenSearch
                 :9090 (public)             :9600 (public)
```

- **Shared clock**: everything uses real UTC wall-clock time (all
  containers share the host clock), so no separate simulated-time
  machinery is needed for Prometheus and OpenSearch timestamps to be
  directly correlatable.
- **exporter.py changes are additive**: incidents nudge existing
  random-walk *targets*, never overwrite derived values directly. The one
  wholly new metric is `node_heartbeat_ok` (needed for the node-failure
  scenario, since nothing else represented liveness).
- **Sinks are pluggable** (`logsim/sinks.py`): `opensearch` for the real
  Docker stack, `file`/`memory` for local development and automated
  tests without a running cluster.

---

## Resource requirements

- Docker Desktop: **4+ CPUs, 6-8 GB RAM** allocated (OpenSearch's JVM
  alone is capped at 512MB heap here, but the JVM + 4 Python exporters +
  Prometheus add up). Under-provisioned Docker will cause OpenSearch to
  fail to start or get OOM-killed with a confusing error — if
  `.\health.ps1` never goes green, check Docker Desktop's resource
  settings first.
- Ports used on the host: `9090` (Prometheus), `9600` (OpenSearch),
  `9500` (incident controller, internal), `9200-9203` (node exporters,
  internal). Free these up or edit `docker-compose.yml` if they clash
  with something else on your machine.

---

## Repository structure

```
prom-simulator/
├── docker-compose.yml         # the whole stack; healthchecked + pinned versions
├── prometheus.docker.yml
├── Dockerfile                 # node exporter image
├── Dockerfile.controller      # incident controller image
├── Dockerfile.logsim          # log generator image
├── requirements.txt / requirements-logsim.txt
│
├── exporter.py                # per-node simulated Prometheus exporter
├── incidents/
│   ├── controller.py          # shared incident state (stdlib HTTP, :9500)
│   ├── controller_client.py
│   └── scenarios.py           # the 6 scenario definitions
├── logsim/
│   ├── main.py                # log generator entrypoint
│   ├── simulator.py           # background noise + incident-scheduled logs
│   ├── sinks.py                # OpenSearch / file / memory sinks + mapping
│   └── log_templates.py
│
├── start.ps1 / stop.ps1 / health.ps1 / reset.ps1 / validate.ps1
│
└── tools/                     # developer utilities, NOT required to start the stack
    ├── trigger_scenario.py
    ├── query_logs.py
    └── validate_correlation.py
```

## Developer utilities (not required for normal use)

`launch_cluster.py` and everything in `tools/` are host-Python
conveniences for people working *on* the simulator itself — they are
**not** part of the normal `docker compose up` startup path and you don't
need Python installed to use the simulator as an agent-testing backend.

```bash
pip install -r requirements-logsim.txt   # only if you want to run tools/query_logs.py --backend opensearch, or logsim locally
python3 tools/trigger_scenario.py --list
python3 tools/query_logs.py --backend opensearch --url http://localhost:9600 --node node-02
python3 tools/validate_correlation.py    # fast, in-process, no Docker needed
```

---

## Assumptions & limitations

- **Docker was authored but not build-tested against a live daemon in
  the environment this was built in** (no `docker` binary, no registry
  access there). All logic — controller, scenario effects, log
  scheduling, sinks, reset/epoch handling, CLI tools — was validated
  in-process instead (`tools/validate_correlation.py`, 28/28 checks) and
  against a real on-disk `FileSink`. **Please run `.\health.ps1` and
  `.\validate.ps1` after your first `docker compose up --build`** to
  confirm everything works in your actual Docker environment; report
  back anything that doesn't match this README.
- The PowerShell scripts likewise couldn't be executed in that sandbox
  (no `pwsh`) — they were written carefully and reviewed by hand, but
  treat `.\validate.ps1` passing as the real confirmation they work.
- OpenSearch runs with `DISABLE_SECURITY_PLUGIN=true` for local-simulator
  convenience — don't reuse this compose file for anything
  internet-facing.
- Only one incident per (node, GPU) or (node, mount) is meaningfully
  supported at a time.
- `filesystem_pressure`'s fill-rate multiplier is tuned to be clearly
  visible within a ~20-30s window rather than modeling a realistic
  real-world multi-hour fill; adjust `fs_fill_rate_multiplier` in
  `incidents/scenarios.py` for slower, longer-running demos.
