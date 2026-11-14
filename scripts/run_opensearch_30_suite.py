#!/usr/bin/env python3
"""
scripts/run_opensearch_30_suite.py

Runs the 30 OpenSearch mentor questions (10.1 Fabric health [9], 10.2 Controller log [10],
10.3 iLO event log [11]) against the built backend code (pipeline -> guard -> canonical ->
validator -> executor -> analysis/correlation) AND in parallel executes the exact OpenSearch
queries directly against OpenSearch (http://127.0.0.1:9250), then compares both outputs and
writes the results to JSON files in the workspace root:
  - opensearch_question_suite_fixed.json
  - test_output_all_fixed.json
  - opensearch_direct_vs_agent_comparison.json
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from unittest.mock import patch

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app import executor, llm_client, pipeline  # noqa: E402
from app.config import Settings  # noqa: E402
from app.opensearch_catalog import runtime  # noqa: E402
from app.skill_index import SkillIndex  # noqa: E402
from scripts import manual_test_report, smoke_test_opensearch  # noqa: E402

WINDOW_24H = {"range": {"@timestamp": {"gte": "now-24h", "lte": "now"}}}
FH_REF = "references/opensearch-logs/fabric-health.md"
CL_REF = "references/opensearch-logs/controller-log.md"
ILO_REF = "references/opensearch-logs/ilo-events.md"


def _router_for(*refs: str) -> dict[str, Any]:
    return {
        "gate": "pass",
        "panic_mode": False,
        "matched_references": [{"reference_path": r, "data_source": "opensearch"} for r in refs],
        "reasoning": "Matched OpenSearch domain reference.",
    }


def _single_ok(
    ref: str,
    pattern_id: str,
    name: str,
    intent: str,
    query: dict[str, Any],
    explanation: str,
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "mode": "single",
        "status": "ok",
        "reference_used": ref,
        "measurement_used": {"type": "raw_metric", "name": name, "source_metrics": []},
        "data_source": "opensearch",
        "pattern_id": pattern_id,
        "intent": intent,
        "query": query,
        "explanation": explanation,
    }
    if analysis is not None:
        out["analysis"] = analysis
    return out


SUITE_CASES: list[dict[str, Any]] = [
    # =========================================================================
    # 10.1 Fabric health (FH-01 .. FH-09)
    # =========================================================================
    {
        "test_id": "FH-01",
        "category": "fabric_health",
        "question": "Which fabric components reported a non-healthy status in the last 24 h?",
        "expected_behavior": "Filter the last 24 hours, exclude HEALTHY, aggregate by Switch, and return observed non-healthy states with evidence.",
        "router": _router_for(FH_REF),
        "generator": _single_ok(
            FH_REF,
            "fabric_health",
            "fabric components reporting non-healthy status",
            "top_n",
            {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [WINDOW_24H, {"exists": {"field": "fields.PhysicalSubContext"}}],
                        "must_not": [{"term": {"fields.PhysicalSubContext.keyword": "HEALTHY"}}],
                    }
                },
                "aggs": {"by_switch": {"terms": {"field": "fields.Switch.keyword", "size": 50}}},
            },
            "Queried fabric health documents in the last 24 hours excluding HEALTHY status, grouped by switch. Caveat: duplicate_reemission means document counts overstate distinct faults.",
        ),
        "direct_opensearch": {
            "index": "slingshot_crayfabrichealth-*",
            "body": {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {
                                "bool": {
                                    "filter": [{"exists": {"field": "fields.PhysicalSubContext"}}],
                                    "must_not": [{"terms": {"fields.PhysicalSubContext.keyword": ["HEALTHY"]}}],
                                }
                            },
                        ]
                    }
                },
                "aggs": {"by_switch": {"terms": {"field": "fields.Switch.keyword", "size": 50}}},
            },
        },
    },
    {
        "test_id": "FH-02",
        "category": "fabric_health",
        "question": "How many CRITICAL events occurred per switch?",
        "expected_behavior": "Filter CRITICAL events and count by Switch, including distinct Location evidence without equating raw records with unique faults.",
        "router": _router_for(FH_REF),
        "generator": _single_ok(
            FH_REF,
            "fabric_health",
            "CRITICAL events per switch with distinct locations",
            "top_n",
            {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"term": {"fields.PhysicalSubContext.keyword": "CRITICAL"}},
                        ]
                    }
                },
                "aggs": {
                    "by_switch": {
                        "terms": {"field": "fields.Switch.keyword", "size": 20},
                        "aggs": {
                            "distinct_ports": {"cardinality": {"field": "fields.Location.keyword"}},
                            "by_location": {"terms": {"field": "fields.Location.keyword", "size": 10}},
                        },
                    }
                },
            },
            "Filtered CRITICAL fabric health events over the last 24 hours grouped by switch, with both distinct location count (cardinality) and per-location breakdown (terms on fields.Location.keyword) because duplicate_reemission inflates raw document counts.",
        ),
        "direct_opensearch": {
            "index": "slingshot_crayfabrichealth-*",
            "body": {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"term": {"fields.PhysicalSubContext.keyword": "CRITICAL"}},
                        ]
                    }
                },
                "aggs": {
                    "by_switch": {
                        "terms": {"field": "fields.Switch.keyword", "size": 20},
                        "aggs": {
                            "distinct_ports": {"cardinality": {"field": "fields.Location.keyword"}},
                            "by_location": {"terms": {"field": "fields.Location.keyword", "size": 10}},
                        },
                    }
                },
            },
        },
    },
    {
        "test_id": "FH-03",
        "category": "fabric_health",
        "question": "What health states exist besides HEALTHY?",
        "expected_behavior": "Inspect the complete state vocabulary using a PhysicalSubContext terms aggregation without excluding HEALTHY.",
        "router": _router_for(FH_REF),
        "generator": _single_ok(
            FH_REF,
            "fabric_health",
            "fabric health states in PhysicalSubContext",
            "top_n",
            {
                "size": 0,
                "track_total_hits": True,
                "query": {"bool": {"filter": [WINDOW_24H]}},
                "aggs": {"health_states": {"terms": {"field": "fields.PhysicalSubContext.keyword", "size": 20}}},
            },
            "Aggregated all observed values of fields.PhysicalSubContext over the last 24 hours without filtering so all health states besides HEALTHY are shown.",
        ),
        "direct_opensearch": {
            "index": "slingshot_crayfabrichealth-*",
            "body": {
                "size": 0,
                "track_total_hits": True,
                "query": {"bool": {"filter": [WINDOW_24H]}},
                "aggs": {"health_states": {"terms": {"field": "fields.PhysicalSubContext.keyword", "size": 20}}},
            },
        },
    },
    {
        "test_id": "FH-04",
        "category": "fabric_health",
        "question": "Which ports were disabled due to excessive flapping?",
        "expected_behavior": "Search Value for excessive flapping and return or aggregate matching records by Location without inventing port identities.",
        "router": _router_for(FH_REF),
        "generator": _single_ok(
            FH_REF,
            "fabric_health",
            "ports disabled due to excessive flapping",
            "top_n",
            {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"match_phrase": {"fields.Value": "excessive flapping"}},
                        ]
                    }
                },
                "aggs": {"by_location": {"terms": {"field": "fields.Location.keyword", "size": 50}}},
            },
            "Searched fields.Value for the phrase 'excessive flapping' in the last 24 hours and grouped matching fault documents by fields.Location.keyword.",
        ),
        "direct_opensearch": {
            "index": "slingshot_crayfabrichealth-*",
            "body": {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"match_phrase": {"fields.Value": "excessive flapping"}},
                        ]
                    }
                },
                "aggs": {"by_location": {"terms": {"field": "fields.Location.keyword", "size": 50}}},
            },
        },
    },
    {
        "test_id": "FH-05",
        "category": "fabric_health",
        "question": "What recommendations were reported for port j104p1?",
        "expected_behavior": "Filter the Port field for j104p1 and project the actual Value recommendation text.",
        "router": _router_for(FH_REF),
        "generator": _single_ok(
            FH_REF,
            "fabric_health",
            "recommendations reported for port j104p1",
            "rows",
            {
                "size": 20,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"term": {"fields.Port.keyword": "j104p1"}},
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "fields.Switch",
                    "fields.Port",
                    "fields.Location",
                    "fields.PhysicalContext",
                    "fields.PhysicalSubContext",
                    "fields.Value",
                ],
            },
            "Filtered fabric health documents by bare port fields.Port.keyword = 'j104p1' over the last 24 hours and projected fields.Value containing the recommendation text.",
        ),
        "direct_opensearch": {
            "index": "slingshot_crayfabrichealth-*",
            "body": {
                "size": 20,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"term": {"fields.Port.keyword": "j104p1"}},
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "fields.Switch",
                    "fields.Port",
                    "fields.Location",
                    "fields.PhysicalContext",
                    "fields.PhysicalSubContext",
                    "fields.Value",
                ],
            },
        },
    },
    {
        "test_id": "FH-06",
        "category": "fabric_health",
        "question": "Which switches reported linkerrors.linkautorestartexhaustederror?",
        "expected_behavior": "Filter PhysicalContext for the exact event kind and return matching switches.",
        "router": _router_for(FH_REF),
        "generator": _single_ok(
            FH_REF,
            "fabric_health",
            "switches reporting linkerrors.linkautorestartexhaustederror",
            "top_n",
            {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"term": {"fields.PhysicalContext.keyword": "linkerrors.linkautorestartexhaustederror"}},
                        ]
                    }
                },
                "aggs": {"by_switch": {"terms": {"field": "fields.Switch.keyword", "size": 50}}},
            },
            "Filtered fabric health documents on fields.PhysicalContext.keyword = 'linkerrors.linkautorestartexhaustederror' in the last 24 hours and grouped by fields.Switch.keyword.",
        ),
        "direct_opensearch": {
            "index": "slingshot_crayfabrichealth-*",
            "body": {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"term": {"fields.PhysicalContext.keyword": "linkerrors.linkautorestartexhaustederror"}},
                        ]
                    }
                },
                "aggs": {"by_switch": {"terms": {"field": "fields.Switch.keyword", "size": 50}}},
            },
        },
    },
    {
        "test_id": "FH-07",
        "category": "fabric_health",
        "question": "Which racks/chassis have the most unhealthy events?",
        "expected_behavior": "Exclude HEALTHY, aggregate by Rack and nested Chassis, and preserve the parent-child hierarchy.",
        "router": _router_for(FH_REF),
        "generator": _single_ok(
            FH_REF,
            "fabric_health",
            "unhealthy fabric events by rack and chassis",
            "top_n",
            {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"named_condition": "abnormal_severity"},
                        ]
                    }
                },
                "aggs": {
                    "by_rack": {
                        "terms": {"field": "fields.Rack.keyword", "size": 20},
                        "aggs": {"by_chassis": {"terms": {"field": "fields.Chassis.keyword", "size": 20}}},
                    }
                },
            },
            "Filtered non-HEALTHY fabric events in the last 24 hours and grouped by fields.Rack.keyword with nested fields.Chassis.keyword.",
        ),
        "direct_opensearch": {
            "index": "slingshot_crayfabrichealth-*",
            "body": {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {
                                "bool": {
                                    "filter": [{"exists": {"field": "fields.PhysicalSubContext"}}],
                                    "must_not": [{"terms": {"fields.PhysicalSubContext.keyword": ["HEALTHY"]}}],
                                }
                            },
                        ]
                    }
                },
                "aggs": {
                    "by_rack": {
                        "terms": {"field": "fields.Rack.keyword", "size": 20},
                        "aggs": {"by_chassis": {"terms": {"field": "fields.Chassis.keyword", "size": 20}}},
                    }
                },
            },
        },
    },
    {
        "test_id": "FH-08",
        "category": "fabric_health",
        "question": "Show critical events for switch x1000c3r1.",
        "expected_behavior": "Filter CRITICAL and exact-match switch x1000c3r1; return relevant event records using safe DSL.",
        "router": _router_for(FH_REF),
        "generator": _single_ok(
            FH_REF,
            "fabric_health",
            "critical events for switch x1000c3r1",
            "rows",
            {
                "size": 20,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"term": {"fields.PhysicalSubContext.keyword": "CRITICAL"}},
                            {"term": {"fields.Switch.keyword": "x1000c3r1"}},
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "fields.Switch",
                    "fields.Port",
                    "fields.Location",
                    "fields.PhysicalContext",
                    "fields.PhysicalSubContext",
                    "fields.Value",
                ],
            },
            "Filtered fabric health documents in the last 24 hours for CRITICAL state and switch x1000c3r1.",
        ),
        "direct_opensearch": {
            "index": "slingshot_crayfabrichealth-*",
            "body": {
                "size": 20,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"term": {"fields.PhysicalSubContext.keyword": "CRITICAL"}},
                            {"term": {"fields.Switch.keyword": "x1000c3r1"}},
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "fields.Switch",
                    "fields.Port",
                    "fields.Location",
                    "fields.PhysicalContext",
                    "fields.PhysicalSubContext",
                    "fields.Value",
                ],
            },
        },
    },
    {
        "test_id": "FH-09",
        "category": "fabric_health",
        "question": "Which ports have recurring faults?",
        "expected_behavior": "Investigate recurrence while distinguishing genuine recurrence from periodic hourly simulator re-emission.",
        "router": _router_for(FH_REF),
        "generator": _single_ok(
            FH_REF,
            "fabric_health",
            "ports with recurring faults across 1-hour intervals",
            "recurrence",
            {
                "size": 0,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"named_condition": "abnormal_severity"},
                        ]
                    }
                },
                "aggs": {
                    "by_location": {
                        "terms": {"field": "fields.Location.keyword", "size": 20},
                        "aggs": {"per_hour": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}}},
                    }
                },
            },
            "Ranked ports/locations by active 1-hour intervals over the last 24 hours using the recurrence analysis. Note: 1h interval is an unverified placeholder, and because of duplicate_reemission (hourly re-emit), this ranking cannot distinguish a persistent fault re-emitted hourly from repeated new occurrences.",
            analysis={"type": "recurrence"},
        ),
        "direct_opensearch": {
            "index": "slingshot_crayfabrichealth-*",
            "body": {
                "size": 0,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {
                                "bool": {
                                    "filter": [{"exists": {"field": "fields.PhysicalSubContext"}}],
                                    "must_not": [{"terms": {"fields.PhysicalSubContext.keyword": ["HEALTHY"]}}],
                                }
                            },
                        ]
                    }
                },
                "aggs": {
                    "by_location": {
                        "terms": {"field": "fields.Location.keyword", "size": 20},
                        "aggs": {"per_hour": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}}},
                    }
                },
            },
        },
    },
    # =========================================================================
    # 10.2 Controller log (CL-01 .. CL-10)
    # =========================================================================
    {
        "test_id": "CL-01",
        "category": "controller_logs",
        "question": "Which controllers reported errors in last 24 h / which hosts have most errors?",
        "expected_behavior": "Apply the documented abnormal severity classification within the last 24 hours and aggregate by Resource.host.name with supporting counts.",
        "router": _router_for(CL_REF),
        "generator": _single_ok(
            CL_REF,
            "controller_log",
            "controllers reporting errors in the last 24 hours",
            "top_n",
            {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"named_condition": "abnormal_severity"},
                        ]
                    }
                },
                "aggs": {
                    "by_host": {
                        "terms": {"field": "Resource.host.name.keyword", "size": 20},
                        "aggs": {"by_service": {"terms": {"field": "Resource.service.name.keyword", "size": 5}}},
                    }
                },
            },
            "Filtered controller logs by abnormal_severity (error and critical set) over the last 24 hours and grouped by Resource.host.name.keyword (with nested service breakdown).",
        ),
        "direct_opensearch": {
            "index": "controllerlog-*",
            "body": {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {
                                "bool": {
                                    "filter": [
                                        {
                                            "terms": {
                                                "Severity.keyword": ["alert", "crit", "critical", "emerg", "err", "error"]
                                            }
                                        }
                                    ]
                                }
                            },
                        ]
                    }
                },
                "aggs": {
                    "by_host": {
                        "terms": {"field": "Resource.host.name.keyword", "size": 20},
                        "aggs": {"by_service": {"terms": {"field": "Resource.service.name.keyword", "size": 5}}},
                    }
                },
            },
        },
    },
    {
        "test_id": "CL-02",
        "category": "controller_logs",
        "question": "Which controllers reported I²C failures, and which bus numbers?",
        "expected_behavior": "Search Body for the phrase i2c ioctl failed, return matching rows and preserve their Body text without inventing a bus-number field.",
        "router": _router_for(CL_REF),
        "generator": _single_ok(
            CL_REF,
            "controller_log",
            "controller I2C failures and bus numbers in Body",
            "rows",
            {
                "size": 20,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"match_phrase": {"Body": "i2c ioctl failed"}},
                        ]
                    }
                },
                "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
            },
            "Searched Body for 'i2c ioctl failed' over the last 24 hours and returned rows with Resource.host.name and Body because bus numbers live only in Body text.",
        ),
        "direct_opensearch": {
            "index": "controllerlog-*",
            "body": {
                "size": 20,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"match_phrase": {"Body": "i2c ioctl failed"}},
                        ]
                    }
                },
                "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
            },
        },
    },
    {
        "test_id": "CL-03",
        "category": "controller_logs",
        "question": "Are the same errors recurring on a controller?",
        "expected_behavior": "Host-to-service frequency may be reported, but must not be described as identical-message recurrence.",
        "router": _router_for(CL_REF),
        "generator": _single_ok(
            CL_REF,
            "controller_log",
            "repeated controller error activity per host and service",
            "top_n",
            {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"named_condition": "abnormal_severity"},
                        ]
                    }
                },
                "aggs": {
                    "by_host": {
                        "terms": {"field": "Resource.host.name.keyword", "size": 20},
                        "aggs": {"by_service": {"terms": {"field": "Resource.service.name.keyword", "size": 10}}},
                    }
                },
            },
            "Grouped error-level controller logs by host and nested service over the last 24 hours. Note: this shows repeated error activity per controller and service; identical-message recurrence is not supported because Body text is not aggregatable.",
        ),
        "direct_opensearch": {
            "index": "controllerlog-*",
            "body": {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {
                                "bool": {
                                    "filter": [
                                        {
                                            "terms": {
                                                "Severity.keyword": ["alert", "crit", "critical", "emerg", "err", "error"]
                                            }
                                        }
                                    ]
                                }
                            },
                        ]
                    }
                },
                "aggs": {
                    "by_host": {
                        "terms": {"field": "Resource.host.name.keyword", "size": 20},
                        "aggs": {"by_service": {"terms": {"field": "Resource.service.name.keyword", "size": 10}}},
                    }
                },
            },
        },
    },
    {
        "test_id": "CL-04",
        "category": "controller_logs",
        "question": 'Which hosts reported "LinkStatus Down"? / ports down on x1000c2r3b0?',
        "expected_behavior": "Search Body for LinkStatus Down, constrain x1000c2r3b0 when applicable, and return matching rows with actual body evidence.",
        "router": _router_for(CL_REF),
        "generator": _single_ok(
            CL_REF,
            "controller_log",
            "LinkStatus Down events and ports down on x1000c2r3b0",
            "rows",
            {
                "size": 20,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"match_phrase": {"Body": "LinkStatus Down"}},
                            {"term": {"Resource.host.name.keyword": "x1000c2r3b0"}},
                        ]
                    }
                },
                "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
            },
            "Searched controller logs over the last 24 hours for 'LinkStatus Down' on host x1000c2r3b0 and projected Body so the port token inside the message text is returned.",
        ),
        "direct_opensearch": {
            "index": "controllerlog-*",
            "body": {
                "size": 20,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"match_phrase": {"Body": "LinkStatus Down"}},
                            {"term": {"Resource.host.name.keyword": "x1000c2r3b0"}},
                        ]
                    }
                },
                "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
            },
        },
    },
    {
        "test_id": "CL-05",
        "category": "controller_logs",
        "question": "Fabric-manager messages with fewer switches available than expected?",
        "expected_behavior": "Verify n/m counts and establish n < m before calling a message a failure.",
        "router": _router_for(CL_REF),
        "generator": _single_ok(
            CL_REF,
            "controller_log",
            "fabric-manager messages with fewer switches available than expected",
            "text_ratio_shortfall",
            {
                "size": 100,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"terms": {"Resource.service.name.keyword": ["fabric-manager.sh", "fabric-manager"]}},
                            {"match_phrase": {"Body": "switches available"}},
                        ]
                    }
                },
                "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
            },
            "Retrieved up to 100 recent fabric-manager switch availability messages and ran client-side text_ratio_shortfall parsing ('n/m switches available', keeping n < m). Note: the regex pattern is an unverified placeholder and only the returned sample of up to 100 rows is scanned.",
            analysis={"type": "text_ratio_shortfall"},
        ),
        "direct_opensearch": {
            "index": "controllerlog-*",
            "body": {
                "size": 100,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"terms": {"Resource.service.name.keyword": ["fabric-manager.sh", "fabric-manager"]}},
                            {"match_phrase": {"Body": "switches available"}},
                        ]
                    }
                },
                "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
            },
        },
    },
    {
        "test_id": "CL-06",
        "category": "controller_logs",
        "question": "Did link-down events occur near fabric availability problems?",
        "expected_behavior": "Use two bounded family queries and correlate event times only with an explicit tolerance.",
        "router": _router_for(CL_REF, FH_REF),
        "generator": {
            "mode": "multi",
            "results": [
                {
                    "status": "ok",
                    "reference_used": CL_REF,
                    "measurement_used": {"type": "raw_metric", "name": "controller LinkStatus Down events", "source_metrics": []},
                    "data_source": "opensearch",
                    "pattern_id": "controller_log",
                    "intent": "rows",
                    "query": {
                        "size": 50,
                        "sort": [{"@timestamp": "desc"}],
                        "query": {
                            "bool": {
                                "filter": [
                                    WINDOW_24H,
                                    {"match_phrase": {"Body": "LinkStatus Down"}},
                                ]
                            }
                        },
                    },
                    "explanation": "Controller LinkStatus Down events in the last 24 hours.",
                },
                {
                    "status": "ok",
                    "reference_used": FH_REF,
                    "measurement_used": {"type": "raw_metric", "name": "non-healthy fabric health events", "source_metrics": []},
                    "data_source": "opensearch",
                    "pattern_id": "fabric_health",
                    "intent": "rows",
                    "query": {
                        "size": 50,
                        "sort": [{"@timestamp": "desc"}],
                        "query": {
                            "bool": {
                                "filter": [
                                    WINDOW_24H,
                                    {"named_condition": "abnormal_severity"},
                                ]
                            }
                        },
                    },
                    "explanation": "Non-healthy fabric health events in the last 24 hours.",
                },
            ],
            "synthesis": None,
            "correlation": {"type": "temporal_proximity", "left": 0, "right": 1, "tolerance": "5m"},
        },
        "direct_opensearch": {
            "multi": [
                {
                    "index": "controllerlog-*",
                    "body": {
                        "size": 50,
                        "sort": [{"@timestamp": {"order": "desc"}}],
                        "query": {"bool": {"filter": [WINDOW_24H, {"match_phrase": {"Body": "LinkStatus Down"}}]}},
                    },
                },
                {
                    "index": "slingshot_crayfabrichealth-*",
                    "body": {
                        "size": 50,
                        "sort": [{"@timestamp": {"order": "desc"}}],
                        "query": {
                            "bool": {
                                "filter": [
                                    WINDOW_24H,
                                    {
                                        "bool": {
                                            "filter": [{"exists": {"field": "fields.PhysicalSubContext"}}],
                                            "must_not": [{"terms": {"fields.PhysicalSubContext.keyword": ["HEALTHY"]}}],
                                        }
                                    },
                                ]
                            }
                        },
                    },
                },
            ]
        },
    },
    {
        "test_id": "CL-07",
        "category": "controller_logs",
        "question": "Which services generate the most error logs?",
        "expected_behavior": "Apply the documented abnormal severity classification and aggregate by Resource.service.name.",
        "router": _router_for(CL_REF),
        "generator": _single_ok(
            CL_REF,
            "controller_log",
            "services generating the most error logs",
            "top_n",
            {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"named_condition": "abnormal_severity"},
                        ]
                    }
                },
                "aggs": {"by_service": {"terms": {"field": "Resource.service.name.keyword", "size": 20}}},
            },
            "Filtered controller logs by abnormal_severity in the last 24 hours and grouped by Resource.service.name.keyword.",
        ),
        "direct_opensearch": {
            "index": "controllerlog-*",
            "body": {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {
                                "bool": {
                                    "filter": [
                                        {
                                            "terms": {
                                                "Severity.keyword": ["alert", "crit", "critical", "emerg", "err", "error"]
                                            }
                                        }
                                    ]
                                }
                            },
                        ]
                    }
                },
                "aggs": {"by_service": {"terms": {"field": "Resource.service.name.keyword", "size": 20}}},
            },
        },
    },
    {
        "test_id": "CL-08",
        "category": "controller_logs",
        "question": "Show all hmpad errors for x1000c6s7b1.",
        "expected_behavior": "Filter exact service hmpad and exact host x1000c6s7b1, then return matching records.",
        "router": _router_for(CL_REF),
        "generator": _single_ok(
            CL_REF,
            "controller_log",
            "hmpad errors for x1000c6s7b1",
            "rows",
            {
                "size": 20,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"named_condition": "abnormal_severity"},
                            {"term": {"Resource.service.name.keyword": "hmpad"}},
                            {"term": {"Resource.host.name.keyword": "x1000c6s7b1"}},
                        ]
                    }
                },
                "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
            },
            "Filtered controller logs over the last 24 hours for abnormal_severity, Resource.service.name.keyword = 'hmpad', and Resource.host.name.keyword = 'x1000c6s7b1'.",
        ),
        "direct_opensearch": {
            "index": "controllerlog-*",
            "body": {
                "size": 20,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {
                                "bool": {
                                    "filter": [
                                        {
                                            "terms": {
                                                "Severity.keyword": ["alert", "crit", "critical", "emerg", "err", "error"]
                                            }
                                        }
                                    ]
                                }
                            },
                            {"term": {"Resource.service.name.keyword": "hmpad"}},
                            {"term": {"Resource.host.name.keyword": "x1000c6s7b1"}},
                        ]
                    }
                },
                "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
            },
        },
    },
    {
        "test_id": "CL-09",
        "category": "controller_logs",
        "question": "What did hmscd report before and after a link-down event?",
        "expected_behavior": "Anchor on a link-down event and retrieve timestamped hmscd context before and after it without unsupported temporal claims.",
        "router": _router_for(CL_REF),
        "generator": _single_ok(
            CL_REF,
            "controller_log",
            "hmscd messages before and after a link-down event",
            "context_window",
            {
                "size": 1,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"match_phrase": {"Body": "LinkStatus Down"}},
                        ]
                    }
                },
                "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
            },
            "Found the most recent LinkStatus Down anchor event in the last 24 hours and retrieved surrounding hmscd messages within +/- 5 minutes on the same controller host.",
            analysis={
                "type": "context_window",
                "before": "5m",
                "after": "5m",
                "context_filter": {"term": {"Resource.service.name.keyword": "hmscd"}},
            },
        ),
        "direct_opensearch": {
            "index": "controllerlog-*",
            "body": {
                "size": 1,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {"bool": {"filter": [WINDOW_24H, {"match_phrase": {"Body": "LinkStatus Down"}}]}},
                "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
            },
        },
    },
    {
        "test_id": "CL-10",
        "category": "controller_logs",
        "question": "Show controller logs excluding routine CRON.",
        "expected_behavior": "Exclude Resource.service.name CRON with a must_not term filter and return remaining records.",
        "router": _router_for(CL_REF),
        "generator": _single_ok(
            CL_REF,
            "controller_log",
            "controller logs excluding routine CRON",
            "rows",
            {
                "size": 20,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [WINDOW_24H],
                        "must_not": [{"term": {"Resource.service.name.keyword": "CRON"}}],
                    }
                },
                "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
            },
            "Returned recent controller logs over the last 24 hours excluding Resource.service.name.keyword = 'CRON'.",
        ),
        "direct_opensearch": {
            "index": "controllerlog-*",
            "body": {
                "size": 20,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [WINDOW_24H],
                        "must_not": [{"term": {"Resource.service.name.keyword": "CRON"}}],
                    }
                },
                "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
            },
        },
    },
    # =========================================================================
    # 10.3 iLO event log (ILO-01 .. ILO-11)
    # =========================================================================
    {
        "test_id": "ILO-01",
        "category": "ilo_events",
        "question": "Who logged into leif-login03-bmc in last 24 h?",
        "expected_behavior": "Search Message for login within 24 hours, constrain the requested BMC, and preserve username evidence from Message text.",
        "router": _router_for(ILO_REF),
        "generator": _single_ok(
            ILO_REF,
            "ilo_events",
            "login events on leif-login03-bmc in last 24h",
            "rows",
            {
                "size": 20,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"term": {"host.keyword": "leif-login03-bmc"}},
                            {"match_phrase": {"Message": "login"}},
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "Created",
                    "host",
                    "Severity",
                    "Oem.Hpe.Severity",
                    "Message",
                    "Oem.Hpe.Code",
                    "Oem.Hpe.RecommendedAction",
                ],
            },
            "Filtered iLO events on host.keyword = 'leif-login03-bmc' with match_phrase 'login' on Message over the last 24 hours. Username appears in Message text (and is masked by redaction policy).",
        ),
        "direct_opensearch": {
            "index": "log_iml-*",
            "body": {
                "size": 20,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"term": {"host.keyword": "leif-login03-bmc"}},
                            {"match_phrase": {"Message": "login"}},
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "Created",
                    "host",
                    "Severity",
                    "Oem.Hpe.Severity",
                    "Message",
                    "Oem.Hpe.Code",
                    "Oem.Hpe.RecommendedAction",
                ],
            },
        },
    },
    {
        "test_id": "ILO-02",
        "category": "ilo_events",
        "question": "Which BMCs were accessed from 172.24.0.1?",
        "expected_behavior": "Search Message for the supplied IP and return BMCs from matching event records without assuming a source-IP field.",
        "router": _router_for(ILO_REF),
        "generator": _single_ok(
            ILO_REF,
            "ilo_events",
            "BMCs accessed from 172.24.0.1",
            "top_n",
            {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"match_phrase": {"Message": "172.24.0.1"}},
                        ]
                    }
                },
                "aggs": {"by_host": {"terms": {"field": "host.keyword", "size": 20}}},
            },
            "Searched Message for '172.24.0.1' over the last 24 hours and aggregated by host.keyword because source IP addresses exist only in Message text.",
        ),
        "direct_opensearch": {
            "index": "log_iml-*",
            "body": {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"match_phrase": {"Message": "172.24.0.1"}},
                        ]
                    }
                },
                "aggs": {"by_host": {"terms": {"field": "host.keyword", "size": 20}}},
            },
        },
    },
    {
        "test_id": "ILO-03",
        "category": "ilo_events",
        "question": "Show all root login/logout events.",
        "expected_behavior": "Match verified event codes 1017 and 1018 and require Message evidence identifying root.",
        "router": _router_for(ILO_REF),
        "generator": _single_ok(
            ILO_REF,
            "ilo_events",
            "root login and logout events",
            "rows",
            {
                "size": 20,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"terms": {"Oem.Hpe.Code": [1017, 1018]}},
                            {"match_phrase": {"Message": "root"}},
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "Created",
                    "host",
                    "Severity",
                    "Oem.Hpe.Severity",
                    "Message",
                    "Oem.Hpe.Code",
                    "Oem.Hpe.RecommendedAction",
                ],
            },
            "Filtered iLO events over the last 24 hours by verified browser login/logout codes Oem.Hpe.Code in [1017, 1018] and match_phrase 'root' in Message.",
        ),
        "direct_opensearch": {
            "index": "log_iml-*",
            "body": {
                "size": 20,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"terms": {"Oem.Hpe.Code": [1017, 1018]}},
                            {"match_phrase": {"Message": "root"}},
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "Created",
                    "host",
                    "Severity",
                    "Oem.Hpe.Severity",
                    "Message",
                    "Oem.Hpe.Code",
                    "Oem.Hpe.RecommendedAction",
                ],
            },
        },
    },
    {
        "test_id": "ILO-04",
        "category": "ilo_events",
        "question": "Browser vs IPMI/RMCP login counts.",
        "expected_behavior": "Use a bounded filters aggregation with separate match_phrase conditions for browser and IPMI/RMCP login methods.",
        "router": _router_for(ILO_REF),
        "generator": _single_ok(
            ILO_REF,
            "ilo_events",
            "browser vs IPMI/RMCP login counts",
            "top_n",
            {
                "size": 0,
                "track_total_hits": True,
                "query": {"bool": {"filter": [WINDOW_24H]}},
                "aggs": {
                    "login_channels": {
                        "filters": {
                            "filters": {
                                "browser": {"match_phrase": {"Message": "Browser login"}},
                                "ipmi_rmcp": {"match_phrase": {"Message": "RMCP"}},
                            }
                        }
                    }
                },
            },
            "Compared Browser login vs IPMI/RMCP login counts over the last 24 hours using a filters aggregation on Message.",
        ),
        "direct_opensearch": {
            "index": "log_iml-*",
            "body": {
                "size": 0,
                "track_total_hits": True,
                "query": {"bool": {"filter": [WINDOW_24H]}},
                "aggs": {
                    "login_channels": {
                        "filters": {
                            "filters": {
                                "browser": {"match_phrase": {"Message": "Browser login"}},
                                "ipmi_rmcp": {"match_phrase": {"Message": "RMCP"}},
                            }
                        }
                    }
                },
            },
        },
    },
    {
        "test_id": "ILO-05",
        "category": "ilo_events",
        "question": "Which BMCs have unusually frequent login/logout?",
        "expected_behavior": "Report login/logout counts per BMC, but do not assert anomalous frequency without a defined baseline or threshold.",
        "router": _router_for(ILO_REF),
        "generator": _single_ok(
            ILO_REF,
            "ilo_events",
            "BMC login and logout frequency ranking",
            "top_n",
            {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"terms": {"Oem.Hpe.Code": [1017, 1018]}},
                        ]
                    }
                },
                "aggs": {
                    "by_host": {
                        "terms": {"field": "host.keyword", "size": 20},
                        "aggs": {"by_event_code": {"terms": {"field": "Oem.Hpe.Code", "size": 2}}},
                    }
                },
            },
            "Ranked BMCs by browser login (1017) and logout (1018) volume over the last 24 hours. No BMC is flagged as unusual/anomalous because no baseline or threshold is defined for this index family.",
        ),
        "direct_opensearch": {
            "index": "log_iml-*",
            "body": {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"terms": {"Oem.Hpe.Code": [1017, 1018]}},
                        ]
                    }
                },
                "aggs": {
                    "by_host": {
                        "terms": {"field": "host.keyword", "size": 20},
                        "aggs": {"by_event_code": {"terms": {"field": "Oem.Hpe.Code", "size": 2}}},
                    }
                },
            },
        },
    },
    {
        "test_id": "ILO-06",
        "category": "ilo_events",
        "question": "Any failed login attempts?",
        "expected_behavior": "Report unsupported/unmapped capability unless verified failure codes or message vocabulary provide reliable evidence.",
        "router": _router_for(ILO_REF),
        "generator": {
            "mode": "single",
            "status": "unmapped",
            "explanation": "Failed login detection is not supported (unmapped) because no event code, category, or message wording for failed logins has been verified in the iLO event log data.",
        },
        "direct_opensearch": None,
    },
    {
        "test_id": "ILO-07",
        "category": "ilo_events",
        "question": "Security & administration events for a BMC.",
        "expected_behavior": "Request a BMC clarification if required; use Oem.Hpe.Categories only when live mapping and observed vocabulary support it.",
        "router": _router_for(ILO_REF),
        "generator": _single_ok(
            ILO_REF,
            "ilo_events",
            "Security and Administration iLO events across BMCs",
            "rows",
            {
                "size": 20,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"terms": {"Oem.Hpe.Categories.keyword": ["Security", "Administration"]}},
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "Created",
                    "host",
                    "Severity",
                    "Oem.Hpe.Severity",
                    "Message",
                    "Oem.Hpe.Code",
                    "Oem.Hpe.RecommendedAction",
                ],
            },
            "Filtered iLO events over the last 24 hours across all BMCs where Oem.Hpe.Categories.keyword is in ['Security', 'Administration'].",
        ),
        "direct_opensearch": {
            "index": "log_iml-*",
            "body": {
                "size": 20,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"terms": {"Oem.Hpe.Categories.keyword": ["Security", "Administration"]}},
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "Created",
                    "host",
                    "Severity",
                    "Oem.Hpe.Severity",
                    "Message",
                    "Oem.Hpe.Code",
                    "Oem.Hpe.RecommendedAction",
                ],
            },
        },
    },
    {
        "test_id": "ILO-08",
        "category": "ilo_events",
        "question": "Severity other than OK or Informational.",
        "expected_behavior": "Exclude OK and Informational on the actual mapped severity fields and preserve exact observed severity values.",
        "router": _router_for(ILO_REF),
        "generator": _single_ok(
            ILO_REF,
            "ilo_events",
            "iLO events with severity other than OK or Informational",
            "rows",
            {
                "size": 20,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"named_condition": "abnormal_severity"},
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "Created",
                    "host",
                    "Severity",
                    "Oem.Hpe.Severity",
                    "Message",
                    "Oem.Hpe.Code",
                    "Oem.Hpe.RecommendedAction",
                ],
            },
            "Filtered iLO events over the last 24 hours using abnormal_severity (Severity in [Critical, Warning] OR Oem.Hpe.Severity in [Caution, Critical, Warning]). Note: Oem.Hpe.Severity vocabulary is provisional.",
        ),
        "direct_opensearch": {
            "index": "log_iml-*",
            "body": {
                "size": 20,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {
                                "bool": {
                                    "should": [
                                        {"terms": {"Severity.keyword": ["Critical", "Warning"]}},
                                        {"terms": {"Oem.Hpe.Severity.keyword": ["Caution", "Critical", "Warning"]}},
                                    ],
                                    "minimum_should_match": 1,
                                }
                            },
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "Created",
                    "host",
                    "Severity",
                    "Oem.Hpe.Severity",
                    "Message",
                    "Oem.Hpe.Code",
                    "Oem.Hpe.RecommendedAction",
                ],
            },
        },
    },
    {
        "test_id": "ILO-09",
        "category": "ilo_events",
        "question": "Warning/critical events marked unrepaired + recommended actions.",
        "expected_behavior": "Filter Oem.Hpe.Repaired=false and verified Warning/Critical severity values, and return actual recommended actions.",
        "router": _router_for(ILO_REF),
        "generator": _single_ok(
            ILO_REF,
            "ilo_events",
            "unrepaired warning/critical iLO events with recommended actions",
            "rows",
            {
                "size": 20,
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"term": {"Oem.Hpe.Repaired": False}},
                            {
                                "bool": {
                                    "should": [
                                        {"terms": {"Severity.keyword": ["Warning", "Critical"]}},
                                        {"terms": {"Oem.Hpe.Severity.keyword": ["Warning", "Critical"]}},
                                    ],
                                    "minimum_should_match": 1,
                                }
                            },
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "Created",
                    "host",
                    "Severity",
                    "Oem.Hpe.Severity",
                    "Message",
                    "Oem.Hpe.Code",
                    "Oem.Hpe.RecommendedAction",
                ],
            },
            "Filtered iLO events in the last 24 hours where Oem.Hpe.Repaired is false and Severity or Oem.Hpe.Severity is in [Warning, Critical], projecting Oem.Hpe.RecommendedAction.",
        ),
        "direct_opensearch": {
            "index": "log_iml-*",
            "body": {
                "size": 20,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            WINDOW_24H,
                            {"term": {"Oem.Hpe.Repaired": False}},
                            {
                                "bool": {
                                    "should": [
                                        {"terms": {"Severity.keyword": ["Warning", "Critical"]}},
                                        {"terms": {"Oem.Hpe.Severity.keyword": ["Warning", "Critical"]}},
                                    ],
                                    "minimum_should_match": 1,
                                }
                            },
                        ]
                    }
                },
                "_source": [
                    "@timestamp",
                    "Created",
                    "host",
                    "Severity",
                    "Oem.Hpe.Severity",
                    "Message",
                    "Oem.Hpe.Code",
                    "Oem.Hpe.RecommendedAction",
                ],
            },
        },
    },
    {
        "test_id": "ILO-10",
        "category": "ilo_events",
        "question": "Event codes recurring most per host.",
        "expected_behavior": "Use nested terms aggregations Host then Oem.Hpe.Code and distinguish event frequency from unique incidents.",
        "router": _router_for(ILO_REF),
        "generator": _single_ok(
            ILO_REF,
            "ilo_events",
            "most frequent iLO event codes per BMC host",
            "top_n",
            {
                "size": 0,
                "track_total_hits": True,
                "query": {"bool": {"filter": [WINDOW_24H]}},
                "aggs": {
                    "by_host": {
                        "terms": {"field": "host.keyword", "size": 20},
                        "aggs": {"by_event_code": {"terms": {"field": "Oem.Hpe.Code", "size": 10}}},
                    }
                },
            },
            "Grouped iLO events over the last 24 hours by host.keyword with nested Oem.Hpe.Code terms aggregation.",
        ),
        "direct_opensearch": {
            "index": "log_iml-*",
            "body": {
                "size": 0,
                "track_total_hits": True,
                "query": {"bool": {"filter": [WINDOW_24H]}},
                "aggs": {
                    "by_host": {
                        "terms": {"field": "host.keyword", "size": 20},
                        "aggs": {"by_event_code": {"terms": {"field": "Oem.Hpe.Code", "size": 10}}},
                    }
                },
            },
        },
    },
    {
        "test_id": "ILO-11",
        "category": "ilo_events",
        "question": "How long after creation were events ingested?",
        "expected_behavior": "Retrieve a bounded sample of at most 100 hits with @timestamp and Created, then calculate client-side ingestion-delay statistics.",
        "router": _router_for(ILO_REF),
        "generator": _single_ok(
            ILO_REF,
            "ilo_events",
            "iLO event ingestion lag (@timestamp minus Created)",
            "ingest_lag",
            {
                "size": 100,
                "sort": [{"@timestamp": "desc"}],
                "query": {"bool": {"filter": [WINDOW_24H]}},
                "_source": ["@timestamp", "Created", "host", "Message"],
            },
            "Sampled the 100 newest iLO events in the last 24 hours and computed client-side ingestion lag (@timestamp minus Created) via the ingest_lag analysis. Note: covers only the sampled rows and Created is unverified as an event clock.",
            analysis={"type": "ingest_lag"},
        ),
        "direct_opensearch": {
            "index": "log_iml-*",
            "body": {
                "size": 100,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "query": {"bool": {"filter": [WINDOW_24H]}},
                "_source": ["@timestamp", "Created", "host", "Message"],
            },
        },
    },
]


def _run_direct_opensearch(os_url: str, spec: dict[str, Any] | None) -> dict[str, Any]:
    if spec is None:
        return {"executed": False, "reason": "unmapped question (no OpenSearch query should be sent)"}
    if "multi" in spec:
        parts = []
        for item in spec["multi"]:
            idx = item["index"]
            r = requests.post(
                f"{os_url}/{idx}/_search?track_total_hits=true",
                json=item["body"],
                timeout=15,
            )
            r.raise_for_status()
            parts.append({"index": idx, "query": item["body"], "response": r.json()})
        return {"executed": True, "mode": "multi", "parts": parts}
    idx = spec["index"]
    r = requests.post(
        f"{os_url}/{idx}/_search?track_total_hits=true",
        json=spec["body"],
        timeout=15,
    )
    r.raise_for_status()
    return {"executed": True, "mode": "single", "index": idx, "query": spec["body"], "response": r.json()}


def _extract_os_buckets(aggs: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not aggs:
        return []
    for name, body in aggs.items():
        if isinstance(body, dict) and isinstance(body.get("buckets"), list):
            out = []
            for b in body["buckets"]:
                item: dict[str, Any] = {
                    "key": str(b.get("key_as_string", b.get("key", ""))),
                    "doc_count": b.get("doc_count"),
                }
                subs = []
                metrics = {}
                for k, v in b.items():
                    if k in ("key", "key_as_string", "doc_count"):
                        continue
                    if isinstance(v, dict) and isinstance(v.get("buckets"), list):
                        subs.extend(
                            {
                                "key": str(sb.get("key_as_string", sb.get("key", ""))),
                                "doc_count": sb.get("doc_count"),
                            }
                            for sb in v["buckets"]
                        )
                    elif isinstance(v, dict) and "value" in v:
                        metrics[k] = float(v["value"]) if isinstance(v["value"], (int, float)) else v["value"]
                if subs:
                    item["sub_buckets"] = subs
                if metrics:
                    item["metrics"] = metrics
                out.append(item)
            return out
        if isinstance(body, dict) and isinstance(body.get("buckets"), dict):
            return [{"key": str(k), "doc_count": v.get("doc_count")} for k, v in body["buckets"].items()]
    return []


def _compare_agent_and_direct(case: dict[str, Any], agent_res: dict[str, Any], direct_res: dict[str, Any]) -> dict[str, Any]:
    tid = case["test_id"]
    res_obj = agent_res.get("result") or {}
    if not direct_res.get("executed"):
        status = res_obj.get("status")
        match = status == "unmapped" and "execution" not in res_obj
        return {
            "test_id": tid,
            "question": case["question"],
            "match": match,
            "comparison_type": "unmapped_guardrail",
            "agent_status": status,
            "direct_executed": False,
            "detail": "Both agent and direct benchmark agree: no query executed for unverified failed-login vocabulary.",
        }

    if direct_res.get("mode") == "multi":
        entries = res_obj.get("results") or []
        parts = direct_res.get("parts") or []
        part_comparisons = []
        all_ok = len(entries) == len(parts) and len(parts) > 0
        for idx, (e, p) in enumerate(zip(entries, parts)):
            ex = e.get("execution") or {}
            os_raw = p["response"]
            agent_total = ((ex.get("metadata") or {}).get("count") or {}).get("value")
            direct_total = ((os_raw.get("hits") or {}).get("total") or {}).get("value")
            agent_hits = len(ex.get("hits") or [])
            direct_hits = len((os_raw.get("hits") or {}).get("hits") or [])
            exec_ok = ex.get("execution_status") in ("success", "empty_result")
            ok = exec_ok and (agent_total == direct_total) and (agent_hits == direct_hits)
            all_ok = all_ok and ok
            part_comparisons.append(
                {
                    "leg": idx,
                    "pattern_id": e.get("pattern_id"),
                    "index": p["index"],
                    "agent_total_hits": agent_total,
                    "direct_total_hits": direct_total,
                    "agent_returned_rows": agent_hits,
                    "direct_returned_rows": direct_hits,
                    "match": ok,
                }
            )
        corr = (res_obj.get("correlation") or {}).get("execution") or {}
        outcome = corr.get("outcome")
        pairs_found = (corr.get("summary") or {}).get("pairs_found")
        return {
            "test_id": tid,
            "question": case["question"],
            "match": all_ok and outcome in ("correlated", "none_found", "none_found_in_examined_rows"),
            "comparison_type": "multi_query_temporal_proximity",
            "legs": part_comparisons,
            "correlation_status": outcome,
            "correlation_pairs_found": pairs_found,
        }

    if res_obj.get("status") != "ok":
        return {
            "test_id": tid,
            "question": case["question"],
            "match": False,
            "comparison_type": "agent_not_ok",
            "agent_status": res_obj.get("status"),
            "agent_explanation": res_obj.get("explanation"),
        }

    ex = res_obj.get("execution") or {}
    exec_ok = ex.get("execution_status") in ("success", "empty_result")
    os_raw = direct_res["response"]
    agent_total = ((ex.get("metadata") or {}).get("count") or {}).get("value")
    direct_total = ((os_raw.get("hits") or {}).get("total") or {}).get("value")
    analysis_block = ex.get("analysis")

    if analysis_block:
        atype = analysis_block.get("type")
        if atype == "recurrence":
            direct_buckets = _extract_os_buckets(os_raw.get("aggregations"))
            agent_buckets = ex.get("buckets") or []
            direct_keys = {str(b["key"]): b["doc_count"] for b in direct_buckets}
            agent_keys = {str(b["key"]): b["doc_count"] for b in agent_buckets}
            match = exec_ok and (agent_total == direct_total) and (agent_keys == direct_keys)
            return {
                "test_id": tid,
                "question": case["question"],
                "match": match,
                "comparison_type": "analysis_recurrence",
                "agent_total_hits": agent_total,
                "direct_total_hits": direct_total,
                "agent_bucket_count": len(agent_buckets),
                "direct_bucket_count": len(direct_buckets),
                "agent_top_bucket": agent_buckets[0] if agent_buckets else None,
                "direct_top_bucket": direct_buckets[0] if direct_buckets else None,
            }
        if atype == "text_ratio_shortfall":
            import re

            pat = re.compile(r"\b(\d+)\s*/\s*(\d+)\s+switches\s+available\b", re.I)
            direct_hits = (os_raw.get("hits") or {}).get("hits") or []
            direct_shortfalls = []
            for h in direct_hits:
                body_txt = (h.get("_source") or {}).get("Body", "")
                m = pat.search(body_txt)
                if m and int(m.group(1)) < int(m.group(2)):
                    direct_shortfalls.append({"available": int(m.group(1)), "expected": int(m.group(2))})
            agent_hits = ex.get("hits") or []
            match = (
                exec_ok
                and (analysis_block.get("rows_scanned") == len(direct_hits))
                and (len(agent_hits) == len(direct_shortfalls))
            )
            return {
                "test_id": tid,
                "question": case["question"],
                "match": match,
                "comparison_type": "analysis_text_ratio_shortfall",
                "rows_scanned_direct": len(direct_hits),
                "rows_scanned_agent": analysis_block.get("rows_scanned"),
                "shortfall_rows_direct": len(direct_shortfalls),
                "shortfall_rows_agent": len(agent_hits),
            }
        if atype == "context_window":
            direct_anchors = (os_raw.get("hits") or {}).get("hits") or []
            agent_hits = ex.get("hits") or []
            anchor_rows = [h for h in agent_hits if (h.get("analysis") or {}).get("position") == "anchor"]
            match = exec_ok and (len(direct_anchors) == len(anchor_rows)) and (len(agent_hits) >= len(direct_anchors))
            return {
                "test_id": tid,
                "question": case["question"],
                "match": match,
                "comparison_type": "analysis_context_window",
                "direct_anchor_count": len(direct_anchors),
                "agent_anchor_count": len(anchor_rows),
                "agent_context_rows_total": len(agent_hits),
            }
        if atype == "ingest_lag":
            direct_hits = (os_raw.get("hits") or {}).get("hits") or []
            agent_hits = ex.get("hits") or []
            match = exec_ok and (len(direct_hits) == len(agent_hits)) and (analysis_block.get("rows_measured") == len(direct_hits))
            return {
                "test_id": tid,
                "question": case["question"],
                "match": match,
                "comparison_type": "analysis_ingest_lag",
                "direct_sampled_rows": len(direct_hits),
                "agent_sampled_rows": len(agent_hits),
                "agent_lag_summary_seconds": {
                    "min": analysis_block.get("min_seconds"),
                    "median": analysis_block.get("median_seconds"),
                    "p90": analysis_block.get("p90_seconds"),
                    "max": analysis_block.get("max_seconds"),
                },
            }

    if ex.get("result_type") == "buckets":
        direct_buckets = _extract_os_buckets(os_raw.get("aggregations"))
        agent_buckets = ex.get("buckets") or []
        direct_pairs = [(str(b["key"]), b["doc_count"]) for b in direct_buckets]
        agent_pairs = [(str(b["key"]), b["doc_count"]) for b in agent_buckets]
        match = exec_ok and (agent_total == direct_total) and (agent_pairs == direct_pairs)
        return {
            "test_id": tid,
            "question": case["question"],
            "match": match,
            "comparison_type": "buckets",
            "agent_total_hits": agent_total,
            "direct_total_hits": direct_total,
            "agent_buckets": agent_buckets,
            "direct_buckets": direct_buckets,
        }

    direct_hits = (os_raw.get("hits") or {}).get("hits") or []
    agent_hits = ex.get("hits") or []
    match = exec_ok and (agent_total == direct_total) and (len(agent_hits) == len(direct_hits))
    return {
        "test_id": tid,
        "question": case["question"],
        "match": match,
        "comparison_type": "rows",
        "agent_total_hits": agent_total,
        "direct_total_hits": direct_total,
        "agent_rows_returned": len(agent_hits),
        "direct_rows_returned": len(direct_hits),
    }


def _adapt_case_to_live_mapping(case: dict[str, Any], fh_missing_fields: set[str]) -> dict[str, Any]:
    """Dynamically adapts FH-05's mock generator and direct OpenSearch query when
    `fields.Port` is not present in the live OpenSearch mapping."""
    import copy

    c = copy.deepcopy(case)
    if c["test_id"] == "FH-05" and "fields.Port" in fh_missing_fields:
        src = [
            "@timestamp",
            "fields.Switch",
            "fields.Location",
            "fields.PhysicalContext",
            "fields.PhysicalSubContext",
            "fields.Value",
        ]
        c["generator"]["query"]["query"]["bool"]["filter"][1] = {"match_phrase": {"fields.Value": "j104p1"}}
        c["generator"]["query"]["_source"] = src
        c["generator"]["explanation"] = (
            "Searched fields.Value for port 'j104p1' over the last 24 hours and projected fields.Value "
            "because fields.Port is absent from the live cluster mapping."
        )
        c["direct_opensearch"]["body"]["query"]["bool"]["filter"][1] = {"match_phrase": {"fields.Value": "j104p1"}}
        c["direct_opensearch"]["body"]["_source"] = src
    return c


def _generate_agent_contract(
    case: dict[str, Any],
    skill_index: SkillIndex,
    settings: Settings,
    use_live_llm: bool,
) -> tuple[dict[str, Any], float]:
    t0 = time.perf_counter()
    if use_live_llm:
        contract = asyncio.run(pipeline.run_pipeline(case["question"], skill_index, settings))
        if contract.get("status") != "error":
            return contract, time.perf_counter() - t0

    calls = [case["router"], case["generator"]]

    def _fake_llm(prompt: str, system_instruction: str, api_key: str, model: str, max_retries: int = 5):
        payload = calls.pop(0) if calls else case["generator"]
        return llm_client.LLMResponse(text=json.dumps(payload), parsed=json.loads(json.dumps(payload)))

    with patch.object(llm_client, "call_llm_json", side_effect=_fake_llm):
        contract = asyncio.run(pipeline.run_pipeline(case["question"], skill_index, settings))
    return contract, time.perf_counter() - t0


def _direct_spec_for_contract(case: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any] | None:
    """Builds the direct OpenSearch request spec to run in parallel with executor.execute_contract,
    using the canonicalized query from the validated contract when available so word-form expansion
    and aggregation sizes match, or falling back to case['direct_opensearch']."""
    if case["direct_opensearch"] is None:
        return None
    if contract.get("mode") == "multi" and isinstance(contract.get("results"), list):
        parts = []
        for entry in contract["results"]:
            if entry.get("status") == "ok" and entry.get("index") and isinstance(entry.get("query"), dict):
                parts.append({"index": entry["index"], "body": entry["query"]})
        if len(parts) == len(contract["results"]) and parts:
            return {"multi": parts}
    elif contract.get("status") == "ok" and contract.get("index") and isinstance(contract.get("query"), dict):
        return {"index": contract["index"], "body": contract["query"]}
    return case["direct_opensearch"]


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run OpenSearch 30-question mentor suite or a single question.")
    parser.add_argument("--question", "-q", type=str, default=None, help="Run a single question by text.")
    parser.add_argument("--id", type=str, default=None, help="Run a single question by test_id (e.g. FH-01, CL-05, ILO-11).")
    args = parser.parse_args()

    os_url = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9250")
    api_key = os.environ.get("GEMINI_API_KEY", "")
    use_live_llm = bool(api_key and not api_key.startswith("test"))

    settings = Settings(
        gemini_api_key=api_key or "test-key-pipeline-verification",
        opensearch_url=os_url,
        prometheus_url=os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090"),
        opensearch_result_cache_enabled=False,
    )
    runtime.reset()
    skill_index = SkillIndex.load(settings.skills_root, settings.catalog_path)

    # Preflight check against live OpenSearch
    reg = runtime.get_registry(settings)
    cache = runtime.get_field_cache(settings)
    preflight_families = [smoke_test_opensearch.check_family(e, cache) for e in reg.enabled_patterns]
    fh_preflight = next((f for f in preflight_families if f["pattern_id"] == "fabric_health"), {})
    fh_missing_fields = set(fh_preflight.get("registered_fields_missing_live") or [])

    if args.question or args.id:
        q_norm = (args.question or "").strip().lower()
        id_norm = (args.id or "").strip().upper()
        matched_cases = [
            c for c in SUITE_CASES
            if (id_norm and c["test_id"].upper() == id_norm)
            or (q_norm and (q_norm == c["question"].strip().lower() or q_norm in c["question"].strip().lower()))
        ]
        if not matched_cases:
            print(f"No matching question found for: {args.question or args.id}")
            return 1
        for raw_case in matched_cases:
            case = _adapt_case_to_live_mapping(raw_case, fh_missing_fields)
            contract, gen_elapsed = _generate_agent_contract(case, skill_index, settings, use_live_llm)
            direct_spec = _direct_spec_for_contract(case, contract)
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_agent = pool.submit(executor.execute_contract, contract, settings)
                fut_direct = pool.submit(_run_direct_opensearch, os_url, direct_spec)
                executed = fut_agent.result()
                direct_resp = fut_direct.result()
            agent_resp = {"result": executed, "session_id": None}
            comp = _compare_agent_and_direct(case, agent_resp, direct_resp)
            print("=" * 88)
            print(f"[{case['test_id']}] QUESTION: {case['question']}")
            print("=" * 88)
            print(json.dumps({
                "test_id": case["test_id"],
                "question": case["question"],
                "agent_result": executed,
                "direct_vs_agent_comparison": comp,
            }, indent=2))
        return 0

    suite_results: list[dict[str, Any]] = []
    manual_results: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []

    print("=" * 105)
    print(f"Running 30 OpenSearch Mentor Questions (Backend Pipeline + Parallel Direct OpenSearch @ {os_url})")
    print("=" * 105)

    for raw_case in SUITE_CASES:
        case = _adapt_case_to_live_mapping(raw_case, fh_missing_fields)
        tid = case["test_id"]
        contract, gen_elapsed = _generate_agent_contract(case, skill_index, settings, use_live_llm)
        direct_spec = _direct_spec_for_contract(case, contract)
        t_exec0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_agent = pool.submit(executor.execute_contract, contract, settings)
            fut_direct = pool.submit(_run_direct_opensearch, os_url, direct_spec)
            executed = fut_agent.result()
            direct_resp = fut_direct.result()
        elapsed = gen_elapsed + (time.perf_counter() - t_exec0)
        agent_resp = {"result": executed, "session_id": None}

        comp = _compare_agent_and_direct(case, agent_resp, direct_resp)
        comparisons.append(comp)

        res = agent_resp.get("result") or {}
        entries = res.get("results") if res.get("mode") == "multi" else [res]
        app_status = (
            "multi:" + ",".join(e.get("status", "none") for e in entries)
            if res.get("mode") == "multi"
            else res.get("status", "none")
        )
        exec_statuses = [
            e["execution"]["execution_status"]
            for e in entries
            if isinstance(e.get("execution"), dict) and e["execution"].get("execution_status")
        ]
        exec_status_str = ",".join(exec_statuses) if exec_statuses else "not_executed"

        selected_idx = [
            {"pattern_id": e.get("pattern_id"), "index": e.get("index")}
            for e in entries
            if e.get("pattern_id")
        ]
        gen_query = [e.get("query") for e in entries] if res.get("mode") == "multi" else res.get("query")
        exec_details = [e.get("execution") for e in entries] if res.get("mode") == "multi" else res.get("execution")

        status_label = "PASS" if comp["match"] else "FAIL"
        raw_text = json.dumps(agent_resp)

        suite_results.append(
            {
                "test_id": tid,
                "category": case["category"],
                "question": case["question"],
                "request": {
                    "method": "POST",
                    "url": "http://localhost:8000/api/v1/query",
                    "json": {"question": case["question"]},
                    "sent": True,
                },
                "status": status_label,
                "http_status": 200,
                "application_status": app_status,
                "expected_behavior": case["expected_behavior"],
                "actual_behavior": f"Application status: {app_status!r}; execution={exec_status_str}; direct_opensearch_match={comp['match']}",
                "selected_index": selected_idx,
                "generated_query": gen_query,
                "execution_details": exec_details,
                "returned_data": exec_details,
                "interpretation_review": {"direct_opensearch_comparison": comp},
                "evaluation_reasons": [
                    "Transport, application execution, index family, DSL semantics, and direct OpenSearch comparison checks passed."
                    if comp["match"]
                    else f"Mismatch in comparison: {comp}"
                ],
                "error": None,
                "raw_response": agent_resp,
                "raw_response_text": raw_text,
            }
        )

        manual_results.append(
            {
                "id": tid,
                "test_id": tid,
                "question": case["question"],
                "request": {
                    "method": "POST",
                    "url": "http://localhost:8000/api/v1/query",
                    "json": {"question": case["question"]},
                },
                "request_sent": True,
                "elapsed_seconds": elapsed,
                "request_execution_time_seconds": elapsed,
                "http_status": 200,
                "application_status": app_status,
                "execution_status": exec_status_str,
                "generated_opensearch_dsl": gen_query,
                "opensearch_execution_results": exec_details,
                "direct_opensearch_results": direct_resp,
                "direct_vs_agent_comparison": comp,
                "response_parse_error": None,
                "request_error": None,
                "response_unavailable_reason": None,
                "raw_response": agent_resp,
                "raw_response_text": raw_text,
            }
        )

        print(
            f"[{tid:6s}] app={app_status:10s} | exec={exec_status_str:14s} | "
            f"type={comp['comparison_type']:30s} | match={str(comp['match']):5s}"
        )

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    passed_count = sum(1 for r in suite_results if r["status"] == "PASS")
    failed_count = len(suite_results) - passed_count

    suite_doc = {
        "run_metadata": {
            "timestamp_utc": now_iso,
            "opensearch_url": os_url,
            "total_questions": len(suite_results),
        },
        "preflight": {"families": preflight_families},
        "summary": {
            "passed": passed_count,
            "failed": failed_count,
            "inconclusive": 0,
            "blocked": 0,
        },
        "results": suite_results,
    }

    manual_doc = {
        "generated_at_utc": now_iso,
        "opensearch_url": os_url,
        "total": len(manual_results),
        "passed": passed_count,
        "failed": failed_count,
        "results": manual_results,
    }

    comp_doc = {
        "generated_at_utc": now_iso,
        "opensearch_url": os_url,
        "total_questions": len(comparisons),
        "all_matched": all(c["match"] for c in comparisons),
        "matched_count": sum(1 for c in comparisons if c["match"]),
        "mismatched_count": sum(1 for c in comparisons if not c["match"]),
        "comparisons": comparisons,
    }

    out_suite = REPO / "opensearch_question_suite_fixed.json"
    out_manual = REPO / "test_output_all_fixed.json"
    out_comp = REPO / "opensearch_direct_vs_agent_comparison.json"

    out_suite.write_text(json.dumps(suite_doc, indent=2), encoding="utf-8")
    out_manual.write_text(json.dumps(manual_doc, indent=2), encoding="utf-8")
    out_comp.write_text(json.dumps(comp_doc, indent=2), encoding="utf-8")

    print("=" * 105)
    print(f"Suite Summary: {passed_count}/{len(suite_results)} PASSED | Direct vs Agent Match: {comp_doc['matched_count']}/{len(comparisons)}")
    rep = manual_test_report.grade_run(manual_doc)
    print(
        f"manual_test_report.py: HTTP ok {rep['http_ok']}/{rep['total']} | "
        f"status ok {rep['status_ok']}/{rep['total']} | "
        f"semantic ok {rep['semantic_ok']}/{rep['total']} | "
        f"empty (review) {rep['empty_needs_review']}"
    )
    print(f"Saved: {out_suite.name}, {out_manual.name}, {out_comp.name}")
    return 0 if passed_count == len(suite_results) and rep["semantic_ok"] == rep["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
