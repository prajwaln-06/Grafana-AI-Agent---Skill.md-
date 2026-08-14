#!/usr/bin/env python3
"""
Deterministic incident injection CLI (spec Phase 8).

    python3 -m tools.trigger_scenario --scenario gpu_overheating \\
        --node node-02 --gpu 3 --start-after 30 --duration 120

    python3 -m tools.trigger_scenario --list

    python3 -m tools.trigger_scenario --scenario filesystem_pressure \\
        --node node-03 --mount /data --duration 100
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, ".")

from incidents.controller_client import ControllerClient, ControllerUnavailable
from incidents.scenarios import SCENARIOS


def main():
    p = argparse.ArgumentParser(description="Trigger a deterministic incident scenario")
    p.add_argument("--controller-url", default=os.environ.get("CONTROLLER_URL", "http://localhost:9500"))
    p.add_argument("--scenario", choices=list(SCENARIOS.keys()))
    p.add_argument("--node")
    p.add_argument("--gpu", type=int, default=None)
    p.add_argument("--mount", default=None)
    p.add_argument("--start-after", type=float, default=0.0)
    p.add_argument("--duration", type=float, default=None,
                    help="Defaults to the scenario's own default_duration_s")
    p.add_argument("--list", action="store_true", help="List available scenarios and exit")
    args = p.parse_args()

    if args.list:
        for sid, defn in SCENARIOS.items():
            flags = []
            if defn.requires_gpu:
                flags.append("--gpu required")
            if defn.requires_filesystem:
                flags.append("--mount optional")
            flag_str = f" ({', '.join(flags)})" if flags else ""
            print(f"  {sid:28s} {defn.display_name}{flag_str} "
                  f"[default duration {defn.default_duration_s:.0f}s]")
        return

    if not args.scenario or not args.node:
        p.error("--scenario and --node are required (or use --list)")

    client = ControllerClient(args.controller_url)
    try:
        client.health()
    except ControllerUnavailable:
        print(f"ERROR: incident controller unreachable at {args.controller_url}. "
              f"Is it running? (python3 -m incidents.controller / docker compose up incident-controller)",
              file=sys.stderr)
        sys.exit(1)

    try:
        instance = client.trigger(
            scenario_id=args.scenario, node=args.node, gpu=args.gpu,
            mount=args.mount, start_after=args.start_after, duration=args.duration,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(instance, indent=2))
    print(f"\nTriggered '{args.scenario}' on {args.node} "
          f"(starts in {args.start_after:.0f}s, runs {instance['duration']:.0f}s).")


if __name__ == "__main__":
    main()
