#!/usr/bin/env python3
"""
Incident controller -- the single shared source of truth for "which
incidents are currently active, where, and with what effects".

This is the concrete implementation of the "common cause" in the project's
mental model:

                    UNDERLYING SIMULATED SYSTEM   <- this service
                              |
                     +--------+--------+
                     |                 |
                     v                 v
               Metric state        Log events
              (exporter.py)         (logsim)

Every exporter.py node process and the logsim process poll this service's
GET /scenarios/active to find out what's happening; deterministic injection
happens via POST /scenarios/trigger (see tools/trigger_scenario.py).

Deliberately stdlib-only (http.server) so it adds zero new dependencies to
the existing project and is trivial to containerize.

Run:
    python3 -m incidents.controller --port 9500
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from incidents.scenarios import get_scenario, SCENARIOS


class IncidentStore:
    """Thread-safe registry of scenario *instances* (a scenario definition
    applied to a specific node/component at a specific time)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._instances: dict[str, dict] = {}
        self.reset_epoch = 0

    def trigger(self, scenario_id, node, gpu=None, mount=None,
                start_after=0.0, duration=None):
        defn = get_scenario(scenario_id)
        if defn is None:
            raise ValueError(f"unknown scenario_id: {scenario_id}")
        if defn.requires_gpu and gpu is None:
            raise ValueError(f"scenario {scenario_id} requires a gpu index")

        now = time.time()
        instance_id = uuid.uuid4().hex[:12]
        start_time = now + float(start_after)
        dur = float(duration) if duration is not None else defn.default_duration_s
        instance = {
            "instance_id": instance_id,   # internal coordination id only;
                                           # NEVER surfaced as an "answer"
                                           # field in metrics/log bodies.
            "scenario_id": scenario_id,
            "node": node,
            "gpu": gpu,
            "mount": mount,
            "start_time": start_time,
            "end_time": start_time + dur,
            "duration": dur,
            "created_at": now,
        }
        with self._lock:
            self._instances[instance_id] = instance
        return instance

    def active(self, node=None):
        now = time.time()
        with self._lock:
            items = list(self._instances.values())
        out = []
        for inst in items:
            if inst["start_time"] <= now <= inst["end_time"]:
                if node is None or inst["node"] == node:
                    out.append(inst)
        return out

    def all(self):
        with self._lock:
            return list(self._instances.values())

    def reset(self):
        """Clear every active/future scenario instance (spec section 11).
        Bumps reset_epoch so pollers (logsim) can tell the difference
        between "nothing happened yet" and "everything was just cleared"
        and drop any already-scheduled-but-not-yet-fired log events."""
        with self._lock:
            self._instances = {}
            self.reset_epoch += 1
            return self.reset_epoch

    def gc(self, retain_seconds=3600):
        """Drop instances that ended long ago, so the store doesn't grow
        forever in a long-running demo."""
        cutoff = time.time() - retain_seconds
        with self._lock:
            self._instances = {
                k: v for k, v in self._instances.items()
                if v["end_time"] >= cutoff
            }


STORE = IncidentStore()


class Handler(BaseHTTPRequestHandler):
    server_version = "IncidentController/1.0"

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # quiet; avoid spamming stdout on every poll

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif parsed.path == "/now":
            self._send_json(200, {"now": time.time()})
        elif parsed.path == "/scenarios/definitions":
            self._send_json(200, {
                sid: {
                    "display_name": d.display_name,
                    "requires_gpu": d.requires_gpu,
                    "requires_filesystem": d.requires_filesystem,
                    "default_duration_s": d.default_duration_s,
                } for sid, d in SCENARIOS.items()
            })
        elif parsed.path == "/scenarios/active":
            node = qs.get("node", [None])[0]
            self._send_json(200, {"active": STORE.active(node=node),
                                   "reset_epoch": STORE.reset_epoch})
        elif parsed.path == "/scenarios/all":
            self._send_json(200, {"instances": STORE.all()})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/scenarios/reset":
            epoch = STORE.reset()
            self._send_json(200, {"status": "reset", "reset_epoch": epoch})
            return
        if parsed.path != "/scenarios/trigger":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
            instance = STORE.trigger(
                scenario_id=body["scenario_id"],
                node=body["node"],
                gpu=body.get("gpu"),
                mount=body.get("mount"),
                start_after=body.get("start_after", 0.0),
                duration=body.get("duration"),
            )
            self._send_json(200, instance)
        except (KeyError, ValueError) as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"error": str(e)})


def _gc_loop(interval=60):
    while True:
        time.sleep(interval)
        STORE.gc()


def main():
    p = argparse.ArgumentParser(description="Shared incident controller")
    p.add_argument("--port", type=int, default=9500)
    args = p.parse_args()

    threading.Thread(target=_gc_loop, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"[incident-controller] listening on :{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
