#!/usr/bin/env python3
"""
Prometheus metrics simulator — node_exporter + DCGM (GPU) style metrics.

Simulates ONE node with N CPUs and M GPUs. Values evolve over time via
bounded random walks so dashboards/alerts/PromQL built against this look
and behave like a real fleet (correlated GPU util/power/temp/clock,
CPU time that actually sums to wall-clock across modes, slowly draining
disk, etc).

Run standalone:
    python3 exporter.py --node-id node-01 --port 9200 --num-cpus 16 --num-gpus 8

Scrape with Prometheus (see prometheus.yml) and query with real PromQL,
e.g.:
    100 - (rate(node_cpu_seconds_total{mode="idle"}[1m]) * 100)
    DCGM_FI_DEV_GPU_UTIL
    rate(DCGM_FI_PROF_PCIE_TX_BYTES[1m])
"""
import argparse
import os
import random
import threading
import time

from prometheus_client import start_http_server, Gauge, Counter, REGISTRY
from prometheus_client.core import GaugeMetricFamily

from incidents.controller_client import ControllerClient, ControllerUnavailable

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def walk(current, lo, hi, max_step, mean_reversion_target=None, pull=0.02):
    """One step of a bounded random walk, with gentle pull back to a target
    (keeps values from drifting to the edges and staying stuck there)."""
    step = random.uniform(-max_step, max_step)
    new_val = current + step
    if mean_reversion_target is not None:
        new_val += (mean_reversion_target - current) * pull
    return clamp(new_val, lo, hi)


# --------------------------------------------------------------------------
# Simulated node state
# --------------------------------------------------------------------------

class NodeState:
    """Holds the live simulated values for one node. A background thread
    mutates this state; the Prometheus collector reads it on every scrape."""

    CPU_MODES = ["user", "system", "idle", "iowait", "nice", "irq", "softirq", "steal"]
    # Roughly how a "typical" tick's busy time splits across non-idle modes
    BUSY_MODE_WEIGHTS = {
        "user": 0.55, "system": 0.20, "iowait": 0.10,
        "nice": 0.02, "irq": 0.05, "softirq": 0.05, "steal": 0.03,
    }

    def __init__(self, node_id, num_cpus, num_gpus, mem_total_gb, swap_total_gb,
                 num_filesystems):
        self.node_id = node_id
        self.num_cpus = num_cpus
        self.num_gpus = num_gpus
        self.lock = threading.Lock()

        # ---- CPU ----
        self.cpu_busy_frac = [random.uniform(0.05, 0.4) for _ in range(num_cpus)]
        # cumulative seconds per (cpu, mode) -- counters must only increase
        self.cpu_seconds = {c: {m: 0.0 for m in self.CPU_MODES} for c in range(num_cpus)}
        self.context_switches_total = random.uniform(1e8, 5e8)
        self.intr_total = random.uniform(5e7, 2e8)

        # ---- Load ----
        base_load = num_cpus * random.uniform(0.15, 0.35)
        self.load1 = base_load
        self.load5 = base_load
        self.load15 = base_load

        # ---- Memory ----
        self.mem_total_bytes = mem_total_gb * (1024 ** 3)
        used_frac = random.uniform(0.3, 0.6)
        self.mem_available_bytes = self.mem_total_bytes * (1 - used_frac)
        self.mem_free_bytes = self.mem_available_bytes * random.uniform(0.5, 0.8)
        self.mem_cached_bytes = self.mem_total_bytes * random.uniform(0.1, 0.25)
        self.mem_buffers_bytes = self.mem_total_bytes * random.uniform(0.01, 0.04)

        # ---- Swap ----
        self.swap_total_bytes = swap_total_gb * (1024 ** 3)
        self.swap_free_bytes = self.swap_total_bytes * random.uniform(0.7, 1.0)

        # ---- Filesystem (simulate a few mounted filesystems) ----
        self.filesystems = []
        mounts = [("/dev/sda1", "/", "ext4"), ("/dev/sda2", "/data", "xfs"),
                  ("/dev/nvme0n1p1", "/var/log", "ext4")][:num_filesystems]
        for device, mountpoint, fstype in mounts:
            size_bytes = random.choice([500, 1000, 2000, 4000]) * (1024 ** 3)
            avail_frac = random.uniform(0.3, 0.7)
            self.filesystems.append({
                "device": device, "mountpoint": mountpoint, "fstype": fstype,
                "size_bytes": size_bytes,
                "avail_bytes": size_bytes * avail_frac,
                "free_bytes": size_bytes * (avail_frac + 0.02),
            })

        # ---- GPU (DCGM) ----
        self.gpus = []
        for g in range(num_gpus):
            fb_total = random.choice([16, 24, 40, 80]) * (1024 ** 3)
            util = random.uniform(5, 30)
            self.gpus.append({
                "util": util,
                "gr_engine_active": util / 100.0,
                "fb_total": fb_total,
                "fb_used": fb_total * random.uniform(0.05, 0.2),
                "mem_copy_util": random.uniform(2, 15),
                "gpu_temp": 35 + util * 0.4,
                "mem_temp": 32 + util * 0.3,
                "power_usage": 60 + util * 2.5,
                "power_violation_total": 0.0,
                "sm_clock": 800 + util * 12,
                "mem_clock": 1200 + util * 4,
                "pcie_tx_total": random.uniform(1e10, 1e11),
                "pcie_rx_total": random.uniform(1e10, 1e11),
                "nvlink_tx_total": random.uniform(1e10, 1e11),
                "nvlink_rx_total": random.uniform(1e10, 1e11),
                "tensor_active": util / 100.0 * random.uniform(0.6, 1.0),
                "fp64_active": random.uniform(0, 0.05),
                "fp32_active": util / 100.0 * random.uniform(0.3, 0.6),
                "fp16_active": util / 100.0 * random.uniform(0.2, 0.5),
                "dram_active": util / 100.0 * random.uniform(0.4, 0.8),
                "ecc_sbe_total": 0.0,
                "ecc_dbe_total": 0.0,
                "retired_sbe_total": 0.0,
                "retired_dbe_total": 0.0,
                "retired_pending": 0,
                "nvlink_crc_error_total": 0.0,
                "nvlink_recovery_error_total": 0.0,
            })

        # ---- Incident integration (additive, optional) ----
        # `incident_effects` is refreshed by an optional background poller
        # (see start_incident_polling) that asks the shared incident
        # controller "what's active on my node right now?". When nothing
        # is polling (e.g. standalone `python3 exporter.py` with no
        # controller running), this stays {} forever and the node behaves
        # exactly as it did before -- fully backward compatible.
        self.incident_effects = {"gpu": {}, "mem": {}, "fs": {}, "heartbeat_ok": 1}
        self._effects_lock = threading.Lock()
        self._seen_instance_bumps = set()  # instance_ids we've already
                                            # applied a deterministic bump for
        self.heartbeat_ok = 1

    # ---------------------------------------------------------------
    def start_incident_polling(self, controller_url, poll_interval=3.0):
        """Spawn a daemon thread that periodically pulls active incidents
        for this node from the shared incident controller and translates
        them into `incident_effects`. Non-fatal if the controller is
        unreachable -- the node just runs without incidents."""
        client = ControllerClient(controller_url)

        def _loop():
            warned = False
            while True:
                try:
                    active = client.active(node=self.node_id)
                    self._apply_active_incidents(active)
                    warned = False
                except ControllerUnavailable:
                    if not warned:
                        print(f"[{self.node_id}] incident controller unreachable "
                              f"at {controller_url}; running without incidents")
                        warned = True
                except Exception as e:  # noqa: BLE001
                    print(f"[{self.node_id}] incident poll error: {e}")
                time.sleep(poll_interval)

        threading.Thread(target=_loop, daemon=True).start()

    def _apply_active_incidents(self, active_instances):
        """Translate controller instances into the effect dict tick()
        consumes. See incidents/scenarios.py for what each key means."""
        from incidents.scenarios import get_scenario

        effects = {"gpu": {}, "mem": {}, "fs": {}, "heartbeat_ok": 1}
        for inst in active_instances:
            defn = get_scenario(inst["scenario_id"])
            if defn is None:
                continue
            fx = defn.metric_effects
            if defn.requires_gpu and inst.get("gpu") is not None:
                gpu_idx = int(inst["gpu"])
                effects["gpu"][gpu_idx] = {**fx, "instance_id": inst["instance_id"],
                                            "elapsed": time.time() - inst["start_time"]}
            elif inst["scenario_id"] in ("memory_pressure",):
                effects["mem"] = fx
            elif inst["scenario_id"] in ("filesystem_pressure",):
                mount = inst.get("mount")
                effects["fs"][mount] = fx  # None mount = apply to all mounts
            elif "heartbeat_ok" in fx:
                effects["heartbeat_ok"] = fx["heartbeat_ok"]
            # ssh_auth_burst is metric-silent by design -- nothing to do here

        with self._effects_lock:
            self.incident_effects = effects

    # ---------------------------------------------------------------
    def tick(self, dt):
        """Advance simulated state by dt seconds. Called periodically."""
        with self.lock:
            with self._effects_lock:
                effects = self.incident_effects
            self._tick_cpu(dt)
            self._tick_load(effects.get("mem", {}))
            self._tick_memory(effects.get("mem", {}))
            self._tick_filesystem(dt, effects.get("fs", {}))
            self._tick_gpu(dt, effects.get("gpu", {}))
            self.heartbeat_ok = effects.get("heartbeat_ok", 1)

    def _tick_cpu(self, dt):
        for c in range(self.num_cpus):
            # random-walk the "busy" fraction for this core
            self.cpu_busy_frac[c] = walk(
                self.cpu_busy_frac[c], 0.02, 0.98, max_step=0.08,
                mean_reversion_target=0.35, pull=0.03,
            )
            busy = self.cpu_busy_frac[c]
            idle_dt = dt * (1 - busy)
            self.cpu_seconds[c]["idle"] += idle_dt
            busy_dt = dt * busy
            for mode, weight in self.BUSY_MODE_WEIGHTS.items():
                self.cpu_seconds[c][mode] += busy_dt * weight
        self.context_switches_total += dt * random.uniform(2000, 20000) * (1 + sum(self.cpu_busy_frac) / self.num_cpus)
        self.intr_total += dt * random.uniform(1000, 8000)

    def _tick_load(self, mem_effects=None):
        avg_busy = sum(self.cpu_busy_frac) / self.num_cpus
        target = avg_busy * self.num_cpus
        target += self.num_cpus * (mem_effects or {}).get("load_bonus", 0.0)
        self.load1 = walk(self.load1, 0, self.num_cpus * 1.5, max_step=self.num_cpus * 0.1,
                           mean_reversion_target=target, pull=0.15)
        self.load5 = walk(self.load5, 0, self.num_cpus * 1.5, max_step=self.num_cpus * 0.05,
                           mean_reversion_target=self.load1, pull=0.08)
        self.load15 = walk(self.load15, 0, self.num_cpus * 1.5, max_step=self.num_cpus * 0.02,
                            mean_reversion_target=self.load5, pull=0.04)

    def _tick_memory(self, mem_effects=None):
        mem_effects = mem_effects or {}
        step = self.mem_total_bytes * 0.01
        # Normal target is 50% of total; a memory_pressure incident pushes
        # this target down toward `mem_available_frac_target`, and the
        # existing walk() machinery (already used for normal drift) does
        # the rest -- no separate code path for "incident mode".
        avail_target_frac = mem_effects.get("mem_available_frac_target", 0.5)
        pull = mem_effects.get("mem_pull", 0.03)
        self.mem_available_bytes = walk(
            self.mem_available_bytes, self.mem_total_bytes * 0.05,
            self.mem_total_bytes * 0.9, max_step=step,
            mean_reversion_target=self.mem_total_bytes * avail_target_frac, pull=pull,
        )
        self.mem_free_bytes = clamp(self.mem_available_bytes * random.uniform(0.5, 0.8),
                                     0, self.mem_total_bytes)
        self.mem_cached_bytes = clamp(
            self.mem_total_bytes - self.mem_available_bytes, 0, self.mem_total_bytes * 0.4)

        swap_target_frac = mem_effects.get("swap_free_frac_target", None)
        swap_pull = mem_effects.get("swap_pull", 0.0)
        swap_target = (self.swap_total_bytes * swap_target_frac
                        if swap_target_frac is not None else None)
        self.swap_free_bytes = clamp(
            walk(self.swap_free_bytes, 0, self.swap_total_bytes,
                 max_step=self.swap_total_bytes * 0.02,
                 mean_reversion_target=swap_target, pull=swap_pull),
            0, self.swap_total_bytes)

    def _tick_filesystem(self, dt, fs_effects=None):
        fs_effects = fs_effects or {}
        for fs in self.filesystems:
            # Incident may target a specific mount, or (mount=None) all of
            # them. Effects dict is keyed by mountpoint string or None.
            fx = fs_effects.get(fs["mountpoint"], fs_effects.get(None, {}))
            multiplier = fx.get("fs_fill_rate_multiplier", 1.0)
            # slow, mostly-one-directional drift (disks fill up over time);
            # an incident just speeds up the same drift term.
            drift = fs["size_bytes"] * 0.0000005 * dt * multiplier
            fs["avail_bytes"] = clamp(fs["avail_bytes"] - drift + random.uniform(-drift, drift * 0.5),
                                       fs["size_bytes"] * 0.005, fs["size_bytes"])
            fs["free_bytes"] = clamp(fs["avail_bytes"] * 1.02, 0, fs["size_bytes"])

    def _tick_gpu(self, dt, gpu_effects=None):
        gpu_effects = gpu_effects or {}
        for idx, gpu in enumerate(self.gpus):
            fx = gpu_effects.get(idx, {})
            util_target = fx.get("gpu_util_target", random.choice([10, 40, 85]))
            util_pull = fx.get("gpu_util_pull", 0.05)
            gpu["util"] = walk(gpu["util"], 0, 100, max_step=15,
                                mean_reversion_target=util_target, pull=util_pull)
            u = gpu["util"] / 100.0
            gpu["gr_engine_active"] = clamp(u * random.uniform(0.9, 1.05), 0, 1)
            gpu["mem_copy_util"] = clamp(u * 100 * random.uniform(0.2, 0.6), 0, 100)
            gpu["fb_used"] = clamp(gpu["fb_total"] * (0.05 + u * 0.7), 0, gpu["fb_total"])
            gpu["gpu_temp"] = clamp(35 + u * 55 + random.uniform(-1, 1), 25, 95)
            gpu["mem_temp"] = clamp(32 + u * 45 + random.uniform(-1, 1), 25, 90)
            gpu["power_usage"] = clamp(60 + u * 350 + random.uniform(-5, 5), 40, 700)
            gpu["sm_clock"] = clamp(800 + u * 1200 + random.uniform(-20, 20), 200, 2100)
            gpu["mem_clock"] = clamp(1200 + u * 500 + random.uniform(-10, 10), 400, 1800)
            gpu["tensor_active"] = clamp(u * random.uniform(0.6, 1.0), 0, 1)
            gpu["fp64_active"] = clamp(random.uniform(0, 0.05), 0, 1)
            gpu["fp32_active"] = clamp(u * random.uniform(0.3, 0.6), 0, 1)
            gpu["fp16_active"] = clamp(u * random.uniform(0.2, 0.5), 0, 1)
            gpu["dram_active"] = clamp(u * random.uniform(0.4, 0.8), 0, 1)

            bw = dt * u * random.uniform(1e8, 8e8)
            gpu["pcie_tx_total"] += bw * random.uniform(0.8, 1.2)
            gpu["pcie_rx_total"] += bw * random.uniform(0.8, 1.2)
            gpu["nvlink_tx_total"] += bw * random.uniform(1.5, 3.0)
            gpu["nvlink_rx_total"] += bw * random.uniform(1.5, 3.0)

            # rare error events, mostly zero -- an active hardware-
            # degradation incident on this GPU raises these probabilities
            # for the duration of the window (same code path, just
            # different odds), per design constraint 13 (prefer explicit,
            # deterministic relationships over arbitrary random ones).
            ecc_sbe_p = fx.get("ecc_sbe_prob", 0.0005)
            ecc_dbe_p = fx.get("ecc_dbe_prob", 0.00002)
            retired_p = fx.get("retired_pending_prob", 0.00002)
            crc_p = fx.get("nvlink_crc_prob", 0.0003)

            if random.random() < ecc_sbe_p:
                gpu["ecc_sbe_total"] += 1
            if random.random() < ecc_dbe_p:
                gpu["ecc_dbe_total"] += 1
            if random.random() < retired_p:
                gpu["retired_sbe_total"] += 1
                gpu["retired_pending"] += 1
            if random.random() < 0.000005:
                gpu["retired_dbe_total"] += 1
            if random.random() < crc_p:
                gpu["nvlink_crc_error_total"] += random.randint(1, 5)
            if random.random() < 0.00005:
                gpu["nvlink_recovery_error_total"] += 1
            if gpu["power_usage"] > 650 and random.random() < 0.05:
                gpu["power_violation_total"] += dt * random.uniform(0.01, 0.2)

            # Deterministic bump: guarantees a hardware-degradation
            # incident always produces *some* concrete evidence shortly
            # after it starts, rather than relying purely on the elevated
            # probabilities above (which could, in principle, roll all
            # zeros during a short demo window).
            bump_at = fx.get("deterministic_bump_at_s")
            instance_id = fx.get("instance_id")
            if (bump_at is not None and instance_id
                    and instance_id not in self._seen_instance_bumps
                    and fx.get("elapsed", 0) >= bump_at):
                gpu["ecc_sbe_total"] += 1
                gpu["retired_sbe_total"] += 1
                gpu["retired_pending"] += 1
                self._seen_instance_bumps.add(instance_id)


# --------------------------------------------------------------------------
# Custom collector: reads NodeState and emits Prometheus metric families
# --------------------------------------------------------------------------

class SimulatedNodeCollector:
    def __init__(self, state: NodeState):
        self.state = state

    def collect(self):
        s = self.state
        with s.lock:
            # ---- node_exporter: CPU ----
            m = GaugeMetricFamily("node_cpu_seconds_total", "CPU time in seconds by mode",
                                   labels=["cpu", "mode"])
            m.type = "counter"
            for c in range(s.num_cpus):
                for mode in s.CPU_MODES:
                    m.add_metric([str(c), mode], s.cpu_seconds[c][mode])
            yield m

            for name, val, help_text in [
                ("node_load1", s.load1, "1-minute load average"),
                ("node_load5", s.load5, "5-minute load average"),
                ("node_load15", s.load15, "15-minute load average"),
            ]:
                fam = GaugeMetricFamily(name, help_text)
                fam.add_metric([], val)
                yield fam

            fam = GaugeMetricFamily("node_context_switches_total", "Total context switches")
            fam.type = "counter"
            fam.add_metric([], s.context_switches_total)
            yield fam

            fam = GaugeMetricFamily("node_intr_total", "Total interrupts serviced")
            fam.type = "counter"
            fam.add_metric([], s.intr_total)
            yield fam

            # Minimal synthetic signal for the node/heartbeat-failure
            # incident scenario (spec section 7, scenario 5). 1 = healthy,
            # 0 = incident-affected. This is the only wholly new metric
            # added for incident support; everything else reuses existing
            # derived relationships.
            fam = GaugeMetricFamily("node_heartbeat_ok", "1 if node heartbeat is healthy, 0 if not")
            fam.add_metric([], getattr(s, "heartbeat_ok", 1))
            yield fam

            # ---- node_exporter: Memory / Swap ----
            for name, val, help_text in [
                ("node_memory_MemTotal_bytes", s.mem_total_bytes, "Total physical memory"),
                ("node_memory_MemAvailable_bytes", s.mem_available_bytes, "Memory available without swapping"),
                ("node_memory_MemFree_bytes", s.mem_free_bytes, "Completely free memory"),
                ("node_memory_Cached_bytes", s.mem_cached_bytes, "Linux page cache"),
                ("node_memory_Buffers_bytes", s.mem_buffers_bytes, "Filesystem buffers"),
                ("node_memory_SwapTotal_bytes", s.swap_total_bytes, "Total swap space"),
                ("node_memory_SwapFree_bytes", s.swap_free_bytes, "Available swap"),
            ]:
                fam = GaugeMetricFamily(name, help_text)
                fam.add_metric([], val)
                yield fam

            # ---- node_exporter: Filesystem ----
            for metric_name, key, help_text in [
                ("node_filesystem_size_bytes", "size_bytes", "Filesystem size"),
                ("node_filesystem_avail_bytes", "avail_bytes", "Available disk space for non-root"),
                ("node_filesystem_free_bytes", "free_bytes", "Total free disk space"),
            ]:
                fam = GaugeMetricFamily(metric_name, help_text, labels=["device", "mountpoint", "fstype"])
                for fs in s.filesystems:
                    fam.add_metric([fs["device"], fs["mountpoint"], fs["fstype"]], fs[key])
                yield fam

            # ---- DCGM: GPU ----
            gpu_gauges = [
                ("DCGM_FI_DEV_GPU_UTIL", "util", "GPU utilization (%)"),
                ("DCGM_FI_PROF_GR_ENGINE_ACTIVE", "gr_engine_active", "Graphics/SM engine active fraction"),
                ("DCGM_FI_DEV_FB_USED", "fb_used", "Used framebuffer (VRAM) memory, bytes"),
                ("DCGM_FI_DEV_FB_FREE", None, "Free framebuffer memory, bytes"),  # computed below
                ("DCGM_FI_DEV_MEM_COPY_UTIL", "mem_copy_util", "Memory controller utilization (%)"),
                ("DCGM_FI_DEV_GPU_TEMP", "gpu_temp", "GPU core temperature (C)"),
                ("DCGM_FI_DEV_MEMORY_TEMP", "mem_temp", "HBM/VRAM temperature (C)"),
                ("DCGM_FI_DEV_POWER_USAGE", "power_usage", "Instantaneous GPU power draw (W)"),
                ("DCGM_FI_DEV_SM_CLOCK", "sm_clock", "SM/core clock (MHz)"),
                ("DCGM_FI_DEV_MEM_CLOCK", "mem_clock", "Memory clock (MHz)"),
                ("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", "tensor_active", "Tensor core utilization fraction"),
                ("DCGM_FI_PROF_PIPE_FP64_ACTIVE", "fp64_active", "FP64 pipeline utilization fraction"),
                ("DCGM_FI_PROF_PIPE_FP32_ACTIVE", "fp32_active", "FP32 pipeline utilization fraction"),
                ("DCGM_FI_PROF_PIPE_FP16_ACTIVE", "fp16_active", "FP16 pipeline utilization fraction"),
                ("DCGM_FI_PROF_DRAM_ACTIVE", "dram_active", "DRAM bandwidth utilization fraction"),
                ("DCGM_FI_DEV_RETIRED_PENDING", "retired_pending", "Pages pending retirement"),
            ]
            for metric_name, key, help_text in gpu_gauges:
                fam = GaugeMetricFamily(metric_name, help_text, labels=["gpu", "device"])
                for idx, gpu in enumerate(s.gpus):
                    device = f"nvidia{idx}"
                    if key is None:  # FB_FREE = total - used
                        val = gpu["fb_total"] - gpu["fb_used"]
                    else:
                        val = gpu[key]
                    fam.add_metric([str(idx), device], val)
                yield fam

            gpu_counters = [
                ("DCGM_FI_DEV_POWER_VIOLATION", "power_violation_total", "Time spent power-throttled (s)"),
                ("DCGM_FI_PROF_PCIE_TX_BYTES", "pcie_tx_total", "Bytes transmitted over PCIe"),
                ("DCGM_FI_PROF_PCIE_RX_BYTES", "pcie_rx_total", "Bytes received over PCIe"),
                ("DCGM_FI_PROF_NVLINK_TX_BYTES", "nvlink_tx_total", "Bytes transmitted over NVLink"),
                ("DCGM_FI_PROF_NVLINK_RX_BYTES", "nvlink_rx_total", "Bytes received over NVLink"),
                ("DCGM_FI_DEV_ECC_SBE_VOL_TOTAL", "ecc_sbe_total", "Total single-bit ECC errors"),
                ("DCGM_FI_DEV_ECC_DBE_VOL_TOTAL", "ecc_dbe_total", "Total double-bit ECC errors"),
                ("DCGM_FI_DEV_RETIRED_SBE", "retired_sbe_total", "Pages retired due to SBE"),
                ("DCGM_FI_DEV_RETIRED_DBE", "retired_dbe_total", "Pages retired due to DBE"),
                ("DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL", "nvlink_crc_error_total", "NVLink CRC errors"),
                ("DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL", "nvlink_recovery_error_total", "NVLink recovery events"),
            ]
            for metric_name, key, help_text in gpu_counters:
                fam = GaugeMetricFamily(metric_name, help_text, labels=["gpu", "device"])
                fam.type = "counter"
                for idx, gpu in enumerate(s.gpus):
                    device = f"nvidia{idx}"
                    fam.add_metric([str(idx), device], gpu[key])
                yield fam


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Prometheus node_exporter + DCGM simulator")
    p.add_argument("--node-id", default=os.environ.get("NODE_ID", "node-01"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "9200")))
    p.add_argument("--num-cpus", type=int, default=int(os.environ.get("NUM_CPUS", "16")))
    p.add_argument("--num-gpus", type=int, default=int(os.environ.get("NUM_GPUS", "8")))
    p.add_argument("--mem-total-gb", type=int, default=int(os.environ.get("MEM_TOTAL_GB", "256")))
    p.add_argument("--swap-total-gb", type=int, default=int(os.environ.get("SWAP_TOTAL_GB", "16")))
    p.add_argument("--num-filesystems", type=int, default=int(os.environ.get("NUM_FILESYSTEMS", "3")))
    p.add_argument("--tick-seconds", type=float, default=float(os.environ.get("TICK_SECONDS", "2")))
    p.add_argument("--controller-url", default=os.environ.get("CONTROLLER_URL", ""),
                    help="Base URL of the shared incident controller "
                         "(e.g. http://incident-controller:9500). Optional -- "
                         "if omitted/unreachable, the node just runs normally "
                         "with no incidents.")
    args = p.parse_args()

    state = NodeState(
        node_id=args.node_id,
        num_cpus=args.num_cpus,
        num_gpus=args.num_gpus,
        mem_total_gb=args.mem_total_gb,
        swap_total_gb=args.swap_total_gb,
        num_filesystems=args.num_filesystems,
    )
    REGISTRY.register(SimulatedNodeCollector(state))

    if args.controller_url:
        state.start_incident_polling(args.controller_url)

    start_http_server(args.port)
    print(f"[{args.node_id}] serving simulated metrics on :{args.port}/metrics "
          f"({args.num_cpus} CPUs, {args.num_gpus} GPUs)")

    try:
        while True:
            time.sleep(args.tick_seconds)
            state.tick(args.tick_seconds)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
