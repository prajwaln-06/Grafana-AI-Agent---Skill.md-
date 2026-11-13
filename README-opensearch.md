# OpenSearch simulator, correlated with the Prometheus simulator

A CPU spike in Prometheus shows up in OpenSearch, and an OpenSearch event shows up in
Prometheus, because **neither side decides anything on its own**. Both read one shared
event timeline:

```
                 POST /events  (you, the team, or chaos mode)
                        |
                +---------------+
                | sim-controller|   the single source of truth
                +---------------+
                  |           |
   every 2s: effects          |  every 1s: "log intents"
                  v           v
     exporters (node-00..03)   log-generator
       Prometheus metrics        OpenSearch documents
       move on that node         (controllerlog, log_iml,
                                  slingshot_crayfabrichealth)
```

Start from either vocabulary (a metric-flavoured event like `cpu_spike` or a log-flavoured
one like `fabric_link_down`): every event projects onto both sides.

## What changed in your project

| File | Status |
|---|---|
| `exporter.py` | **replaced** (adds controller sync + effects; still works standalone) |
| `launch_cluster.py` | **replaced** (adds optional `--controller-url`) |
| `sim_controller/`, `log_generator/`, `config/` | new |
| `docker-compose.opensearch.yml` | new |
| `docker-compose.yml`, `prometheus.docker.yml`, `Dockerfile`, `requirements.txt` | **unchanged. Do not overwrite your copies**: `prometheus.docker.yml` holds your Grafana Cloud `remote_write` credentials |

## Steps

1. Unzip over your project folder (it only adds files and replaces the two above).
2. **Linux only:** `sudo sysctl -w vm.max_map_count=262144` (OpenSearch requires it; Docker Desktop on Windows/Mac handles this itself).
3. Start everything (the `--build` matters: the node images contain the new `exporter.py`):
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.opensearch.yml up -d --build
   ```
   Expect about 2 GB of extra RAM. Give OpenSearch ~1 minute to come up.
4. Open:
   - Swagger UI for the controller: http://localhost:8000/docs
   - OpenSearch Dashboards: http://localhost:5601 (the log-generator creates the index patterns on startup; if they are missing, create `controllerlog-*`, `log_iml-*` and `slingshot_crayfabrichealth-*` with time field `@timestamp`)
   - OpenSearch API: http://localhost:9250
5. Inject an event and watch both sides:
   ```bash
   curl -X POST localhost:8000/events -H 'Content-Type: application/json' \
     -d '{"type":"cpu_spike","node":"node-00","duration_s":240}'
   ```
   - **Prometheus / Grafana:** `100 - avg by (node_id)(rate(node_cpu_seconds_total{mode="idle"}[1m]))*100`
     goes to ~95% for node-00 only. Load average climbs (1m first, 15m slowly).
   - **OpenSearch Dashboards -> Discover**, index `log_iml-*`, query `sim.event_id:*` : iLO
     "System Overheating (... Location CPU ...)" entries from `leif-login01-bmc` at onset, every
     30 s while it lasts, and a "cleared" entry at the end.

   Use durations of **2 minutes or more**: your Prometheus scrapes about once a minute, so
   shorter events can fall between scrapes.

## Event reference

`GET /event-types` returns this live. Every event has `duration_s`, `ramp_s`, `start_in_s`.

| type | Prometheus effect | OpenSearch effect |
|---|---|---|
| `cpu_spike` (`intensity`, `cores`) | idle rate drops, load1/5/15 climb, ctx switches/interrupts rise | `log_iml`: iLO IML "System Overheating (CPU)" -> repeats -> cleared |
| `gpu_thermal_throttle` (`gpu`, omit = all) | GPU temp/mem temp up, power near cap, SM clock down, power-violation counter grows | `log_iml`: IML "System Overheating (Accelerator n)" per GPU -> cleared |
| `gpu_ecc_error` (`gpu`, `kind` sbe/dbe, `count`) | ECC SBE/DBE counters rise by exactly `count`; DBE leaves `RETIRED_PENDING` set | `log_iml`: IML memory-error entries; their `Count` fields sum to `count` |
| `fabric_link_down` | PCIe TX/RX collapse ~95%, GPU util sags | `controllerlog`: hmscd link_down, kernel `sbl` faults, Rosetta ASIC errors, link_up on recovery. `slingshot_crayfabrichealth`: the node's switch goes DEGRADED, then HEALTHY |

Other endpoints: `GET /events`, `GET /events/{id}`, `DELETE /events/{id}` (cancel, metrics
recover immediately), `POST /admin/reset` (clears events and the persistent ECC /
retired-page counters), `GET /nodes`.

**Chaos mode** (default on, about one event per 10 minutes, `CHAOS_MEAN_INTERVAL_S`) keeps
the system lively. For fully deterministic tests: `CHAOS_ENABLED=false`.

## How node-00 maps to log hosts

`config/topology.yaml` (hostnames come from your exports):

| Prometheus node | iLO host (`log_iml`) | Switch (`controllerlog`, fabric health) |
|---|---|---|
| node-00 | leif-login01-bmc | x1000c4r1b0 |
| node-01 | leif-login02-bmc | x1000c6r3b0 |
| node-02 | leif-login03-bmc | x1000c4r5b0 |
| node-03 | fmn01-bmc | x1000c2r7b0 |

## Backfill past data (choose the timestamps)

Fill the last days with history so queries and dashboards have something to look at. It
replays a past window with the **same code** as live generation: background noise across the
whole window, plus events at times you choose and/or random ones.

```bash
# 6 days of history, 6 random events per day
docker compose -f docker-compose.yml -f docker-compose.opensearch.yml exec log-generator \
    python3 backfill.py --days 6

# events at exactly the times you pick (see config/backfill_events.example.json)
docker compose -f docker-compose.yml -f docker-compose.opensearch.yml exec log-generator \
    python3 backfill.py --days 6 --events-file /config/backfill_events.example.json
```

Add `--dry-run` first to print the plan without writing anything.

Events file: a JSON list. `start` accepts ISO-8601 (`2026-11-01T14:30:00Z`), epoch seconds, or
relative (`-90m`, `-2d`, `-1d6h`). Other fields are the same as `POST /events`:

```json
[{"type": "fabric_link_down", "node": "node-01", "start": "-52h30m", "duration_s": 300},
 {"type": "gpu_ecc_error", "node": "node-00", "gpu": 3, "kind": "sbe", "count": 40, "start": "2026-11-01T14:30:00Z"}]
```

| Flag | Meaning |
|---|---|
| `--days N` / `--from X --to Y` | the window (default: last 3 days, ending now) |
| `--events-file F` | your events; random events are then off unless you also set `--events-per-day` |
| `--events-per-day N` | random events per day (default 6) |
| `--ambient-scale 0.25` | a quarter as much background noise (full scale is about 85k docs/day) |
| `--seed N` | same seed and same `--from/--to` = a re-run overwrites instead of duplicating |
| `--dry-run` | show the plan, write nothing |

Things to know:
- **Backfilled events exist only in OpenSearch.** Prometheus cannot be backfilled this way, so
  there are no metrics for those times. Every correlated backfilled document carries
  `sim.backfilled:true` so you can include or exclude them. Live events are unaffected.
- Run it **once, right after the first start**. Backfill covers the window up to *now*; if the
  live generator has been running for a while, that overlap gets double background noise and the
  iLO `Id`/`EventNumber` can interleave. Use `--to` to stop where live data begins.
- The window is clipped to the retention period (below), since older data would be deleted
  immediately.
- A bigger window takes longer (about 85k docs per day at full scale). Start with `--ambient-scale 0.25`.

## Retention (7 days)

Logs older than 7 days are deleted automatically: the log generator checks at startup and then
every hour. Change it with `RETENTION_DAYS` in `.env` (`0` = keep forever).

- Indices are **daily**, so deletion happens a whole day at a time. An index is removed once its
  date is 7 or more days old, which means no log is ever older than about 7 days, and the oldest
  day you see may hold 6 to 7 days of history.
- It deletes by the **date in the index name**, not with OpenSearch ISM. ISM counts from when an
  index was *created*, so a backfilled index for an old date, created today, would survive another
  week. Only `controllerlog-*`, `log_iml-*` and `slingshot_crayfabrichealth-*` daily indices are
  ever touched.

## Correlated vs ambient logs

- **Correlated** documents come from events and carry a `sim` field
  (`sim.event_id`, `event_type`, `phase`, `node`, `gpu`). Search `sim.event_id:<id>` to get
  every log line behind one event. Set `ADD_SIM_FIELD=false` for the exact production schema.
- **Ambient** documents (cron, systemd, iLO logins, switch heartbeats) have no `sim` field
  and are deliberately benign. Nothing in them implies a CPU/GPU/fabric problem, so any
  trouble you see in OpenSearch is also in Prometheus.
- While organic errors are off (the default with a controller), the ECC, NVLink-error and
  power-violation counters only move through events, so each one has a log line.
  Consequence: **the NVLink CRC/recovery counters stay at 0** (no log source maps to them).
  Set `ORGANIC_ERRORS=true` on the nodes if you want them to move without logs.

## Configuration

| Where | Knob | Default |
|---|---|---|
| controller env | `CHAOS_ENABLED`, `CHAOS_MEAN_INTERVAL_S`, `LOG_INTERVAL_S`, `SIM_API_KEY` | true, 600, 30, empty |
| log-generator env | `HEARTBEAT_S` (per switch), `AMBIENT_CONTROLLERLOG_PER_S`, `ILO_LOGIN_EVERY_S`, `ADD_SIM_FIELD`, `RETENTION_DAYS` | 60, 0.5, 60, true, 7 |
| `.env` file | `OPENSEARCH_VERSION` (Dashboards must match) | 2.19.4 |
| `config/topology.yaml` | node -> host/switch/port mapping, switch roster | see file |
| `config/log_catalog.yaml` | exact wording, severities, codes of every correlated log line | see file |

**Team access:** reads (`GET`) are always open. Set `SIM_API_KEY=<secret>` (in `.env`) and
`POST`/`DELETE` require header `X-API-Key: <secret>`: you hold the key, everyone else can
view. The exporters' internal sync path stays open. This is a simple shared secret, not
user accounts. OpenSearch itself runs with security **disabled**, so keep this on a trusted
network.

## Add a new event type (3 places)

1. `sim_controller/controller.py`: add it to `EVENT_TYPES` (+ effect in `node_effects` if it moves metrics).
2. `exporter.py`: apply the effect in `_tick_cpu` / `_tick_gpu` (read from `self.fx`).
3. `config/log_catalog.yaml`: add `onset` / `ongoing` / `clear` entries.

## Known approximations (please review)

- **Document structure and field types match your exports** (verified field-by-field), and iLO IML
  structure follows HPE's published Redfish examples. The `log_iml` hardware entries use the real
  IML shape with `Class`/`Code` placeholders and **invented wording** for the CPU/GPU thermal and
  ECC messages. Your samples only contained login/logout entries. Fix the wording in
  `log_catalog.yaml` to match your real logs. Each entry is tagged `[SAMPLE]`, `[HPE-DOC]` or `[APPROX]`.
- iLO has no "CPU utilisation" event, so a CPU spike is logged as the thermal entry it would
  plausibly cause. If your real stack has an OS syslog index for compute nodes, that is where CPU
  lines belong. Upload a sample and it can be added.
- Fabric `link_up` and the `DEGRADED` heartbeat state/text are extrapolated from your `link_down`
  and `HEALTHY` samples.
- `@timestamp` is ingest time (about 0.5 s after the event), not the minutes-long batch lag in
  your exports, so correlation works in real time.
- Volume is deliberately light (about 1 doc/s total) versus your real ~300k controllerlog docs/day.
- iLO `Id` / `EventNumber` are derived from the entry's timestamp, so they stay in order across restarts and
  between backfill and live data. Right at the backfill-to-live seam they can differ by a few (cosmetic).
