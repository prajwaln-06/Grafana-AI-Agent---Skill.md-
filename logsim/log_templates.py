"""
Document construction (mentor-provided schema, spec section 5) and normal
background log activity (spec section 6/11: heartbeat/console/syslog/ssh
noise so incident logs sit inside realistic surrounding traffic rather than
being the only thing in the index).
"""
from __future__ import annotations

import random

from logsim.sinks import now_iso


def build_doc(*, service, host, body, severity="INFO", attributes=None,
              severity_text_key="Severity"):
    """Shared envelope for all three index types. Field names/casing match
    the mentor's Phase 1 examples exactly (spec section 5) -- including the
    slightly inconsistent Severity/SeverityText key some examples use."""
    ts = now_iso()
    doc = {
        "@timestamp": ts,
        "Attributes": attributes or {},
        "Resource": {
            "host.name": host,
            "service.name": service,
        },
        "Body": body,
        "@version": "1",
        "Timestamp": ts,
    }
    doc[severity_text_key] = severity
    return doc


# --------------------------------------------------------------------
# Background (normal) activity -- one function per index family. Called
# on a per-node timer by the simulator loop; nodes currently affected by
# a node_heartbeat_failure incident are skipped (see simulator.py) so the
# "silence" itself is part of the evidence, matching the spec's "keep
# unaffected nodes healthy / incident logs sit inside normal noise" rule.
# --------------------------------------------------------------------

def heartbeat_doc(node):
    pid = random.randint(1000, 9000)
    return build_doc(
        service="heartbeat", host=node,
        body="EVENT:HEARTBEAT APP:clmgr-power SEV:LOG_INFO TEXT:Heartbeat detected",
        severity="INFO",
        attributes={
            "heartbeat.text": "Heartbeat detected",
            "heartbeat.event": "HEARTBEAT",
            "heartbeat.app": "clmgr-power",
            "process.pid": pid,
        },
        severity_text_key="SeverityText",
    ), "heartbeat"


_CONSOLE_LINES = [
    "[-- Console up -- ]",
    "[-- MARK -- ]",
    "login: session opened for user svc-monitor",
    "systemd-journald[512]: Runtime journal reduced online",
]


def console_doc(node):
    line = random.choice(_CONSOLE_LINES)
    return build_doc(
        service="conserver", host=node,
        body=line,
        severity="INFO",
        attributes={"file.path": f"/var/log/consoles/{node}"},
    ), "consolelog"


_SYSLOG_NORMAL = [
    ("sshd", "Accepted publickey for deploy from 10.0.{0}.{1} port {2} ssh2"),
    ("systemd", "Started Daily apt download activities."),
    ("cron", "(root) CMD (   cd / && run-parts --report /etc/cron.hourly)"),
    ("kernel", "TCP: request_sock_TCP: Possible SYN flooding, sending cookies"),
]


def syslog_doc(node):
    service, template = random.choice(_SYSLOG_NORMAL)
    body = template.format(random.randint(0, 255), random.randint(2, 254),
                            random.randint(1024, 65000))
    return build_doc(
        service=service, host=node, body=body, severity="info",
        attributes={
            "priority": str(random.choice([6, 14, 30, 86])),
            "type": "log_syslog",
            "facility": str(random.choice([1, 3, 4, 10])),
        },
    ), "syslog"
