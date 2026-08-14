#!/usr/bin/env python3
"""
End-to-end correlation validation (spec Phase 9/10/11).

Runs the incident controller + a handful of simulated NodeState objects +
the log simulator (MemorySink) all in-process, triggers each scenario in
turn, and asserts:

  - the affected node/component shows abnormal metrics
  - unaffected nodes/components stay normal
  - correlated OpenSearch-shaped log events appear for the same node,
    inside a realistic time window around the metric anomaly (not at
    identical timestamps)
  - background noise (heartbeat/console/syslog) is present alongside the
    incident logs, not replaced by them
  - none of the above requires reading scenario_id -- only node, time,
    service, severity, and body content (the fields an agent could query)

This intentionally does NOT require Docker or a running OpenSearch cluster
-- it's the fast, deterministic regression test to run after every change.
For a real end-to-end smoke test against the actual containers, see
README.md "Verifying the stack".
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

from incidents import controller as controller_mod
from incidents.controller_client import ControllerClient
from exporter import NodeState
from logsim.simulator import LogSimulator
from logsim.sinks import MemorySink
from http.server import ThreadingHTTPServer


NODES = ["node-00", "node-01", "node-02", "node-03"]
GPU_COUNTS = {"node-00": 8, "node-01": 8, "node-02": 4, "node-03": 0}
MOUNTS = {n: ["/", "/data", "/var/log"] for n in NODES}

FAILURES = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    if not cond:
        FAILURES.append(label)


def start_controller(port):
    server = ThreadingHTTPServer(("127.0.0.1", port), controller_mod.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def main():
    port = 9531
    controller_url = f"http://127.0.0.1:{port}"
    start_controller(port)
    time.sleep(0.3)
    client = ControllerClient(controller_url)
    assert client.health()["status"] == "ok"

    # ---- build node states, each polling the controller ----
    states = {}
    for n in NODES:
        s = NodeState(n, num_cpus=8, num_gpus=GPU_COUNTS[n],
                       mem_total_gb=64, swap_total_gb=8, num_filesystems=3)
        s.start_incident_polling(controller_url, poll_interval=0.5)
        states[n] = s

    def tick_loop():
        while True:
            for s in states.values():
                s.tick(1.0)
            time.sleep(0.5)

    threading.Thread(target=tick_loop, daemon=True).start()

    # ---- build log simulator against a memory sink ----
    sink = MemorySink()
    sim = LogSimulator(sink=sink, controller_url=controller_url,
                        nodes=NODES, node_gpu_counts=GPU_COUNTS,
                        node_mounts=MOUNTS, poll_interval=0.5)
    threading.Thread(target=sim.run_forever, daemon=True).start()

    # let background noise establish itself before any incident
    time.sleep(3)

    print("\n=== Scenario: gpu_overheating on node-02/gpu2 ===")
    t0 = time.time()
    # duration only bounds how long the metric-side effect is applied; log
    # events are scheduled up-front at trigger time regardless of duration
    # (see LogSimulator._poll_and_schedule), so a short duration is fine --
    # we just need to wait long enough for the *last* offset (62s) before
    # checking log correlation.
    client.trigger("gpu_overheating", "node-02", gpu=2, start_after=0, duration=25)

    time.sleep(15)   # mid-incident: metric effect should be well established
    g_target = states["node-02"].gpus[2]
    g_other = states["node-02"].gpus[0]
    g_other_node = states["node-01"].gpus[2]
    check("targeted GPU util elevated (>75)", g_target["util"] > 75)
    check("targeted GPU temp elevated (>70)", g_target["gpu_temp"] > 70)
    check("unaffected GPU on same node stayed lower", g_other["util"] < g_target["util"])
    check("unaffected node's GPU stayed lower", g_other_node["util"] < g_target["util"])

    time.sleep(55)   # total ~70s: past the last log offset (62s)
    logs = sink.query(node="node-02", index_prefix="syslog")
    check("correlated syslog entries mention node-02 & GPU 2",
          any("GPU 2" in d["Body"] and "node-02" in d["Body"] for _, d in logs))
    other_node_logs = sink.query(node="node-01", index_prefix="syslog")
    check("no GPU-overheating logs leaked onto node-01",
          not any("workload threshold exceeded" in d["Body"] for _, d in other_node_logs))

    incident_bodies = ("threshold exceeded", "temperature warning", "power draw")
    matching = [d for _, d in logs if any(b in d["Body"] for b in incident_bodies)]
    offsets = sorted(
        datetime.fromisoformat(d["@timestamp"].replace("Z", "+00:00")).timestamp() - t0
        for d in matching
    )
    check("all 3 correlated log events fired", len(matching) == 3)
    check("log offsets are NOT all identical", len(set(round(o) for o in offsets)) > 1)
    check("logs land within the incident's realistic window (0-90s)",
          all(0 <= o <= 90 for o in offsets))

    bg_present = sink.query(node="node-02", index_prefix="heartbeat")
    check("background heartbeat noise still present on incident node",
          len(bg_present) > 0)

    # duration was 25s and we've now waited ~70s total since trigger --
    # the metric effect should be long expired and the value decaying
    # back toward normal (spec section 8: normal -> anomaly -> recovery,
    # not normal -> permanent anomaly).
    check("GPU util recovers toward normal after incident expires (<70)",
          g_target["util"] < 70)

    print("\n=== Scenario: node_heartbeat_failure on node-03 ===")
    t0 = time.time()
    client.trigger("node_heartbeat_failure", "node-03", start_after=0, duration=15)
    time.sleep(1)
    hb_before = len(sink.query(node="node-03", index_prefix="heartbeat"))
    time.sleep(12)
    hb_after = len(sink.query(node="node-03", index_prefix="heartbeat"))
    check("heartbeat count stalls while node-03 incident is active",
          hb_after - hb_before <= 1)
    check("node_heartbeat_ok gauge flips to 0 on node-03",
          states["node-03"].heartbeat_ok == 0)
    check("other nodes keep heartbeating normally",
          states["node-00"].heartbeat_ok == 1)
    missed = sink.query(node="node-03", index_prefix="heartbeat")
    check("a 'Heartbeat missed' event was logged for node-03",
          any("missed" in d["Body"].lower() for _, d in missed))

    print("\n=== Scenario: ssh_auth_burst on node-01 (metric-silent) ===")
    util_before = states["node-01"].gpus[0]["util"] if GPU_COUNTS["node-01"] else None
    client.trigger("ssh_auth_burst", "node-01", start_after=0, duration=25)
    time.sleep(27)
    ssh_logs = sink.query(node="node-01", service="sshd")
    check("ssh burst produced multiple failed-auth log lines",
          sum(1 for _, d in ssh_logs if "failed authentication" in d["Body"]) >= 3)

    print("\n=== Scenario: gpu_hardware_degradation on node-00/gpu5 ===")
    ecc_before = states["node-00"].gpus[5]["ecc_sbe_total"]
    retired_before = states["node-00"].gpus[5]["retired_pending"]
    client.trigger("gpu_hardware_degradation", "node-00", gpu=5, start_after=0, duration=45)
    time.sleep(45)
    g = states["node-00"].gpus[5]
    check("deterministic ECC bump applied (ecc_sbe_total increased)",
          g["ecc_sbe_total"] > ecc_before)
    check("deterministic retired-page bump applied", g["retired_pending"] > retired_before)
    hw_logs = sink.query(node="node-00", index_prefix="syslog")
    check("ECC/degradation logs correlated to node-00 & GPU 5",
          any("GPU 5" in d["Body"] and "node-00" in d["Body"] for _, d in hw_logs))

    print("\n=== Scenario: memory_pressure on node-01 ===")
    client.trigger("memory_pressure", "node-01", start_after=0, duration=35)
    time.sleep(20)
    s1, s0 = states["node-01"], states["node-00"]
    check("MemAvailable pushed down on node-01",
          s1.mem_available_bytes / s1.mem_total_bytes < 0.35)
    check("node-00 memory unaffected",
          s0.mem_available_bytes / s0.mem_total_bytes > s1.mem_available_bytes / s1.mem_total_bytes)
    time.sleep(16)
    mem_logs = sink.query(node="node-01", index_prefix="syslog")
    check("memory-pressure logs correlated to node-01",
          any("Memory pressure detected" in d["Body"] for _, d in mem_logs))

    print("\n=== Scenario: filesystem_pressure on node-03 (/data) ===")
    fs_before = next(fs["avail_bytes"] for fs in states["node-03"].filesystems
                      if fs["mountpoint"] == "/data")
    client.trigger("filesystem_pressure", "node-03", mount="/data", start_after=0, duration=20)
    time.sleep(20)
    fs_after = next(fs["avail_bytes"] for fs in states["node-03"].filesystems
                     if fs["mountpoint"] == "/data")
    other_fs_after = next(fs["avail_bytes"] for fs in states["node-03"].filesystems
                           if fs["mountpoint"] == "/")
    check("/data avail_bytes dropped sharply on node-03", fs_after < fs_before * 0.9)
    time.sleep(15)
    fs_logs = sink.query(node="node-03", index_prefix="syslog")
    check("filesystem-pressure logs correlated to node-03 (/data)",
          any("/data" in d["Body"] and "node-03" in d["Body"] for _, d in fs_logs))

    print("\n=== Scenario reset (clears effects + pending log schedule) ===")
    client.trigger("memory_pressure", "node-00", start_after=0, duration=60)
    time.sleep(2)
    check("effects present on node-00 before reset",
          states["node-00"].incident_effects.get("mem", {}) != {})
    epoch_before = client._get("/scenarios/active")["reset_epoch"]
    client.reset()
    time.sleep(2)  # let exporter + logsim poll at least once post-reset
    epoch_after = client._get("/scenarios/active")["reset_epoch"]
    check("reset_epoch incremented", epoch_after > epoch_before)
    check("effects cleared on node-00 after reset",
          states["node-00"].incident_effects.get("mem", {}) == {})
    check("logsim has no pending log events after reset", len(sim._pending) == 0)

    print(f"\n{len(FAILURES)} failing check(s)." if FAILURES else "\nAll checks passed.")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
