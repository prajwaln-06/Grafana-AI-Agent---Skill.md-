"""
Scenario/incident library.

A Scenario is the SHARED CAUSE that produces both:
  - metric effects  -> consumed by exporter.py (NodeState)
  - log events      -> consumed by logsim (OpenSearch log generator)

Design notes (see project spec, Phase 3/7):
  - metric_effects describes *how to nudge the existing random-walk state*,
    not raw values to overwrite. exporter.py applies these as adjustments to
    walk() targets/bounds so derived relationships (e.g. GPU temp/power
    derived from util) continue to hold naturally.
  - log_events is a list of (offset_seconds, index, severity, service,
    body_template, extra_attributes) tuples. offset_seconds is relative to
    the incident's start_time and must NOT all be identical, per spec
    section 9/18.
  - Nothing here is an "answer field" exposed to a diagnosis agent -- node,
    component, time and log content are the only observables. The
    scenario_id is internal coordination metadata only (spec section 15).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass(frozen=True)
class LogEvent:
    offset_seconds: float          # relative to incident start_time
    index: str                     # "syslog" | "consolelog" | "heartbeat"
    severity: str                  # "INFO" | "WARN" | "ERROR"
    service: str                   # Resource.service.name
    body: str                      # Body template, may use {node}/{gpu}/{fs}/{mount}
    attributes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScenarioDefinition:
    scenario_id: str
    display_name: str
    requires_gpu: bool = False
    requires_filesystem: bool = False
    default_duration_s: float = 120.0
    # metric_effects: applied every exporter tick while the incident is
    # active. Keys documented per-scenario below; consumed in exporter.py.
    metric_effects: dict = field(default_factory=dict)
    log_events: tuple = field(default_factory=tuple)


SCENARIOS: dict[str, ScenarioDefinition] = {

    # ------------------------------------------------------------------
    "gpu_overheating": ScenarioDefinition(
        scenario_id="gpu_overheating",
        display_name="GPU overheating",
        requires_gpu=True,
        default_duration_s=120.0,
        metric_effects={
            # Push the GPU's utilization random-walk target way up.
            # Temperature / power / clocks are already *derived* from util
            # inside exporter.py's _tick_gpu(), so this single lever
            # naturally cascades into all of them -- no independent
            # overwrite of temp/power needed (spec section 8).
            "gpu_util_target": 97,
            "gpu_util_pull": 0.55,   # strong pull so it reliably converges
                                     # within a few ticks -- incidents need
                                     # to be reproducible (spec section 9),
                                     # not just probably-eventually true
        },
        log_events=(
            LogEvent(5, "syslog", "WARN", "dcgm-exporter",
                     "GPU {gpu} workload threshold exceeded on {node}",
                     {"gpu": "{gpu}", "metric": "GPU_UTIL"}),
            LogEvent(24, "syslog", "WARN", "dcgm-exporter",
                     "GPU {gpu} temperature warning on {node}: high thermal reading",
                     {"gpu": "{gpu}", "metric": "GPU_TEMP"}),
            LogEvent(62, "syslog", "WARN", "dcgm-exporter",
                     "GPU {gpu} thermal/power warning on {node}: sustained high power draw",
                     {"gpu": "{gpu}", "metric": "POWER_USAGE"}),
        ),
    ),

    # ------------------------------------------------------------------
    "gpu_hardware_degradation": ScenarioDefinition(
        scenario_id="gpu_hardware_degradation",
        display_name="GPU hardware degradation",
        requires_gpu=True,
        default_duration_s=90.0,
        metric_effects={
            # Elevated (but still probabilistic) error rates during the
            # window, PLUS one deterministic bump shortly after start so
            # the scenario is reliably reproducible for tests/demos
            # (spec section 13/18 prefer determinism over pure randomness).
            "ecc_sbe_prob": 0.02,
            "ecc_dbe_prob": 0.003,
            "retired_pending_prob": 0.004,
            "nvlink_crc_prob": 0.01,
            "deterministic_bump_at_s": 8.0,
        },
        log_events=(
            LogEvent(6, "syslog", "WARN", "dcgm-exporter",
                     "GPU {gpu} ECC error detected on {node}",
                     {"gpu": "{gpu}", "metric": "ECC_SBE"}),
            LogEvent(19, "syslog", "ERROR", "dcgm-exporter",
                     "GPU {gpu} memory page retirement pending on {node}",
                     {"gpu": "{gpu}", "metric": "RETIRED_PENDING"}),
            LogEvent(41, "syslog", "WARN", "dcgm-exporter",
                     "GPU {gpu} hardware degradation warning on {node}",
                     {"gpu": "{gpu}", "metric": "HW_DEGRADED"}),
        ),
    ),

    # ------------------------------------------------------------------
    "memory_pressure": ScenarioDefinition(
        scenario_id="memory_pressure",
        display_name="Memory pressure",
        default_duration_s=100.0,
        metric_effects={
            "mem_available_frac_target": 0.06,   # push MemAvailable way down
            "mem_pull": 0.25,
            "swap_free_frac_target": 0.05,
            "swap_pull": 0.2,
            "load_bonus": 0.6,   # extra load, on top of derived CPU busy
        },
        log_events=(
            LogEvent(4, "syslog", "WARN", "kernel",
                     "Memory pressure detected on {node}",
                     {"metric": "MemAvailable"}),
            LogEvent(15, "syslog", "WARN", "kernel",
                     "Allocation warning on {node}: low memory watermark crossed",
                     {"metric": "MemAvailable"}),
            LogEvent(33, "syslog", "WARN", "kernel",
                     "Swap activity increased on {node}",
                     {"metric": "SwapFree"}),
        ),
    ),

    # ------------------------------------------------------------------
    "filesystem_pressure": ScenarioDefinition(
        scenario_id="filesystem_pressure",
        display_name="Filesystem pressure",
        requires_filesystem=True,
        default_duration_s=100.0,
        metric_effects={
            # Base drift is ~5e-7 fraction/sec (tuned for slow real-world
            # disk fill). A 400x multiplier is barely visible over a
            # short demo/test window, so bump it enough to produce a
            # clearly visible drop within tens of seconds.
            "fs_fill_rate_multiplier": 20000.0,
        },
        log_events=(
            LogEvent(7, "syslog", "WARN", "kernel",
                     "Low filesystem space on {node} ({mount})",
                     {"mount": "{mount}"}),
            LogEvent(28, "syslog", "WARN", "kernel",
                     "Filesystem/disk warning on {node} ({mount}): usage critical",
                     {"mount": "{mount}"}),
        ),
    ),

    # ------------------------------------------------------------------
    "node_heartbeat_failure": ScenarioDefinition(
        scenario_id="node_heartbeat_failure",
        display_name="Node/heartbeat failure",
        default_duration_s=75.0,
        metric_effects={
            # exporter.py exposes a small synthetic node_heartbeat_ok gauge;
            # this just flips it to 0 for the incident window.
            "heartbeat_ok": 0,
        },
        log_events=(
            LogEvent(10, "heartbeat", "ERROR", "heartbeat",
                     "Heartbeat missed for {node}",
                     {"heartbeat.event": "HEARTBEAT_MISSED"}),
            LogEvent(11, "consolelog", "ERROR", "conserver",
                     "[-- Console session lost for {node} --]",
                     {}),
            LogEvent(40, "syslog", "ERROR", "clmgr",
                     "Node/service unavailable: {node} not responding",
                     {"metric": "node_heartbeat_ok"}),
        ),
    ),

    # ------------------------------------------------------------------
    "ssh_auth_burst": ScenarioDefinition(
        scenario_id="ssh_auth_burst",
        display_name="SSH authentication burst",
        default_duration_s=45.0,
        metric_effects={},   # intentionally metric-silent, spec section 12
        log_events=(
            LogEvent(2, "syslog", "WARN", "sshd",
                     "drop connection from suspicious source penalty: failed authentication on {node}",
                     {"priority": "86", "facility": "10"}),
            LogEvent(9, "syslog", "WARN", "sshd",
                     "drop connection penalty: failed authentication on {node}",
                     {"priority": "86", "facility": "10"}),
            LogEvent(16, "syslog", "WARN", "sshd",
                     "drop connection penalty: failed authentication on {node}",
                     {"priority": "86", "facility": "10"}),
            LogEvent(23, "syslog", "WARN", "sshd",
                     "connection dropped on {node}: repeated authentication failures",
                     {"priority": "86", "facility": "10"}),
        ),
    ),
}


def get_scenario(scenario_id: str) -> Optional[ScenarioDefinition]:
    return SCENARIOS.get(scenario_id)
