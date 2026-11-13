#!/usr/bin/env python3
"""
OpenSearch log generator -- the log-side projection of the simulation.

Produces documents in the exact shape of your three exported indices:
    controllerlog-YYYY.MM.DD               Slingshot switch-controller syslog
    log_iml-YYYY.MM.DD                     HPE iLO event/management log (Redfish LogEntry)
    slingshot_crayfabrichealth-YYYY.MM.DD  switch fabric-health heartbeats

Two kinds of content:
  1. CORRELATED  -- rendered from the controller's "log intents" (the same events
                    that move the Prometheus metrics). Tagged with a `sim` field so
                    you can join them back to an event.
  2. AMBIENT     -- benign background noise (cron, logins, heartbeats) with no
                    Prometheus counterpart, so the correlated lines stand out.
"""
import calendar
import hashlib
import heapq
import itertools
import json
import os
import random
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import requests
import yaml

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200").rstrip("/")
DASHBOARDS_URL = os.environ.get("DASHBOARDS_URL", "").rstrip("/")      # empty = skip
CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://localhost:8000").rstrip("/")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
ADD_SIM_FIELD = os.environ.get("ADD_SIM_FIELD", "true").lower() == "true"
HEARTBEAT_S = float(os.environ.get("HEARTBEAT_S", "60"))                # per switch
AMBIENT_CONTROLLERLOG_PER_S = float(os.environ.get("AMBIENT_CONTROLLERLOG_PER_S", "0.5"))
ILO_LOGIN_EVERY_S = float(os.environ.get("ILO_LOGIN_EVERY_S", "60"))    # per BMC
FLUSH_S = float(os.environ.get("FLUSH_S", "2"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))              # 0 = keep forever
RETENTION_CHECK_S = float(os.environ.get("RETENTION_CHECK_S", "3600"))
ILO_IEL_RATE = 2.5 / max(float(os.environ.get("ILO_LOGIN_EVERY_S", "60")), 1.0)   # entries/s, see _next_ilo
ILO_IML_RATE = 0.01   # hardware-log entries/s; generous so the time curve stays ahead of event bursts
POLL_S = float(os.environ.get("POLL_S", "1"))


def log(msg):
    print(f"[loggen] {msg}", flush=True)


with open(os.path.join(CONFIG_DIR, "topology.yaml")) as f:
    TOPO = yaml.safe_load(f)
with open(os.path.join(CONFIG_DIR, "log_catalog.yaml")) as f:
    CAT = yaml.safe_load(f)

SWITCHES = TOPO["switches"]
BMC_HOSTS = sorted({n["bmc_host"] for n in TOPO["nodes"].values()} | set(TOPO.get("ilo_counters", {})))


class SafeDict(defaultdict):
    def __missing__(self, key):
        return ""


def fmt(template, ctx):
    return str(template).format_map(SafeDict(str, ctx))


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def iso_ns(ts):
    sec = int(ts)
    ns = int((ts - sec) * 1e9)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(sec)) + f".{ns:09d}Z"


def iso_ms(ts):
    return iso_ns(ts)[:23] + "Z"


def iso_s(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))


def syslog_ts(ts):
    return time.strftime("%b %e %H:%M:%S", time.gmtime(int(ts)))


def index_name(prefix, ts):
    return f"{prefix}-{time.strftime('%Y.%m.%d', time.gmtime(ts))}"


# Virtual clock: backfill.py replays past time by setting this. None = real time.
_VNOW = None
BACKFILL_RUN = None            # set by backfill.py: tags docs + makes every _id deterministic
_PUT_N = itertools.count()


def clock():
    return _VNOW if _VNOW is not None else time.time()


def set_virtual_now(t):
    global _VNOW
    _VNOW = t


def ingest_ts(ts):
    """@timestamp is when the pipeline ingested the line: never before it happened."""
    if _VNOW is not None:           # backfill: the replayed instant, plus a realistic ingest lag
        return ts + 0.4
    return max(ts, time.time())


# --------------------------------------------------------------------------
# Identity resolution (Prometheus node -> log identities)
# --------------------------------------------------------------------------
def ident(node):
    t = TOPO["nodes"].get(node)
    if t:
        return t
    h = int(hashlib.sha1(node.encode()).hexdigest(), 16)
    sw = SWITCHES[h % len(SWITCHES)]
    return {"bmc_host": f"{node}-bmc", "ilo_generation": 6, "switch": sw,
            "port": {"name": f"{sw[:-2]}j24p1", "number": 36}}


def port_j(port_name):
    m = re.search(r"(j\d+p\d+)$", port_name)
    return m.group(1) if m else port_name


_BOOT_TS: dict[str, float] = {}


def uptime(host, ts):
    """Fake kernel uptime seconds for `host` at time ts."""
    if host not in _BOOT_TS:
        _BOOT_TS[host] = clock() - random.uniform(2e5, 2e6)
    return ts - _BOOT_TS[host]


# --------------------------------------------------------------------------
# Document builders -- mirror the exported schemas field-for-field
# --------------------------------------------------------------------------
_ILO_CTR: dict[tuple, int] = {}


def _ilo_as_of():
    raw = TOPO.get("ilo_counters_as_of")
    if raw:
        return calendar.timegm(time.strptime(raw, "%Y-%m-%dT%H:%M:%SZ"))
    return time.time()


_ILO_AS_OF = _ilo_as_of()   # the date your iLO export's Id/EventNumber values were taken


def _next_ilo(host, which, ts):
    """Id / EventNumber for a new iLO entry.

    They are derived from the entry's TIMESTAMP (base value from your export, advancing
    at a fixed rate since the export date) and forced strictly increasing per host+log.
    Because it depends only on time, backfilled history and live data line up, and a
    restart never makes the counters jump backwards."""
    c = TOPO.get("ilo_counters", {}).get(host)
    if c and which == "IEL":
        base_id, base_ev = c["id"], c["event_number"]
    else:
        h = int(hashlib.sha1(f"{host}-{which}".encode()).hexdigest(), 16)
        base_id, base_ev = 5 + h % 50, 8000 + h % 1000
    rate = ILO_IEL_RATE if which == "IEL" else ILO_IML_RATE
    ev = base_ev + int(max(0.0, ts - _ILO_AS_OF) * rate)
    prev = _ILO_CTR.get((host, which))
    if prev is not None and ev <= prev:
        ev = prev + 1
    _ILO_CTR[(host, which)] = ev
    return base_id + (ev - base_ev), ev


def ilo_doc(ts, host, gen, which, severity, hpe_severity, code, cls, cls_desc,
            categories, message, recommended="", count=1):
    entry_id, event_number = _next_ilo(host, which, ts)
    is_iml = which == "IML"
    learn = (f"http://www.hpe.com/support/class0x{cls:04x}code0x{code:04x}-gen{10 if gen == 5 else 11}"
             if is_iml else "")
    return {
        "Created": iso_s(ts),
        "odata_context": "/redfish/v1/$metadata#LogEntry.LogEntry",
        "odata_type": "#LogEntry.v1_11_0.LogEntry",
        "Name": "Integrated Management Log" if is_iml else "iLO Event Log",
        "EntryType": "Oem",
        "Severity": severity,
        "@timestamp": iso_ns(ingest_ts(ts)),
        "Oem": {"Hpe": {
            "odata_context": "/redfish/v1/$metadata#HpeLogEntry.HpeLogEntry",
            "EventNumber": event_number,
            "odata_type": "#HpeLogEntry.v2_4_0.HpeLogEntry",
            "Severity": hpe_severity,
            "Repaired": False,
            "Class": cls,
            "ClassDescription": cls_desc,
            "Categories": categories,
            "Updated": iso_s(ts),
            "RecommendedAction": recommended,
            "LearnMoreLink": learn,
            "Count": count,
            "Code": code,
        }},
        "OemRecordFormat": "Hpe-IML" if is_iml else "Hpe-iLOEventLog",
        "Id": str(entry_id),
        "@version": "1",
        "Message": message,
        "odata_etag": "",
        "odata_id": (f"/redfish/v1/Systems/1/LogServices/IML/Entries/{entry_id}" if is_iml
                     else f"/redfish/v1/Managers/1/LogServices/IEL/Entries/{entry_id}"),
        "host": host,
    }


def controllerlog_doc(ts, host, service, severity, body, priority, facility, pid="",
                      attrs_extra=None, ts_array=False):
    sl = syslog_ts(ts)
    attrs = {"priority": str(priority), "facility": str(facility), "type": "log_controllerlog"}
    if attrs_extra:
        attrs = {"type": "log_controllerlog", **attrs_extra, "priority": str(priority),
                 "facility": str(facility)}
    return {
        "Severity": severity,
        "Resource": {"process_id": str(pid), "host.name": host, "service.name": service},
        "Body": body,
        "@timestamp": iso_ns(ingest_ts(ts)),
        "@version": "1",
        "Timestamp": [sl, iso_ms(ts)] if ts_array else sl,
        "Attributes": attrs,
    }


_XNAME = re.compile(r"x(\d+)c(\d+)r(\d+)b(\d+)")


def fabric_doc(ts, switch, degraded_ctx=None):
    m = _XNAME.match(switch)
    rack, chassis, slot, asic = f"x{m.group(1)}", f"c{m.group(2)}", f"r{m.group(3)}", f"b{m.group(4)}"
    if rack == "x1000":
        index = 4 * int(m.group(2)) + (int(m.group(3)) - 1) // 2     # matches your sample's Index
    else:
        index = SWITCHES.index(switch) if switch in SWITCHES else 99
    fh = CAT["fabric_health"]
    if degraded_ctx:
        sub, value = fh["degraded_state"], fmt(fh["degraded"], degraded_ctx)
        port_val = degraded_ctx.get("port_j")
        loc_val = f"{switch}{port_val}" if port_val else switch
        phys_ctx = degraded_ctx.get("physical_context", "runtime.heartbeat")
    else:
        sub, value = "HEALTHY", fh["healthy"]
        port_val = None
        loc_val = switch
        phys_ctx = "runtime.heartbeat"
    ms = int(ts * 1000)
    fields_obj = {
        "DeviceSpecificContext": "", "Switch": switch, "Slot": slot,
        "PhysicalContext": phys_ctx, "Asic": asic, "ParentalIndex": 1,
        "Location": loc_val, "Index": index, "SstValue": 0, "SubIndex": -1,
        "Value": value, "Chassis": chassis, "PhysicalSubContext": sub,
        "Category": "CrayFabricHealth", "Rack": rack, "Timestamp": ms,
    }
    if port_val:
        fields_obj["Port"] = port_val
    return {
        "timestamp": int(ts),
        "_tags": [{}],
        "tags": ["_tagsparsefailure"],
        "@timestamp": iso_ns(ingest_ts(ts)),
        "fields": fields_obj,
        "name": "CrayFabricHealth",
        "@version": "1",
    }


# --------------------------------------------------------------------------
# Bulk writer
# --------------------------------------------------------------------------
class Bulk:
    def __init__(self):
        self.q = deque()
        self.lock = threading.Lock()
        self.sent = 0
        self.failed_batches = 0

    def add(self, index, doc, _id=None):
        with self.lock:
            if len(self.q) > 100_000:
                self.q.popleft()          # backpressure: drop oldest if OpenSearch is down for long
            self.q.append((index, doc, _id))

    def _drain(self, n=500):
        with self.lock:
            items = [self.q.popleft() for _ in range(min(n, len(self.q)))]
        return items

    def flush(self):
        while True:
            items = self._drain()
            if not items:
                return
            lines = []
            for index, doc, _id in items:
                meta = {"_index": index}
                if _id:
                    meta["_id"] = _id
                lines.append(json.dumps({"index": meta}))
                lines.append(json.dumps(doc))
            try:
                r = requests.post(f"{OPENSEARCH_URL}/_bulk", data="\n".join(lines) + "\n",
                                  headers={"Content-Type": "application/x-ndjson"}, timeout=15)
                r.raise_for_status()
                body = r.json()
                if body.get("errors"):
                    bad = [i for i in body["items"] if next(iter(i.values())).get("error")]
                    log(f"bulk: {len(bad)} doc errors, first: {next(iter(bad[0].values()))['error']}")
                self.sent += len(items)
            except Exception as exc:
                self.failed_batches += 1
                with self.lock:
                    self.q.extendleft(reversed(items))     # put back, retry next flush
                log(f"bulk failed ({exc!r}); {len(self.q)} queued, retrying")
                return

    def run(self):
        while True:
            time.sleep(FLUSH_S)
            self.flush()


BULK = Bulk()


def put(index_prefix, doc, ts, intent=None, _id=None):
    if intent is not None and ADD_SIM_FIELD:
        doc["sim"] = {"event_id": intent["event_id"], "event_type": intent["event_type"],
                      "phase": intent["phase"], "node": intent["node"]}
        if intent.get("gpu") is not None:
            doc["sim"]["gpu"] = intent["gpu"]
        if BACKFILL_RUN:
            doc["sim"]["backfilled"] = True
    if BACKFILL_RUN and _id is None:          # re-running the same backfill overwrites, never duplicates
        _id = hashlib.sha1(f"{BACKFILL_RUN}-{next(_PUT_N)}".encode()).hexdigest()[:20]
    BULK.add(index_name(index_prefix, ingest_ts(ts)), doc, _id)


# --------------------------------------------------------------------------
# Correlated: intents -> documents
# --------------------------------------------------------------------------
DEGRADED: dict[str, dict] = {}           # switch -> ctx while a link-down event is active
_ROSETTA_COUNT: dict[str, int] = {}      # event_id -> running error count
_ROSETTA_CNTR: dict[str, int] = {}       # switch -> running frf_empty_route_edge_cntr
_ROSETTA_SEQ: dict[str, int] = {}        # host -> running [seq] in the body


def det_id(intent, n):
    raw = f"{intent['boot_id']}-{intent['seq']}-{n}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def build_ctx(intent, idt):
    gpu = intent.get("gpu")
    p = idt["port"]
    ctx = {
        "node": intent["node"], "event_id": intent["event_id"],
        "gpu": gpu if gpu is not None else "", "slot": (gpu + 1) if gpu is not None else "",
        "sensor": (10 + gpu) if gpu is not None else "",
        "bmc_host": idt["bmc_host"], "switch": idt["switch"],
        "port_name": p["name"], "port_number": p["number"], "port_j": port_j(p["name"]),
        "cpu_temp": round(60 + 32 * float(intent.get("intensity", 1.0)) + random.uniform(-2, 2)),
        "gpu_temp": round(88 + random.uniform(0, 6)),
        "count": intent.get("count", 1), "total": intent.get("total", intent.get("count", 1)),
        "uptime": f"{uptime(idt['switch'], intent['ts']):.6f}",
    }
    return ctx


def render_entry(entry, ctx, intent, idt, n):
    ts = intent["ts"]
    if entry["index"] == "log_iml":
        doc = ilo_doc(
            ts, idt["bmc_host"], idt.get("ilo_generation", 6), entry.get("log", "IML"),
            entry["severity"], entry["hpe_severity"], entry["code"], entry["class"],
            entry["class_description"], list(entry["categories"]), fmt(entry["message"], ctx),
            entry.get("recommended_action", ""), count=int(ctx["count"] or 1))
        put("log_iml", doc, ts, intent, det_id(intent, n))
        return
    host = fmt(entry.get("host", "{switch}"), ctx)
    if entry.get("kind") == "rosetta":
        eid, sw = intent["event_id"], idt["switch"]
        if entry.get("is_first"):
            _ROSETTA_COUNT[eid] = 0
        else:
            _ROSETTA_COUNT[eid] = _ROSETTA_COUNT.get(eid, 0) + random.randint(1, 5)
        _ROSETTA_CNTR[sw] = _ROSETTA_CNTR.get(sw, 230) + random.randint(1, 40)
        _ROSETTA_SEQ[host] = _ROSETTA_SEQ.get(host, 5000) + random.randint(1, 3)
        time_set = f"{uptime(sw, ts):.9f}"
        is_first = "true" if entry.get("is_first") else "false"
        body = fmt(CAT["rosetta_body"], {
            **ctx, "seq": _ROSETTA_SEQ[host], "iso_ms": iso_ms(ts), "is_first": is_first,
            "time_set": time_set, "count": _ROSETTA_COUNT[eid], "cntr": _ROSETTA_CNTR[sw]})
        doc = controllerlog_doc(
            ts, host, "fabric-manager.sh", "info", body, 30, 3, pid=random.randint(3500, 3700),
            ts_array=True,
            attrs_extra={"Severity": "S", "Throttled": "false", "isFirst": is_first,
                         "PortNumber": str(ctx["port_number"]), "Block": "frf",
                         "Flag": "empty_route_edge", "Category": "EC_UNCOR_NS",
                         "Count": str(_ROSETTA_COUNT[eid]), "TimeSet": time_set,
                         "PortName": ctx["port_name"]})
    else:
        pid = entry["pid"] if "pid" in entry else random.randint(*entry.get("pid_range", [1000, 2000]))
        doc = controllerlog_doc(ts, host, entry["service"], entry["severity"], fmt(entry["body"], ctx),
                                entry["priority"], entry["facility"], pid=pid)
    put("controllerlog", doc, ts, intent, det_id(intent, n))


def handle_intent(intent):
    idt = ident(intent["node"])
    ctx = build_ctx(intent, idt)
    entries = (CAT.get(intent["event_type"]) or {}).get(intent["phase"], [])
    n = 0
    for entry in entries:
        when = entry.get("when")
        if when and any(intent.get("params", {}).get(k) != v for k, v in when.items()):
            continue
        render_entry(entry, ctx, intent, idt, n)
        n += 1
    if intent["event_type"] == "fabric_link_down":
        sw = idt["switch"]
        if intent["phase"] == "clear":
            DEGRADED.pop(sw, None)
        else:
            DEGRADED[sw] = ctx
        if intent["phase"] in ("onset", "clear"):       # show the state change right away
            put("slingshot_crayfabrichealth", fabric_doc(intent["ts"], sw, DEGRADED.get(sw)),
                intent["ts"], intent, det_id(intent, 99))


def intent_loop():
    cursor, last_boot, was_ok = -1, None, None
    while True:
        try:
            r = requests.get(f"{CONTROLLER_URL}/log-intents", params={"after": cursor, "limit": 1000},
                             timeout=5)
            r.raise_for_status()
            data = r.json()
            if was_ok is not True:
                log("connected to controller")
                was_ok = True
            if last_boot is not None and data["boot_id"] != last_boot:
                log("controller restarted: resetting cursor")
                cursor = -1
                DEGRADED.clear()
                last_boot = data["boot_id"]
                continue
            last_boot = data["boot_id"]
            for it in data["intents"]:
                handle_intent(it)
                cursor = max(cursor, it["seq"])
            if len(data["intents"]) >= 1000:
                continue
        except Exception as exc:
            if was_ok is not False:
                log(f"controller unreachable ({exc!r}); will keep retrying")
                was_ok = False
        time.sleep(POLL_S)


# --------------------------------------------------------------------------
# Ambient noise (scheduled with a heap of due-times)
# --------------------------------------------------------------------------
_HEAP: list = []
_TIE = itertools.count()


def schedule(due, fn, *args):
    heapq.heappush(_HEAP, (due, next(_TIE), fn, args))


def gen_heartbeat(switch):
    now = clock()
    ctx = DEGRADED.get(switch)
    # A degraded heartbeat is a CORRELATED document: tag it with the event that caused it.
    tag = ({"event_id": ctx["event_id"], "event_type": "fabric_link_down",
            "phase": "ongoing", "node": ctx["node"]} if ctx else None)
    put("slingshot_crayfabrichealth", fabric_doc(now, switch, ctx), now, tag)
    schedule(now + HEARTBEAT_S + random.uniform(-5, 5), gen_heartbeat, switch)


def gen_ilo_login(host, gen):
    now = clock()
    ssh = gen == 6 and random.random() < 0.15
    kind_in, kind_out = ("ssh_login", "ssh_logout") if ssh else ("browser_login", "browser_logout")
    ip = random.choice(["172.24.0.1"] * 8 + ["172.24.0.2", "172.24.0.10"])
    for k, when in ((kind_in, now), (kind_out, now + random.uniform(1, 3))):
        schedule(when, emit_ilo_ambient, host, gen, k, ip)
    schedule(now + ILO_LOGIN_EVERY_S + random.uniform(-30, 30), gen_ilo_login, host, gen)


def emit_ilo_ambient(host, gen, kind, ip):
    ts = clock()
    spec = CAT["ambient_ilo"][kind]
    doc = ilo_doc(ts, host, gen, "IEL", "OK", "Informational", spec["code"], 35, f"iLO {gen}",
                  ["Security", "Administration"], fmt(spec["message"], {"ip": ip}),
                  count=random.randint(1, 20))
    put("log_iml", doc, ts)


_AMB = CAT["ambient_controllerlog"]
_AMB_W = [e["weight"] for e in _AMB]


def gen_ambient_controllerlog():
    now = clock()
    e = random.choices(_AMB, weights=_AMB_W)[0]
    host = random.choice(SWITCHES)
    pid = e["pid"] if "pid" in e else random.randint(*e.get("pid_range", [1000, 2000]))
    if e.get("kind") == "available_agents":
        avail = 0 if host in DEGRADED else 1
        body = (f"[{random.randint(4900, 5300)}][I][{iso_ms(now)}][{random.randint(17, 21)}]"
                f"[8000/fabric/available-agents][getSwitchStates][Switch Availability: {avail}/1 switches available]")
    else:
        body = e["body"]
    put("controllerlog", controllerlog_doc(now, host, e["service"], e["severity"], body,
                                           e["priority"], e["facility"], pid=pid), now)
    schedule(now + random.expovariate(AMBIENT_CONTROLLERLOG_PER_S), gen_ambient_controllerlog)


def seed_ambient(t0):
    """Schedule the first run of every ambient stream, starting at time t0."""
    for sw in SWITCHES:
        schedule(t0 + random.uniform(0, HEARTBEAT_S), gen_heartbeat, sw)
    gens = {n["bmc_host"]: n.get("ilo_generation", 6) for n in TOPO["nodes"].values()}
    for host in BMC_HOSTS:
        schedule(t0 + random.uniform(0, ILO_LOGIN_EVERY_S), gen_ilo_login, host, gens.get(host, 6))
    if AMBIENT_CONTROLLERLOG_PER_S > 0:
        schedule(t0 + 1, gen_ambient_controllerlog)


def ambient_loop():
    seed_ambient(time.time())
    while True:
        now = time.time()
        while _HEAP and _HEAP[0][0] <= now:
            _, _, fn, args = heapq.heappop(_HEAP)
            try:
                fn(*args)
            except Exception as exc:
                log(f"ambient error in {fn.__name__}: {exc!r}")
        time.sleep(0.2)


# --------------------------------------------------------------------------
# OpenSearch / Dashboards setup
# --------------------------------------------------------------------------
def _tmpl(patterns, props):
    return {"index_patterns": patterns, "priority": 100,
            "template": {"settings": {"number_of_shards": 1, "number_of_replicas": 0},
                         "mappings": {"properties": {
                             "@timestamp": {"type": "date"},
                             "sim": {"properties": {k: {"type": "keyword"} for k in
                                                    ("event_id", "event_type", "phase", "node")}
                                     | {"gpu": {"type": "integer"},
                                                                     "backfilled": {"type": "boolean"}}},
                             **props}}}}


TEMPLATES = {
    "sim-controllerlog": _tmpl(["controllerlog-*"], {}),
    "sim-log-iml": _tmpl(["log_iml-*"], {"Created": {"type": "date"}}),
    "sim-crayfabrichealth": _tmpl(
        ["slingshot_crayfabrichealth-*"],
        {
            "timestamp": {"type": "long"},
            "fields": {
                "properties": {
                    "Port": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                    }
                }
            },
        },
    ),
}
INDEX_PATTERNS = {"sim-controllerlog": "controllerlog-*", "sim-log-iml": "log_iml-*",
                  "sim-crayfabrichealth": "slingshot_crayfabrichealth-*"}


def wait_for_opensearch():
    log(f"waiting for OpenSearch at {OPENSEARCH_URL} ...")
    while True:
        try:
            r = requests.get(f"{OPENSEARCH_URL}/_cluster/health", timeout=3)
            if r.ok:
                log(f"OpenSearch is up (status {r.json().get('status')})")
                return
        except Exception:
            pass
        time.sleep(3)


def install_templates():
    for name, body in TEMPLATES.items():
        r = requests.put(f"{OPENSEARCH_URL}/_index_template/{name}", json=body, timeout=10)
        if not r.ok:
            log(f"WARNING: index template {name} failed: {r.status_code} {r.text[:200]}")
    log("index templates installed")


def setup_dashboards():
    if not DASHBOARDS_URL:
        return
    for attempt in range(60):
        try:
            if requests.get(f"{DASHBOARDS_URL}/api/status", timeout=3).ok:
                break
        except Exception:
            pass
        time.sleep(5)
    else:
        log("Dashboards never became reachable; create index patterns manually (see README)")
        return
    for pid, title in INDEX_PATTERNS.items():
        try:
            r = requests.post(f"{DASHBOARDS_URL}/api/saved_objects/index-pattern/{pid}?overwrite=true",
                              json={"attributes": {"title": title, "timeFieldName": "@timestamp"}},
                              headers={"osd-xsrf": "true"}, timeout=10)
            log(f"dashboards index pattern {title}: HTTP {r.status_code}")
        except Exception as exc:
            log(f"dashboards index pattern {title} failed: {exc!r}")


_IDX_RE = re.compile(r"^(controllerlog|log_iml|slingshot_crayfabrichealth)-(\d{4})\.(\d{2})\.(\d{2})$")


def retention_keep_from():
    """Epoch seconds of the earliest instant that survives retention (None = keep forever)."""
    if RETENTION_DAYS <= 0:
        return None
    today = datetime.now(timezone.utc).date()
    first_kept = today - timedelta(days=RETENTION_DAYS - 1)
    return datetime(first_kept.year, first_kept.month, first_kept.day, tzinfo=timezone.utc).timestamp()


def cleanup_old_indices():
    """Delete daily indices whose DATE (from the index name) is RETENTION_DAYS or more days old.

    Deliberately name-based, not OpenSearch ISM: ISM counts from the index's *creation* time,
    so a backfilled index for an old date, created today, would survive another week."""
    if RETENTION_DAYS <= 0:
        return []
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=RETENTION_DAYS)
    pats = "controllerlog-*,log_iml-*,slingshot_crayfabrichealth-*"
    r = requests.get(f"{OPENSEARCH_URL}/_cat/indices/{pats}", params={"format": "json", "h": "index"},
                     timeout=15)
    r.raise_for_status()
    deleted = []
    for row in r.json():
        m = _IDX_RE.match(row["index"])
        if not m:
            continue
        day = datetime(int(m[2]), int(m[3]), int(m[4])).date()
        if day <= cutoff:
            d = requests.delete(f"{OPENSEARCH_URL}/{row['index']}", timeout=30)
            if d.ok:
                deleted.append(row["index"])
            else:
                log(f"retention: could not delete {row['index']}: {d.status_code}")
    if deleted:
        log(f"retention ({RETENTION_DAYS}d): deleted {len(deleted)} old index(es): {', '.join(sorted(deleted))}")
    return deleted


def retention_loop():
    while True:
        try:
            cleanup_old_indices()
        except Exception as exc:
            log(f"retention check failed ({exc!r}); will retry")
        time.sleep(RETENTION_CHECK_S)


def stats_loop():
    while True:
        time.sleep(30)
        log(f"indexed={BULK.sent} queued={len(BULK.q)} failed_batches={BULK.failed_batches}")


def main():
    log(f"opensearch={OPENSEARCH_URL} controller={CONTROLLER_URL} sim_field={ADD_SIM_FIELD}")
    wait_for_opensearch()
    install_templates()
    log(f"retention: {RETENTION_DAYS} days" if RETENTION_DAYS > 0 else "retention: keep forever")
    for target in (BULK.run, ambient_loop, intent_loop, stats_loop, retention_loop):
        threading.Thread(target=target, daemon=True).start()
    threading.Thread(target=setup_dashboards, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        BULK.flush()


if __name__ == "__main__":
    main()
