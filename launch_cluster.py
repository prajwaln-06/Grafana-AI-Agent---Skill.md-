#!/usr/bin/env python3
"""
Spin up a simulated cluster of N nodes, each running exporter.py on its own
port, and generate a prometheus.yml scrape config that targets all of them.

Usage:
    python3 launch_cluster.py --num-nodes 4 --base-port 9200 \
        --num-cpus 32 --num-gpus 8

Each node is a separate OS process (own Python interpreter), same as real
node_exporter/dcgm-exporter instances would be — one per host. Ctrl+C stops
all of them.
"""
import argparse
import subprocess
import sys
import time
import signal

PROM_CONFIG_TEMPLATE = """\
global:
  scrape_interval: 10s

scrape_configs:
  - job_name: 'node_exporter_sim'
    static_configs:
{node_targets}

  - job_name: 'dcgm_exporter_sim'
    static_configs:
{dcgm_targets}
"""


def build_prometheus_config(num_nodes, base_port):
    node_targets = []
    dcgm_targets = []
    for i in range(num_nodes):
        node_id = f"node-{i:02d}"
        port = base_port + i
        # both node + DCGM metrics are served from the same /metrics endpoint
        # per node in this simulator, but we register two jobs pointing at
        # the same target so team members can filter/alert per "component"
        # the same way they would against separate real exporters.
        node_targets.append(
            f"      - targets: ['localhost:{port}']\n"
            f"        labels:\n"
            f"          node_id: '{node_id}'\n"
            f"          cluster: 'simulated'"
        )
        dcgm_targets.append(
            f"      - targets: ['localhost:{port}']\n"
            f"        labels:\n"
            f"          node_id: '{node_id}'\n"
            f"          cluster: 'simulated'"
        )
    return PROM_CONFIG_TEMPLATE.format(
        node_targets="\n".join(node_targets),
        dcgm_targets="\n".join(dcgm_targets),
    )


def main():
    p = argparse.ArgumentParser(description="Launch a simulated multi-node Prometheus target cluster")
    p.add_argument("--num-nodes", type=int, default=4)
    p.add_argument("--base-port", type=int, default=9200)
    p.add_argument("--num-cpus", type=int, default=32)
    p.add_argument("--num-gpus", type=int, default=8)
    p.add_argument("--mem-total-gb", type=int, default=256)
    p.add_argument("--swap-total-gb", type=int, default=16)
    p.add_argument("--tick-seconds", type=float, default=2)
    p.add_argument("--write-config", default="prometheus.generated.yml",
                    help="Where to write the generated Prometheus scrape config")
    args = p.parse_args()

    config = build_prometheus_config(args.num_nodes, args.base_port)
    with open(args.write_config, "w") as f:
        f.write(config)
    print(f"Wrote Prometheus scrape config -> {args.write_config}")

    procs = []

    def shutdown(*_):
        print("\nStopping all simulated nodes...")
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    for i in range(args.num_nodes):
        node_id = f"node-{i:02d}"
        port = args.base_port + i
        cmd = [
            sys.executable, "exporter.py",
            "--node-id", node_id,
            "--port", str(port),
            "--num-cpus", str(args.num_cpus),
            "--num-gpus", str(args.num_gpus),
            "--mem-total-gb", str(args.mem_total_gb),
            "--swap-total-gb", str(args.swap_total_gb),
            "--tick-seconds", str(args.tick_seconds),
        ]
        proc = subprocess.Popen(cmd)
        procs.append(proc)
        print(f"Started {node_id} on :{port} (pid {proc.pid})")

    print(f"\n{args.num_nodes} simulated nodes running. "
          f"Point Prometheus at {args.write_config}, or run:\n"
          f"  prometheus --config.file={args.write_config}\n"
          f"Press Ctrl+C to stop all nodes.")

    while True:
        time.sleep(1)
        for proc in procs:
            if proc.poll() is not None:
                print(f"WARNING: a node process exited (code {proc.returncode})")


if __name__ == "__main__":
    main()
