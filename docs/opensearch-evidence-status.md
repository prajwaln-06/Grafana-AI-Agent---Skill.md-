# OpenSearch Evidence Readiness and Metadata Status

## 1. Overview & Operational Context

This document tracks the verification status, empirical evidence, and operational assumptions for all OpenSearch index families supported by the Grafana AI Agent (`fabric_health`, `controller_log`, `ilo_events`).

### Critical Environment Boundary
* **Live Cluster Access:** Not available in this local environment.
* **Fail-Closed Principle:** No provisional assumption is promoted to "verified" without direct evidence from a live cluster.
* **Synthetic Fixtures:** Fixtures in `tests/fixtures/opensearch/` represent synthetic test contracts, not cluster exports.
* **Conservative Mapping:** Features with unverified wording or codes (such as failed-login detection) remain intentionally unsupported (`status: "unmapped"`).

---

## 2. Evidence Status Matrix

| Area / Target | Current Code Status | Current Evidence Basis | Missing Evidence | Classification | Verification Procedure |
|---|---|---|---|---|---|
| **iLO Categories** (`Oem.Hpe.Categories` in `ilo_events`) | Unverified | Synthetic mapping confirms `text` with `.keyword` subfield. No registered enum values. | Full list of distinct category strings emitted by real BMCs. | **Unknown** (Discovered dynamically, not hardcoded) | Execute `terms` aggregation on `Oem.Hpe.Categories.keyword` over 24h–7d window. Sample representative documents. |
| **iLO Severity** (`Oem.Hpe.Severity` in `ilo_events`) | Provisional (`verification: "provisional"`) | Observed `Informational` and `Caution` in historical test notes. `Warning` and `Critical` from HPE documentation. | Live cluster distribution across normal and abnormal events. | **Provisional** (System flags `severity_vocabulary_provisional`) | Run distinct-value `terms` aggregation on `Oem.Hpe.Severity.keyword`. Correlate with Redfish `Severity`. |
| **CL-05 Switch Availability** (`text_ratio_shortfall` in `controller_log`) | Placeholder (`verified: false`) | Provisional regex: `(?i)(\d+)\s*/\s*(\d+)\s+switches\s+available`. | Actual log messages emitted by `fabric-manager` during normal operations and degradation. | **Placeholder** (Flagged in `summary.pattern_verified: false`) | Search `Resource.service.name: "fabric-manager"` for switch count messages. Capture real regex variations. |
| **FH-10 Heartbeat Interval** (`absence` in `fabric_health`) | Placeholder (`verified: false`) | Assumed 60 seconds (`heartbeat_interval_seconds: 60`). | Timestamp inter-arrival distribution of `PhysicalContext: "runtime.heartbeat"` documents. | **Placeholder** (Flagged in `summary.threshold_verified: false`) | Calculate inter-arrival time ($\Delta t$) between consecutive heartbeats per switch ASIC. |
| **FH-10 Missing Threshold** (`absence` in `fabric_health`) | Placeholder (`verified: false`) | Assumed 3 missed intervals (`dead_after_intervals: 3`, defaulting `recent_window` to 3m). | Operational metrics on heartbeat jitter and transient network drops. | **Placeholder** (Configurable via `recent_window`) | Analyze historical heartbeat gaps during known switch maintenance or power-off events. |
| **ILO-06 Failed Logins** (`ilo_events`) | Unmapped by design (`status: "unmapped"`) | No failed-login records observed or verified. No failure codes registered. | Sanitized BMC logs of failed browser, SSH, and IPMI login attempts. | **Unmapped** (Safely declined; no queries sent) | Inspect BMC security logs during deliberate failed authentication tests; capture event code and message text. |
| **Live Index Mappings** (all three families) | Synthetic baseline only | Handcrafted `mapping_*.json` in `tests/fixtures/opensearch/`. | Sanitized cluster `_mapping` exports from target deployment. | **Not accessible** (Local cluster unavailable) | Run `scripts/smoke_test_opensearch.py --save-mappings tests/fixtures/opensearch/live/`. |
| **Date Rotation Pattern** (all three families) | Unverified (`date_rotation.verified: false`) | Assumed `-YYYY.MM.dd` daily rotation suffix. | Concrete index names from `_cat/indices` on live cluster. | **Provisional** (Registry permits alias or wildcard matching) | Query `GET /_cat/indices/<pattern>*?h=index` to confirm rotation suffix and frequency. |
| **Controller Context Window** (`context_window` in `controller_log`) | Documented default (`verified: false`) | Assumed 5m before / 5m after anchor event. | Empirical duration of controller incident blast radius. | **Provisional** (Configurable by user in prompt) | Analyze syslog cluster around `LinkStatus Down` events. |
| **Fabric Recurrence Interval** (`recurrence` in `fabric_health`) | Placeholder (`verified: false`) | Assumed 1h histogram interval (`interval: "1h"`). | Re-emission schedule of persistent Slingshot switch/port faults. | **Placeholder** (Documents re-emitted hourly) | Measure re-emission frequency of unresolved port faults over 7-day period. |
| **iLO Ingest Lag Time Field** (`ingest_lag` in `ilo_events`) | Unverified (`verified: false`) | Uses display field `Created` vs `@timestamp`. | Clock synchronization and reliability of BMC `Created` timestamp. | **Provisional** (Negative lag explicitly warned) | Compute `(@timestamp - Created)` across sample BMC documents to measure clock drift. |

---

## 3. Provenance of Historical Data Claims

Previous documentation and commit messages (`e5cc5da`) cited:
* *48 `DEGRADED` documents in 24 hours*
* *0 `CRITICAL` documents in 7 days*
* *120,645-document sampled iLO export*

### Provenance Audit Findings
1. **Source of Claims:** These figures were recorded in commit `e5cc5da` commit notes and comments in `index_registry.json` based on an external manual testing session conducted on 2026-11-10.
2. **Current Repository State:** No raw response payloads, log files, or cluster export dumps from that session were committed to the repository (the working tree contains only synthetic fixtures).
3. **Audit Verdict:** While these observations provided the rationale for adding `DEGRADED` to `index_registry.json` and recognizing event codes `1017`/`1018`, **they cannot be independently reproduced in the local environment** due to the absence of live cluster access and raw export dumps.
4. **Governing Policy:** These numbers are classified as **historical report observations**, not verified ground truth. The agent continues to handle both `CRITICAL` and `DEGRADED` safely via `{"named_condition": "abnormal_severity"}`.

---

## 4. Live Cluster Verification Procedures (Readiness Checklist)

When access to an actual OpenSearch cluster is established, execute the following read-only commands:

### A. Mapping & Index Export
```bash
# 1. Fetch and sanitize live mappings for all three families
python scripts/smoke_test_opensearch.py --save-mappings tests/fixtures/opensearch/live/

# 2. Check concrete index names and date rotation styles
curl -s "http://<opensearch>:9200/_cat/indices/slingshot_crayfabrichealth-*,controllerlog-*,log_iml-*?v&h=index,docs.count,store.size"
```

### B. iLO Categories Verification (Phase B)
```json
// POST /log_iml-*/_search
{
  "size": 0,
  "query": { "range": { "@timestamp": { "gte": "now-7d" } } },
  "aggs": {
    "categories": {
      "terms": { "field": "Oem.Hpe.Categories.keyword", "size": 50 }
    }
  }
}
```
*Goal:* Identify the actual string vocabulary (e.g. `"Security"`, `"Administration"`, `"Hardware"`).

### C. iLO Severity Verification (Phase C)
```json
// POST /log_iml-*/_search
{
  "size": 0,
  "query": { "range": { "@timestamp": { "gte": "now-7d" } } },
  "aggs": {
    "dmtf_severity": { "terms": { "field": "Severity.keyword", "size": 10 } },
    "oem_severity": { "terms": { "field": "Oem.Hpe.Severity.keyword", "size": 10 } }
  }
}
```
*Goal:* Confirm whether `Oem.Hpe.Severity` uses `Caution`, `Warning`, or `Critical`, and whether unexpected values exist.

### D. Switch-Availability Message Sampling (Phase D)
```json
// POST /controllerlog-*/_search
{
  "size": 20,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-7d" } } },
        { "term": { "Resource.service.name.keyword": "fabric-manager" } },
        { "match_phrase": { "Body": "switches" } }
      ]
    }
  },
  "_source": ["@timestamp", "Body", "Resource.host.name"]
}
```
*Goal:* Confirm exact message wording and regex capture groups for available vs expected switches.

### E. Fabric Heartbeat Interval Analysis (Phase E)
```json
// POST /slingshot_crayfabrichealth-*/_search
{
  "size": 50,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-1h" } } },
        { "term": { "fields.PhysicalContext.keyword": "runtime.heartbeat" } }
      ]
    }
  },
  "sort": [{ "@timestamp": "asc" }],
  "_source": ["@timestamp", "fields.Switch"]
}
```
*Goal:* Calculate $(\Delta t)$ between consecutive heartbeats for a single switch ASIC to confirm interval (60s).

### F. Failed-Login Event Detection (Phase F)
```json
// POST /log_iml-*/_search
{
  "size": 20,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-7d" } } },
        { "match": { "Message": "login" } }
      ],
      "must_not": [
        { "terms": { "Oem.Hpe.Code": [1017, 1018] } }
      ]
    }
  },
  "_source": ["@timestamp", "Message", "Oem.Hpe.Code", "Severity"]
}
```
*Goal:* Inspect non-1017/1018 login messages to identify whether failed authentication emits a specific event code or text.

---

## 5. Fixture Sanitization Guidelines

When committing live exports to `tests/fixtures/opensearch/live/`:
1. **Never commit raw log messages, passwords, tokens, or IP addresses.**
2. **Strip internal cluster metadata:** remove cluster UUIDs, index routing hashes, and shard allocation details.
3. **Preserve exact schema types:** field types, nested properties, `.keyword` multi-fields, and `ignore_above` parameters must remain intact.

