#!/usr/bin/env python3
"""
Backfill historical logs into OpenSearch with chosen timestamps.

It replays a past time window using the SAME document builders as the live generator:
  * ambient background noise across the whole window (heartbeats, cron, iLO logins...)
  * events at the times you choose (from a file) and/or randomly placed
    -> rendered exactly like live events (onset / ongoing / clear, DEGRADED heartbeats...)

IMPORTANT: backfilled events exist ONLY in OpenSearch. Prometheus cannot be backfilled
this way, so there are no matching metrics for those times. Every backfilled correlated
document is tagged sim.backfilled:true so you can include or exclude them.

Run inside the stack:
  docker compose -f docker-compose.yml -f docker-compose.opensearch.yml exec log-generator \\
      python3 backfill.py --days 6

Times accept ISO-8601 (2026-11-01T14:30:00Z), epoch seconds, or relative (-90m, -2d, -1d6h).
Re-running with the same --seed and the same --from/--to overwrites rather than duplicates.
"""
import argparse
import hashlib
import heapq
import itertools
import json
import random
import re
import sys
import time
from datetime import datetime, timezone

import requests

import log_generator as lg

# Keep in sync with sim_controller/controller.py EVENT_TYPES["defaults"].
DEFAULTS = {
    "cpu_spike": {"duration_s": 120, "intensity": 0.95},
    "gpu_thermal_throttle": {"duration_s": 180, "intensity": 1.0},
    "gpu_ecc_error": {"duration_s": 60, "intensity": 1.0},
    "fabric_link_down": {"duration_s": 120, "intensity": 1.0},
}
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


# --------------------------------------------------------------------------
# Time parsing
# --------------------------------------------------------------------------
def parse_time(value, now_ts):
    """ISO-8601, epoch seconds, or relative like -90m / -2d / -1d6h."""
    if isinstance(value, (int, float)):
        return float(value)
    v = str(value).strip()
    m = re.fullmatch(r"-((?:\d+[smhd])+)", v)
    if m:
        secs = sum(int(n) * UNITS[u] for n, u in re.findall(r"(\d+)([smhd])", m.group(1)))
        return now_ts - secs
    try:
        return float(v)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"cannot parse time '{value}' (use ISO-8601, epoch seconds, or e.g. -2h)")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) + "Z"


# --------------------------------------------------------------------------
# Nodes (GPU counts) -- learned from the controller, or given with --gpus
# --------------------------------------------------------------------------
def load_nodes(gpus_arg):
    nodes = {}
    try:
        r = requests.get(f"{lg.CONTROLLER_URL}/nodes", timeout=5)
        r.raise_for_status()
        nodes = {n: {"num_gpus": i["num_gpus"]} for n, i in r.json().items()}
    except Exception as exc:
        if not gpus_arg:
            sys.exit(f"ERROR: cannot reach the controller at {lg.CONTROLLER_URL} ({exc!r}).\n"
                     "It is needed to learn each node's GPU count. Start the stack, or pass\n"
                     "--gpus node-00=8,node-01=8,node-02=4,node-03=0")
    if gpus_arg:
        for part in gpus_arg.split(","):
            name, n = part.split("=")
            nodes[name.strip()] = {"num_gpus": int(n)}
    if not nodes:
        sys.exit("ERROR: no nodes known yet (exporters register on their first sync). Wait a few "
                 "seconds and retry, or pass --gpus.")
    return nodes


# --------------------------------------------------------------------------
# Events: validate, generate, expand into log intents
# --------------------------------------------------------------------------
def normalize(raw, i, nodes, now_ts):
    """Validate one event from the events file. Returns a dict or raises ValueError."""
    where = f"event #{i + 1}"
    t = raw.get("type")
    if t not in DEFAULTS:
        raise ValueError(f"{where}: unknown type '{t}' (valid: {sorted(DEFAULTS)})")
    node = raw.get("node")
    if node not in nodes:
        raise ValueError(f"{where}: unknown node '{node}' (known: {sorted(nodes)})")
    if "start" not in raw:
        raise ValueError(f"{where}: missing 'start'")
    n_gpus = nodes[node]["num_gpus"]
    gpu = raw.get("gpu")
    if t == "gpu_ecc_error" and gpu is None:
        raise ValueError(f"{where}: gpu_ecc_error requires 'gpu'")
    if t in ("gpu_ecc_error", "gpu_thermal_throttle") and n_gpus == 0:
        raise ValueError(f"{where}: node '{node}' has no GPUs")
    if gpu is not None and not (0 <= int(gpu) < n_gpus):
        raise ValueError(f"{where}: gpu must be 0..{n_gpus - 1} on '{node}'")
    if t in ("cpu_spike", "fabric_link_down"):
        gpu = None
    d = DEFAULTS[t]
    ev = {"type": t, "node": node, "gpu": None if gpu is None else int(gpu),
          "start_ts": parse_time(raw["start"], now_ts),
          "duration_s": float(raw.get("duration_s", d["duration_s"])),
          "intensity": float(raw.get("intensity", d["intensity"])), "params": {}}
    if ev["duration_s"] <= 0:
        raise ValueError(f"{where}: duration_s must be > 0")
    if t == "gpu_ecc_error":
        kind = raw.get("kind", "sbe")
        if kind not in ("sbe", "dbe"):
            raise ValueError(f"{where}: kind must be 'sbe' or 'dbe'")
        ev["params"] = {"kind": kind, "count": int(raw.get("count", 25 if kind == "sbe" else 1))}
    return ev


def random_events(n, t0, t1, nodes):
    """Chaos-style events spread uniformly over [t0, t1]."""
    gpu_nodes = [k for k, v in nodes.items() if v["num_gpus"] > 0]
    out, tries = [], 0
    while len(out) < n and tries < n * 20:
        tries += 1
        t = random.choice(["cpu_spike", "cpu_spike", "fabric_link_down", "fabric_link_down",
                           "gpu_thermal_throttle", "gpu_ecc_error"])
        if t in ("gpu_thermal_throttle", "gpu_ecc_error") and not gpu_nodes:
            continue
        node = random.choice(gpu_nodes if t.startswith("gpu") else sorted(nodes))
        ng = nodes[node]["num_gpus"]
        ev = {"type": t, "node": node, "gpu": None, "intensity": DEFAULTS[t]["intensity"], "params": {}}
        if t == "cpu_spike":
            ev.update(duration_s=random.uniform(90, 300), intensity=random.uniform(0.75, 1.0))
        elif t == "gpu_thermal_throttle":
            ev.update(gpu=random.randrange(ng), duration_s=random.uniform(120, 300))
        elif t == "gpu_ecc_error":
            dbe = random.random() < 0.1
            ev.update(gpu=random.randrange(ng), duration_s=60.0,
                      params={"kind": "dbe" if dbe else "sbe",
                              "count": 1 if dbe else random.randint(5, 60)})
        else:
            ev.update(duration_s=random.uniform(90, 240))
        if t0 + ev["duration_s"] >= t1:
            continue
        ev["start_ts"] = random.uniform(t0, t1 - ev["duration_s"])
        if any(o["node"] == node and o["type"] == t and
               o["start_ts"] < ev["start_ts"] + ev["duration_s"] and
               ev["start_ts"] < o["start_ts"] + o["duration_s"] for o in out):
            continue
        out.append(ev)
    return out


def expand(ev, nodes, log_interval, run_id, seq):
    """Turn one event into the same onset/ongoing/clear intents the live controller emits."""
    start, end = ev["start_ts"], ev["start_ts"] + ev["duration_s"]

    def mk(phase, ts, gpu, **extra):
        return {"seq": next(seq), "boot_id": run_id, "ts": ts, "event_id": ev["id"],
                "event_type": ev["type"], "phase": phase, "node": ev["node"], "gpu": gpu,
                "intensity": ev["intensity"], "params": ev["params"], **extra}

    intents = []
    if ev["type"] == "gpu_ecc_error":
        total, logged = ev["params"]["count"], 0
        times = [start]
        k = 1
        while start + k * log_interval < end:
            times.append(start + k * log_interval)
            k += 1
        times.append(end)
        for t in times:
            done = t >= end
            cum = total if done else max(1, int(total * (t - start) / ev["duration_s"]))
            if cum - logged > 0:
                intents.append(mk("onset" if logged == 0 else "ongoing",
                                  start if logged == 0 else t, ev["gpu"],
                                  count=cum - logged, total=cum))
                logged = cum
        return intents

    if ev["gpu"] is not None:
        gpus = [ev["gpu"]]
    elif ev["type"] == "gpu_thermal_throttle":
        gpus = list(range(nodes[ev["node"]]["num_gpus"]))
    else:
        gpus = [None]
    phases = [("onset", start)]
    k = 1
    while start + k * log_interval < end:
        phases.append(("ongoing", start + k * log_interval))
        k += 1
    phases.append(("clear", end))
    for phase, ts in phases:
        for g in gpus:
            intents.append(mk(phase, ts, g))
    return intents


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------
def flush_checked(force=False):
    """Flush the bulk queue; abort loudly if OpenSearch keeps refusing (never silently drop)."""
    for attempt in range(6):
        lg.BULK.flush()
        if not lg.BULK.q:
            return
        time.sleep(2)
    sys.exit(f"ERROR: OpenSearch kept rejecting bulk writes ({len(lg.BULK.q)} docs unsent). Aborting.")


def main():
    ap = argparse.ArgumentParser(description="Backfill historical logs with chosen timestamps",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--days", type=float, default=3, help="window = last N days (default 3)")
    ap.add_argument("--from", dest="t_from", help="window start (overrides --days)")
    ap.add_argument("--to", dest="t_to", help="window end (default: now)")
    ap.add_argument("--events-file", help='JSON list of events: [{"type","node","start",...}]')
    ap.add_argument("--events-per-day", type=float, default=None,
                    help="random events per day (default 6; 0 when --events-file is given)")
    ap.add_argument("--ambient-scale", type=float, default=1.0,
                    help="scale background volume, e.g. 0.25 = a quarter as many docs (default 1)")
    ap.add_argument("--log-interval", type=float, default=30, help="'ongoing' log cadence (s)")
    ap.add_argument("--seed", type=int, help="fix the randomness; same seed+window = idempotent re-run")
    ap.add_argument("--gpus", help="node GPU counts if the controller is unreachable: node-00=8,node-03=0")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    args = ap.parse_args()

    now_ts = time.time()
    t_end = min(parse_time(args.t_to, now_ts), now_ts) if args.t_to else now_ts
    t_start = parse_time(args.t_from, now_ts) if args.t_from else t_end - args.days * 86400
    if t_start >= t_end:
        sys.exit("ERROR: window start must be before its end")

    keep_from = lg.retention_keep_from()
    if keep_from is not None and t_start < keep_from:
        print(f"NOTE: retention is {lg.RETENTION_DAYS} days, so data before {fmt_ts(keep_from)} would be "
              f"deleted straight away.\n      Window start clipped from {fmt_ts(t_start)} to {fmt_ts(keep_from)}.")
        t_start = keep_from
        if t_start >= t_end:
            sys.exit("ERROR: nothing left in the window after clipping to the retention period")

    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1, 2 ** 31)
    random.seed(seed)
    run_id = f"bf-{seed}"
    nodes = load_nodes(args.gpus)

    # ---- events: from file (chosen timestamps) + random -------------------------------
    events, problems = [], []
    if args.events_file:
        with open(args.events_file) as f:
            raw = json.load(f)
        raw = raw["events"] if isinstance(raw, dict) else raw
        for i, r in enumerate(raw):
            try:
                events.append(normalize(r, i, nodes, now_ts))
            except ValueError as exc:
                problems.append(str(exc))
    if problems:
        sys.exit("ERROR in events file:\n  " + "\n  ".join(problems))
    per_day = args.events_per_day if args.events_per_day is not None else (0 if args.events_file else 6)
    n_random = round(per_day * (t_end - t_start) / 86400)
    events += random_events(n_random, t_start, t_end, nodes)

    kept = []
    for ev in events:
        end = ev["start_ts"] + ev["duration_s"]
        if ev["start_ts"] < t_start or ev["start_ts"] >= t_end:
            print(f"SKIPPED {ev['type']} on {ev['node']} at {fmt_ts(ev['start_ts'])}: starts outside the window")
            continue
        if end > t_end:
            print(f"NOTE: {ev['type']} on {ev['node']} clipped to end at the window end")
            ev["duration_s"] = t_end - ev["start_ts"]
        kept.append(ev)
    events = sorted(kept, key=lambda e: e["start_ts"])
    for i, ev in enumerate(events):
        ev["id"] = hashlib.sha1(f"{run_id}-ev{i}".encode()).hexdigest()[:10]

    # ---- plan --------------------------------------------------------------------------
    print(f"\nBackfill window : {fmt_ts(t_start)}  ->  {fmt_ts(t_end)}  ({(t_end - t_start) / 86400:.2f} days)")
    print(f"Seed / run id   : {seed} / {run_id}   (reuse --seed with the same window to re-run idempotently)")
    print(f"Ambient scale   : {args.ambient_scale}")
    print(f"Events          : {len(events)}")
    for ev in events:
        g = "" if ev["gpu"] is None else f" gpu{ev['gpu']}"
        print(f"   {fmt_ts(ev['start_ts'])}  {ev['duration_s']:>5.0f}s  {ev['type']:<22} {ev['node']}{g}  id={ev['id']}")
    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    # ---- replay ------------------------------------------------------------------------
    lg.wait_for_opensearch()
    lg.install_templates()
    lg.BACKFILL_RUN = run_id
    lg.HEARTBEAT_S /= max(args.ambient_scale, 1e-6)
    lg.ILO_LOGIN_EVERY_S /= max(args.ambient_scale, 1e-6)
    lg.AMBIENT_CONTROLLERLOG_PER_S *= args.ambient_scale

    per_index = {}
    orig_add = lg.BULK.add

    def counting_add(index, doc, _id=None):
        per_index[index] = per_index.get(index, 0) + 1
        orig_add(index, doc, _id)
    lg.BULK.add = counting_add

    lg.set_virtual_now(t_start)
    lg.seed_ambient(t_start)
    seq = itertools.count(1)
    n_intents = 0
    for ev in events:
        for it in expand(ev, nodes, args.log_interval, run_id, seq):
            if it["ts"] <= t_end:
                lg.schedule(it["ts"], lg.handle_intent, it)
                n_intents += 1

    print(f"\nReplaying {n_intents} event log intents + ambient noise ...")
    span, last_pct, started = t_end - t_start, -1, time.time()
    while lg._HEAP:
        due, _, fn, fargs = heapq.heappop(lg._HEAP)
        if due > t_end:
            break
        lg.set_virtual_now(due)
        fn(*fargs)
        if len(lg.BULK.q) >= 2000:
            flush_checked()
        pct = int(100 * (due - t_start) / span)
        if pct // 10 != last_pct // 10:
            last_pct = pct
            print(f"   {pct:3d}%  at {fmt_ts(due)}  docs indexed so far: {lg.BULK.sent + len(lg.BULK.q)}")
    flush_checked()

    print(f"\nDone in {time.time() - started:.0f}s. Indexed {lg.BULK.sent} documents:")
    for idx in sorted(per_index):
        print(f"   {idx:<48} {per_index[idx]:>8}")
    print("\nIn OpenSearch Dashboards -> Discover:")
    print("   set the time range to cover the window, then search  sim.backfilled:true  to see the events.")
    print("Remember: Prometheus has no metrics for these historical events.")


if __name__ == "__main__":
    main()
