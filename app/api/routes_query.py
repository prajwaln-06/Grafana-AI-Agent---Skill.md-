"""api/routes_query.py -- POST /api/v1/query, the main entry point."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging

from fastapi import APIRouter, HTTPException, Request

from app import executor
from app.api.schemas import QueryRequest, QueryResponse
from app.config import get_settings
from app.pipeline import PipelineContext, run_pipeline
from app.session_store import get_session_store

logger = logging.getLogger(__name__)
router = APIRouter()

CLARIFICATION_STATUSES = {"ambiguous_metric"}
CLARIFICATION_REASONS = {"parameter_requires_clarification"}


@router.post("/api/v1/query", response_model=QueryResponse)
async def query(request: Request, body: QueryRequest) -> QueryResponse:
    settings = get_settings()
    skill_index = request.app.state.skill_index
    store = get_session_store(settings.session_ttl_seconds)

    context = PipelineContext()
    if body.session_id:
        entry = store.get(body.session_id)
        if entry is None:
            logger.warning("Session %s not found or expired; starting fresh context", body.session_id)
            context = PipelineContext()
        else:
            context = PipelineContext(
                previous_question=entry.question,
                previous_result=entry.result,
                clarification_answer=body.question,
            )
            store.delete(body.session_id)

    try:
        contract = await asyncio.wait_for(
            run_pipeline(body.question, skill_index, settings, context=context),
            timeout=settings.pipeline_timeout_seconds,
        )
    except asyncio.TimeoutError:
        # PIPELINE_TIMEOUT_SECONDS exists in .env for exactly this: a hung
        # Gemini call or an unresponsive Prometheus/OpenSearch backend
        # should never leave an HTTP request hanging indefinitely. Without
        # this wrapper the setting was declared but silently never enforced
        # anywhere -- a request could hang forever with no ceiling at all.
        logger.error("Pipeline timed out after %.0fs for question: %s",
                      settings.pipeline_timeout_seconds, body.question)
        raise HTTPException(
            status_code=504,
            detail=f"The query pipeline took longer than {settings.pipeline_timeout_seconds:.0f}s "
                   f"and was aborted. Please try again.",
        )
    except Exception:
        logger.exception("Pipeline failed for question: %s", body.question)
        raise HTTPException(status_code=502, detail="The query pipeline failed unexpectedly. Please try again.")

    if _needs_clarification(contract):
        new_session_id = store.create(body.question, contract)
        return QueryResponse(result=contract, session_id=new_session_id)

    executed = executor.execute_contract(contract, settings)
    return QueryResponse(result=executed, session_id=None)


from app import label_discovery, prometheus_client, time_utils


@router.get("/api/catalog")
async def get_catalog(request: Request) -> dict:
    return {
        "prometheus": {
            "id": "prometheus",
            "label": "Prometheus Metrics",
            "description": "Infrastructure and hardware telemetry from Prometheus.",
            "metrics": [
                {
                    "id": "cpu",
                    "label": "CPU Utilization",
                    "description": "Host CPU usage, load average, and idle percentages.",
                    "queries": [
                        {
                            "id": "cpu_busy",
                            "label": "CPU Busy %",
                            "expr": '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
                            "unit": "percent",
                            "legend": "{{instance}}",
                        },
                        {
                            "id": "node_load1",
                            "label": "1-Minute Load Average",
                            "expr": "node_load1",
                            "unit": "short",
                            "legend": "{{instance}}",
                        },
                    ],
                },
                {
                    "id": "memory",
                    "label": "Host Memory",
                    "description": "Available, free, and cached memory.",
                    "queries": [
                        {
                            "id": "mem_avail",
                            "label": "Available Memory",
                            "expr": "node_memory_MemAvailable_bytes",
                            "unit": "bytes",
                            "legend": "{{instance}}",
                        },
                        {
                            "id": "mem_free",
                            "label": "Free Memory",
                            "expr": "node_memory_MemFree_bytes",
                            "unit": "bytes",
                            "legend": "{{instance}}",
                        },
                    ],
                },
                {
                    "id": "gpu",
                    "label": "GPU Telemetry (DCGM)",
                    "description": "NVIDIA GPU temperatures, utilization, and power.",
                    "queries": [
                        {
                            "id": "gpu_temp",
                            "label": "GPU Temperature",
                            "expr": "DCGM_FI_DEV_GPU_TEMP",
                            "unit": "celsius",
                            "legend": "GPU {{gpu}}",
                        },
                        {
                            "id": "gpu_util",
                            "label": "GPU Utilization",
                            "expr": "DCGM_FI_DEV_GPU_UTIL",
                            "unit": "percent",
                            "legend": "GPU {{gpu}}",
                        },
                    ],
                },
            ],
        }
    }


@router.get("/api/labels")
async def get_labels(request: Request, labelName: str, metric: str | None = None) -> dict:
    settings = get_settings()
    values = label_discovery.discover_label_values(settings.prometheus_url, labelName, metric=metric)
    return {"values": list(values)}


@router.post("/api/query")
async def run_catalog_query(request: Request, body: dict) -> dict:
    settings = get_settings()
    source_id = body.get("sourceId", "prometheus")
    metric_id = body.get("metricId", "")
    query_id = body.get("queryId", "")
    range_str = body.get("range", "1h")
    
    catalog = await get_catalog(request)
    source = catalog.get(source_id, {})
    metrics = source.get("metrics", [])
    found_metric = next((m for m in metrics if m["id"] == metric_id), None)
    if not found_metric and metrics:
        found_metric = metrics[0]
    
    queries = found_metric.get("queries", []) if found_metric else []
    found_query = next((q for q in queries if q["id"] == query_id), None)
    if not found_query and queries:
        found_query = queries[0]
        
    if not found_query:
        raise HTTPException(status_code=404, detail="Query not found in catalog.")

    expr = found_query["expr"]
    
    # Parse range
    range_map = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400}
    duration_sec = range_map.get(range_str, 3600)
    step_sec = 15 if duration_sec <= 900 else (60 if duration_sec <= 3600 else 300)
    
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=duration_sec)
    
    series_list = []
    try:
        outcome = prometheus_client.query_range(
            settings.prometheus_url, expr, start, end, step_seconds=step_sec,
            timeout=settings.prometheus_timeout_seconds
        )
        if outcome.status == "success" and outcome.raw_data:
            for item in outcome.raw_data.get("result", []):
                metric_labels = item.get("metric", {})
                name = metric_labels.get("instance") or metric_labels.get("gpu") or metric_labels.get("cpu") or found_query["label"]
                pts = []
                for t, v in item.get("values", []):
                    try:
                        val = float(v)
                        pts.append({"t": int(float(t)), "v": val})
                    except (ValueError, TypeError):
                        pass
                series_list.append({"name": name, "labels": metric_labels, "points": pts})
    except Exception as e:
        logger.error("Failed to query prometheus for %s: %s", expr, e)

    return {
        "source": {"id": source_id, "label": source.get("label", "Prometheus")},
        "metric": {"id": metric_id, "label": found_metric.get("label", "") if found_metric else ""},
        "query": {
            "id": found_query["id"],
            "label": found_query["label"],
            "expr": expr,
            "unit": found_query.get("unit", "short"),
        },
        "backend": "prometheus",
        "range": {"start": int(start.timestamp()), "end": int(end.timestamp()), "step": step_sec, "label": range_str},
        "series": series_list,
        "usedLlm": False,
    }


@router.post("/api/adk/glance/board")
async def get_glance_board(request: Request) -> dict:
    settings = get_settings()
    panels = []
    
    requested_ids = None
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("panel_ids"), list):
            requested_ids = body.get("panel_ids")
    except Exception:
        pass

    catalog = {
        "cpu_busy": ("CPU Busy %", '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)', "percent", "prometheus"),
        "memory_avail": ("Memory Available", "node_memory_MemAvailable_bytes", "bytes", "prometheus"),
        "gpu_temp": ("GPU Temperature", "DCGM_FI_DEV_GPU_TEMP", "celsius", "prometheus"),
        "gpu_util": ("GPU Utilization", "DCGM_FI_DEV_GPU_UTIL", "percent", "prometheus"),
        # Backward-compatible aliases
        "demo_latency": ("Memory Available", "node_memory_MemAvailable_bytes", "bytes", "prometheus"),
        "demo_errors": ("GPU Temperature", "DCGM_FI_DEV_GPU_TEMP", "celsius", "prometheus"),
        "error_logs": ("GPU Utilization", "DCGM_FI_DEV_GPU_UTIL", "percent", "prometheus"),
    }

    default_order = ["cpu_busy", "memory_avail", "gpu_temp", "gpu_util"]
    selected_ids = requested_ids if requested_ids else default_order

    seen = set()
    board_queries = []
    for pid in selected_ids:
        if pid in catalog and pid not in seen:
            seen.add(pid)
            title, expr, unit, source = catalog[pid]
            board_queries.append((pid, title, expr, unit, source))

    if not board_queries:
        board_queries = [(pid, *catalog[pid]) for pid in default_order]
    
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=1)
    
    for pid, title, expr, unit, source in board_queries:
        series_list = []
        latest_val = None
        status = "offline"
        
        try:
            outcome = prometheus_client.query_range(
                settings.prometheus_url, expr, start, end, step_seconds=60,
                timeout=settings.prometheus_timeout_seconds
            )
            if outcome.status == "success" and outcome.raw_data:
                status = "success"
                for item in outcome.raw_data.get("result", []):
                    metric_labels = item.get("metric", {})
                    name = metric_labels.get("instance") or metric_labels.get("gpu") or title
                    pts = []
                    for t, v in item.get("values", []):
                        try:
                            val = float(v)
                            pts.append({"t": int(float(t)), "v": val})
                            latest_val = val
                        except (ValueError, TypeError):
                            pass
                    series_list.append({"name": name, "labels": metric_labels, "points": pts})
        except Exception:
            status = "offline"
            
        panels.append({
            "panel_id": pid,
            "title": title,
            "unit": unit,
            "expr": expr,
            "source": source,
            "status": status,
            "latest_value": latest_val,
            "series": series_list,
        })
        
    return {"panels": panels, "range": "1h"}


@router.get("/api/search")
async def search_observability(request: Request, q: str = "") -> dict:
    return {
        "query": q,
        "dashboards": [],
        "panels": [],
        "metrics": [],
        "best": None,
    }


def _needs_clarification(contract: dict) -> bool:
    """True if the contract itself needs a clarifying answer before
    anything should be executed. Checks BOTH shapes SKILL.md §9 allows:
    a top-level status (mode: "single"), and a status nested inside any
    entry of a mode: "multi" response's `results` array -- §9 explicitly
    states results holds "the same per-status shapes" as single mode, so a
    multi-measurement question ("show me both X and Y") can have one
    measurement resolve cleanly while another comes back ambiguous. In that
    case we hold off on ALL execution, not just the ambiguous entry: once
    the user's answer changes the Generator's understanding of the request,
    it may reconstruct the whole multi-result differently, not just patch
    one entry."""
    if _entry_needs_clarification(contract):
        return True
    if contract.get("mode") == "multi":
        return any(_entry_needs_clarification(entry) for entry in contract.get("results", []))
    return False


def _entry_needs_clarification(entry: dict) -> bool:
    if entry.get("status") in CLARIFICATION_STATUSES:
        return True
    if entry.get("status") == "declined" and entry.get("reason") in CLARIFICATION_REASONS:
        return True
    return False
