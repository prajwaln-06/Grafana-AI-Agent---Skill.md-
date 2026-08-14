#!/usr/bin/env python3
"""
OpenSearch log simulator entrypoint.

    python3 -m logsim.main --sink opensearch --opensearch-url https://opensearch:9200 \\
        --controller-url http://incident-controller:9500 \\
        --nodes node-00,node-01,node-02,node-03 --gpu-counts 8,8,4,0

    # local dev, no OpenSearch/Docker needed:
    python3 -m logsim.main --sink file --data-dir ./data/logsim \\
        --controller-url http://localhost:9500 --nodes node-00,node-01
"""
from __future__ import annotations

import argparse
import os

from logsim.simulator import LogSimulator
from logsim.sinks import make_sink

READY_FILE = "/tmp/logsim_ready"


def parse_nodes(nodes_arg, gpu_counts_arg, mounts_arg):
    nodes = [n.strip() for n in nodes_arg.split(",") if n.strip()]
    gpu_counts = {}
    if gpu_counts_arg:
        counts = [int(c) for c in gpu_counts_arg.split(",")]
        for node, count in zip(nodes, counts):
            gpu_counts[node] = count
    else:
        gpu_counts = {n: 0 for n in nodes}

    mounts = {}
    default_mounts = ["/", "/data", "/var/log"]
    if mounts_arg:
        for chunk in mounts_arg.split(";"):
            node, mnts = chunk.split("=")
            mounts[node.strip()] = [m.strip() for m in mnts.split(",")]
    else:
        for n in nodes:
            mounts[n] = default_mounts
    return nodes, gpu_counts, mounts


def main():
    p = argparse.ArgumentParser(description="OpenSearch-correlated log simulator")
    p.add_argument("--sink", choices=["opensearch", "file", "memory"],
                    default=os.environ.get("SINK", "opensearch"))
    p.add_argument("--opensearch-url", default=os.environ.get("OPENSEARCH_URL", "https://opensearch:9200"))
    p.add_argument("--opensearch-user", default=os.environ.get("OPENSEARCH_USER", "admin"))
    p.add_argument("--opensearch-password", default=os.environ.get("OPENSEARCH_PASSWORD", "admin"))
    p.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "./data/logsim"))
    p.add_argument("--controller-url", default=os.environ.get("CONTROLLER_URL", "http://incident-controller:9500"))
    p.add_argument("--nodes", default=os.environ.get("NODES", "node-00,node-01,node-02,node-03"))
    p.add_argument("--gpu-counts", default=os.environ.get("GPU_COUNTS", "8,8,4,0"),
                    help="comma-separated, aligned with --nodes")
    p.add_argument("--mounts", default=os.environ.get("MOUNTS", ""),
                    help="node=mount1,mount2;node2=mount1 -- defaults to /,/data,/var/log per node")
    p.add_argument("--poll-interval", type=float, default=2.0)
    args = p.parse_args()

    nodes, gpu_counts, mounts = parse_nodes(args.nodes, args.gpu_counts, args.mounts)

    if args.sink == "opensearch":
        sink = make_sink("opensearch", url=args.opensearch_url,
                          username=args.opensearch_user, password=args.opensearch_password)
    elif args.sink == "file":
        sink = make_sink("file", data_dir=args.data_dir)
    else:
        sink = make_sink("memory")

    print(f"[logsim] sink={args.sink} nodes={nodes} controller={args.controller_url}")

    # Deterministic startup (spec section 5): don't generate a single log
    # until the sink is actually ready and the known index bases exist.
    # For file/memory sinks this is an immediate no-op.
    sink.ensure_ready(["syslog", "consolelog", "heartbeat"])

    try:
        with open(READY_FILE, "w") as f:
            f.write("ready\n")
    except OSError:
        pass  # non-fatal -- just means the Docker HEALTHCHECK won't see it

    print("[logsim] Log simulator started.", flush=True)

    sim = LogSimulator(
        sink=sink,
        controller_url=args.controller_url,
        nodes=nodes,
        node_gpu_counts=gpu_counts,
        node_mounts=mounts,
        poll_interval=args.poll_interval,
    )
    sim.run_forever()


if __name__ == "__main__":
    main()
