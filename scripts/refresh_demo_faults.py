#!/usr/bin/env python3
"""
Refresh fault scenarios in OpenSearch (http://127.0.0.1:9250) into today's daily indices
with timestamps within the last 1-3 hours so that all 30 mentor demo questions have
active fault data inside the rolling `now-24h` window, and flush the running server's
in-memory cache (`POST http://127.0.0.1:8000/api/v1/admin/reload-skill`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import requests

OS_URL = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9250").rstrip("/")
SERVER_URL = os.environ.get("AGENT_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def main() -> None:
    now = datetime.now(timezone.utc)
    day_suffix = now.strftime("%Y.%m.%d")

    fh_index = f"slingshot_crayfabrichealth-{day_suffix}"
    cl_index = f"controllerlog-{day_suffix}"
    ilo_index = f"log_iml-{day_suffix}"

    t_fault_1 = now - timedelta(minutes=25)
    t_fault_2 = now - timedelta(minutes=85)
    t_fault_3 = now - timedelta(minutes=145)

    # 1. Fabric health fault documents (FH-01..FH-09)
    fh_docs = [
        (
            "mentor-fh-crit-x1000c3r1-1",
            {
                "@timestamp": iso(t_fault_1),
                "timereported": iso(t_fault_1 - timedelta(seconds=2)),
                "fields": {
                    "Switch": "x1000c3r1",
                    "Port": "j104p1",
                    "Location": "x1000c3r1j104p1",
                    "Rack": "x1000",
                    "Chassis": "c3",
                    "Slot": "r1",
                    "Category": "Switch",
                    "Sensor": "FabricHealth",
                    "PhysicalContext": "linkerrors.linkautorestartexhaustederror",
                    "PhysicalSubContext": "CRITICAL",
                    "Value": (
                        "Recommend: Verify cable and optics on port j104p1 and replace transceiver if needed; "
                        "Cause: Port disabled due to excessive flapping (linkerrors.linkautorestartexhaustederror)"
                    ),
                    " Specifics": "linkautorestartexhaustederror on x1000c3r1j104p1",
                },
            },
        ),
        (
            "mentor-fh-crit-x1000c3r1-2",
            {
                "@timestamp": iso(t_fault_2),
                "timereported": iso(t_fault_2 - timedelta(seconds=2)),
                "fields": {
                    "Switch": "x1000c3r1",
                    "Port": "j104p1",
                    "Location": "x1000c3r1j104p1",
                    "Rack": "x1000",
                    "Chassis": "c3",
                    "Slot": "r1",
                    "Category": "Switch",
                    "Sensor": "FabricHealth",
                    "PhysicalContext": "linkerrors.linkautorestartexhaustederror",
                    "PhysicalSubContext": "CRITICAL",
                    "Value": (
                        "Recommend: Verify cable and optics on port j104p1 and replace transceiver if needed; "
                        "Cause: Port disabled due to excessive flapping (linkerrors.linkautorestartexhaustederror)"
                    ),
                    " Specifics": "linkautorestartexhaustederror on x1000c3r1j104p1",
                },
            },
        ),
        (
            "mentor-fh-crit-x1000c1r7-1",
            {
                "@timestamp": iso(t_fault_3),
                "timereported": iso(t_fault_3 - timedelta(seconds=2)),
                "fields": {
                    "Switch": "x1000c1r7b0",
                    "Port": "j102p0",
                    "Location": "x1000c1r7b0j102p0",
                    "Rack": "x1000",
                    "Chassis": "c1",
                    "Slot": "r7",
                    "Category": "Switch",
                    "Sensor": "FabricHealth",
                    "PhysicalContext": "linkerrors.linkautorestartexhaustederror",
                    "PhysicalSubContext": "CRITICAL",
                    "Value": (
                        "Recommend: Inspect connector on port j102p0; "
                        "Cause: Port disabled due to excessive flapping"
                    ),
                    " Specifics": "linkautorestartexhaustederror on x1000c1r7b0j102p0",
                },
            },
        ),
    ]

    for doc_id, doc in fh_docs:
        r = requests.put(f"{OS_URL}/{fh_index}/_doc/{doc_id}", json=doc, timeout=10)
        r.raise_for_status()

    # 2. Controller log fault documents (CL-01..CL-10)
    cl_docs = [
        (
            "mentor-cl-hmpad-x1000c6s7b1-1",
            {
                "@timestamp": iso(now - timedelta(minutes=20)),
                "Timestamp": (now - timedelta(minutes=20)).strftime("%b %d %H:%M:%S"),
                "Severity": "error",
                "Resource": {"host.name": "x1000c6s7b1", "service.name": "hmpad", "process_id": "1650"},
                "Attributes": {"facility": "1", "priority": "11", "type": "log_controllerlog"},
                "Body": "dil_query_bus_pmbus: i2c ioctl failed (rc = -1) for bus number: 14",
            },
        ),
        (
            "mentor-cl-hmpad-x1000c6s7b1-2",
            {
                "@timestamp": iso(now - timedelta(minutes=35)),
                "Timestamp": (now - timedelta(minutes=35)).strftime("%b %d %H:%M:%S"),
                "Severity": "error",
                "Resource": {"host.name": "x1000c6s7b1", "service.name": "hmpad", "process_id": "1650"},
                "Attributes": {"facility": "1", "priority": "11", "type": "log_controllerlog"},
                "Body": "dil_query_bus_pmbus: i2c ioctl failed (rc = -1) for bus number: 17",
            },
        ),
        (
            "mentor-cl-hmscd-x1000c2r3b0-before",
            {
                "@timestamp": iso(t_fault_1 - timedelta(minutes=2)),
                "Timestamp": (t_fault_1 - timedelta(minutes=2)).strftime("%b %d %H:%M:%S"),
                "Severity": "warning",
                "Resource": {"host.name": "x1000c2r3b0", "service.name": "hmscd", "process_id": "1710"},
                "Attributes": {"PortName": "j105p0", "Switch": "x1000c2r3b0", "type": "log_controllerlog"},
                "Body": "hmscd_port_monitor: High symbol error rate detected on port j105p0 before link transition",
            },
        ),
        (
            "mentor-cl-hmscd-x1000c2r3b0-linkdown",
            {
                "@timestamp": iso(t_fault_1 - timedelta(seconds=30)),
                "Timestamp": (t_fault_1 - timedelta(seconds=30)).strftime("%b %d %H:%M:%S"),
                "Severity": "error",
                "Resource": {"host.name": "x1000c2r3b0", "service.name": "hmscd", "process_id": "1710"},
                "Attributes": {
                    "PortName": "j105p0",
                    "Switch": "x1000c2r3b0",
                    "Severity": "Major",
                    "type": "log_controllerlog",
                },
                "Body": "update_redis_network_adapters_link_down: Setting LinkStatus Down for j105p0 (p1)",
            },
        ),
        (
            "mentor-cl-hmscd-x1000c2r3b0-after",
            {
                "@timestamp": iso(t_fault_1 + timedelta(minutes=1)),
                "Timestamp": (t_fault_1 + timedelta(minutes=1)).strftime("%b %d %H:%M:%S"),
                "Severity": "info",
                "Resource": {"host.name": "x1000c2r3b0", "service.name": "hmscd", "process_id": "1710"},
                "Attributes": {"PortName": "j105p0", "Switch": "x1000c2r3b0", "type": "log_controllerlog"},
                "Body": "hmscd_port_monitor: Scheduled link auto-restart attempt 1 for j105p0",
            },
        ),
        (
            "mentor-cl-fabmgr-shortfall-1",
            {
                "@timestamp": iso(t_fault_1),
                "Timestamp": t_fault_1.strftime("%b %d %H:%M:%S"),
                "Severity": "warning",
                "Resource": {"host.name": "x1000c2r3b0", "service.name": "fabric-manager.sh", "process_id": "3540"},
                "Attributes": {"facility": "3", "priority": "30", "type": "log_controllerlog"},
                "Body": (
                    f"[5240][W][{iso(t_fault_1)}][18][8000/fabric/available-agents]"
                    "[getSwitchStates][Switch Availability: 0/1 switches available]"
                ),
            },
        ),
        (
            "mentor-cl-fabmgr-shortfall-2",
            {
                "@timestamp": iso(t_fault_1 - timedelta(minutes=5)),
                "Timestamp": (t_fault_1 - timedelta(minutes=5)).strftime("%b %d %H:%M:%S"),
                "Severity": "warning",
                "Resource": {"host.name": "x1000c3r1b0", "service.name": "fabric-manager", "process_id": "3541"},
                "Attributes": {"facility": "3", "priority": "30", "type": "log_controllerlog"},
                "Body": (
                    f"[5240][W][{iso(t_fault_1 - timedelta(minutes=5))}][18][8000/fabric/available-agents]"
                    "[getSwitchStates][Switch Availability: 3/4 switches available]"
                ),
            },
        ),
    ]

    for doc_id, doc in cl_docs:
        r = requests.put(f"{OS_URL}/{cl_index}/_doc/{doc_id}", json=doc, timeout=10)
        r.raise_for_status()

    # 3. iLO event log fault documents (ILO-01..ILO-11)
    ilo_docs = [
        (
            "mentor-ilo-ipmi-login-1",
            {
                "@timestamp": iso(now - timedelta(minutes=15)),
                "Created": iso(now - timedelta(minutes=16)),
                "host": "leif-login03-bmc",
                "Name": "Integrated Management Log",
                "Severity": "OK",
                "Message": "IPMI/RMCP login: root - 172.24.0.1(DNS name not found).",
                "Oem": {
                    "Hpe": {
                        "Categories": ["Security", "Administration"],
                        "Code": 1024,
                        "Count": 1,
                        "Repaired": False,
                        "Severity": "Informational",
                        "RecommendedAction": "None.",
                        "Updated": iso(now - timedelta(minutes=16)),
                    }
                },
            },
        ),
        (
            "mentor-ilo-unrepaired-warn-1",
            {
                "@timestamp": iso(now - timedelta(minutes=18)),
                "Created": iso(now - timedelta(minutes=19)),
                "host": "leif-login02-bmc",
                "Name": "Integrated Management Log",
                "Severity": "Warning",
                "Message": "System Overheating (Temperature Sensor 1, Location System, Temperature 87C)",
                "Oem": {
                    "Hpe": {
                        "Categories": ["Hardware"],
                        "Code": 3033,
                        "Count": 2,
                        "Repaired": False,
                        "Severity": "Warning",
                        "RecommendedAction": (
                            "Check fan modules and system airflow; replace faulty cooling fan if overheating persists."
                        ),
                        "Updated": iso(now - timedelta(minutes=19)),
                    }
                },
            },
        ),
        (
            "mentor-ilo-unrepaired-crit-1",
            {
                "@timestamp": iso(now - timedelta(minutes=22)),
                "Created": iso(now - timedelta(minutes=23)),
                "host": "leif-login01-bmc",
                "Name": "Integrated Management Log",
                "Severity": "Critical",
                "Message": "Power Supply 2 Failure Detected",
                "Oem": {
                    "Hpe": {
                        "Categories": ["Hardware"],
                        "Code": 3040,
                        "Count": 1,
                        "Repaired": False,
                        "Severity": "Critical",
                        "RecommendedAction": "Verify AC input cable and replace failed power supply unit in Bay 2.",
                        "Updated": iso(now - timedelta(minutes=23)),
                    }
                },
            },
        ),
    ]

    for doc_id, doc in ilo_docs:
        r = requests.put(f"{OS_URL}/{ilo_index}/_doc/{doc_id}", json=doc, timeout=10)
        r.raise_for_status()

    requests.post(f"{OS_URL}/_refresh", timeout=10)
    print(f"Refreshed fault documents into {fh_index}, {cl_index}, {ilo_index}")

    try:
        resp = requests.post(f"{SERVER_URL}/api/v1/admin/reload-skill", timeout=5)
        if resp.ok:
            print("Flushed agent server result & plan caches via /api/v1/admin/reload-skill")
    except Exception:
        pass


if __name__ == "__main__":
    main()
