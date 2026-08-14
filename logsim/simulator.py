"""
Log simulator core loop.

Two independent streams feed the same sink:

  1. Background noise (heartbeat/console/syslog) on a per-node timer, on
     ALL healthy nodes, all the time (spec section 6/11).
  2. Incident-triggered events, scheduled from the shared incident
     controller's active-scenario list. Each active incident's log_events
     (see incidents/scenarios.py) get scheduled once, at start_time +
     offset_seconds each, and fired at that wall-clock moment -- so
     offsets are realistic and non-identical (spec section 5/9/18).

A node currently affected by `node_heartbeat_failure` has its background
heartbeat generator paused, so the *absence* of heartbeats is itself part
of the evidence (mirrors a real node going dark).
"""
from __future__ import annotations

import heapq
import random
import time

from datetime import datetime, timezone

from incidents.controller_client import ControllerClient, ControllerUnavailable
from incidents.scenarios import get_scenario
from logsim.log_templates import heartbeat_doc, console_doc, syslog_doc


def _iso(ts: float) -> str:
    return (datetime.fromtimestamp(ts, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")


class LogSimulator:
    def __init__(self, sink, controller_url, nodes, node_gpu_counts,
                 node_mounts, poll_interval=2.0):
        """
        nodes: list of node ids, e.g. ["node-00", ... "node-03"]
        node_gpu_counts: dict node_id -> num_gpus (0 for GPU-less nodes)
        node_mounts: dict node_id -> list of mountpoint strings
        """
        self.sink = sink
        self.controller = ControllerClient(controller_url) if controller_url else None
        self.nodes = nodes
        self.node_gpu_counts = node_gpu_counts
        self.node_mounts = node_mounts
        self.poll_interval = poll_interval

        self._next_bg = {node: {"heartbeat": 0.0, "console": 0.0, "syslog": 0.0}
                          for node in nodes}
        self._scheduled_instances = set()   # instance_ids already scheduled
        self._pending = []                  # heap of (fire_time, seq, doc_index, doc)
        self._seq = 0
        self._heartbeat_suppressed = set()  # nodes currently node-down
        self._last_epoch = None             # detects /scenarios/reset

    # -----------------------------------------------------------------
    def run_forever(self):
        last_poll = 0.0
        while True:
            now = time.time()
            if self.controller and now - last_poll >= self.poll_interval:
                self._poll_and_schedule()
                last_poll = now
            self._emit_background(now)
            self._flush_due(now)
            time.sleep(0.5)

    # -----------------------------------------------------------------
    def _poll_and_schedule(self):
        try:
            active, epoch = self.controller.active_with_epoch()
        except ControllerUnavailable:
            return

        if self._last_epoch is not None and epoch != self._last_epoch:
            # A /scenarios/reset happened (spec section 11/12). Drop
            # anything we'd already queued so no stale future log event
            # fires after the test harness explicitly cleared state.
            print(f"[logsim] reset detected (epoch {self._last_epoch} -> {epoch}); "
                  f"clearing {len(self._pending)} pending log event(s)")
            self._pending = []
            self._scheduled_instances = set()
        self._last_epoch = epoch

        active_node_incidents = {inst["node"] for inst in active
                                  if inst["scenario_id"] == "node_heartbeat_failure"}
        self._heartbeat_suppressed = active_node_incidents

        for inst in active:
            if inst["instance_id"] in self._scheduled_instances:
                continue
            defn = get_scenario(inst["scenario_id"])
            if defn is None:
                continue
            for ev in defn.log_events:
                fire_time = inst["start_time"] + ev.offset_seconds
                index_base, doc = self._render_log_event(ev, inst, fire_time)
                self._push(fire_time, doc, index_base)
            self._scheduled_instances.add(inst["instance_id"])

    def _render_log_event(self, ev, inst, fire_time):
        node = inst["node"]
        gpu = inst.get("gpu")
        mount = inst.get("mount") or (self.node_mounts.get(node) or [None])[0]
        body = ev.body.format(node=node, gpu=gpu, mount=mount)
        attrs = {}
        for k, v in ev.attributes.items():
            if isinstance(v, str):
                v = v.format(node=node, gpu=gpu, mount=mount)
            attrs[k] = v
        from logsim.log_templates import build_doc
        doc = build_doc(service=ev.service, host=node, body=body,
                         severity=ev.severity, attributes=attrs)
        # Incident log events are scheduled ahead of time but must carry
        # the timestamp of when they actually fire, not when they were
        # scheduled -- otherwise every event from the same incident would
        # appear to happen simultaneously (violates spec section 18 rule 3).
        ts_iso = _iso(fire_time)
        doc["@timestamp"] = ts_iso
        doc["Timestamp"] = ts_iso
        return ev.index, doc

    def _push(self, fire_time, doc, index_base):
        self._seq += 1
        heapq.heappush(self._pending, (fire_time, self._seq, index_base, doc))

    def _flush_due(self, now):
        while self._pending and self._pending[0][0] <= now:
            _, _, index_base, doc = heapq.heappop(self._pending)
            self.sink.index(index_base, doc)

    # -----------------------------------------------------------------
    def _emit_background(self, now):
        for node in self.nodes:
            nb = self._next_bg[node]

            if node not in self._heartbeat_suppressed:
                if now >= nb["heartbeat"]:
                    doc, idx = heartbeat_doc(node)
                    self.sink.index(idx, doc)
                    nb["heartbeat"] = now + random.uniform(10, 20)
            # else: incident-affected node stops heartbeating -- the gap
            # itself is observable evidence, not something we need to log.

            if now >= nb["console"]:
                doc, idx = console_doc(node)
                self.sink.index(idx, doc)
                nb["console"] = now + random.uniform(25, 60)

            if now >= nb["syslog"]:
                doc, idx = syslog_doc(node)
                self.sink.index(idx, doc)
                nb["syslog"] = now + random.uniform(8, 25)
