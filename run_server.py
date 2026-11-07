import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Any

from google.adk.cli.utils.service_factory import create_session_service_from_options, create_artifact_service_from_options, create_memory_service_from_options
from google.adk.cli.utils.agent_loader import AgentLoader
from google.adk.auth.credential_service.in_memory_credential_service import InMemoryCredentialService
from google.adk.cli.api_server import ApiServer
from google.adk.evaluation.local_eval_sets_manager import LocalEvalSetsManager
from google.adk.evaluation.local_eval_set_results_manager import LocalEvalSetResultsManager
from google.genai import types

from app.config import get_settings

logger = logging.getLogger(__name__)

agents_dir = "."
session_service = create_session_service_from_options(base_dir=agents_dir)
artifact_service = create_artifact_service_from_options(base_dir=agents_dir)
memory_service = create_memory_service_from_options(base_dir=agents_dir)
credential_service = InMemoryCredentialService()
agent_loader = AgentLoader(agents_dir)
eval_sets_manager = LocalEvalSetsManager(agents_dir=agents_dir)
eval_set_results_manager = LocalEvalSetResultsManager(agents_dir=agents_dir)

adk_web_server = ApiServer(
    agent_loader=agent_loader,
    session_service=session_service,
    artifact_service=artifact_service,
    memory_service=memory_service,
    credential_service=credential_service,
    eval_sets_manager=eval_sets_manager,
    eval_set_results_manager=eval_set_results_manager,
    agents_dir=agents_dir,
    auto_create_session=True,
)


def _log_opensearch_readiness(settings) -> None:
    """Tells the operator at startup, in the log, whether the OpenSearch part is usable.
    Loading the registry with the skills root also proves every family's skill file exists.
    Never raises: a problem here must not stop Prometheus questions from being served."""
    from app.opensearch_catalog import runtime as os_runtime
    from app.opensearch_catalog.registry import RegistryError
    try:
        reg = os_runtime.get_registry(settings)
    except RegistryError as e:
        logger.error("OpenSearch is DISABLED (every OpenSearch question will be refused): %s", e)
        return
    except Exception:  # noqa: BLE001 -- startup logging must never crash the server
        logger.exception("Could not check the OpenSearch registry at startup")
        return
    names = ", ".join(p.pattern_id for p in reg.enabled_patterns)
    logger.info("OpenSearch registry loaded: %d enabled index families (%s).", len(reg.enabled_patterns), names)


@asynccontextmanager
async def _catalog_runtime_lifespan(_app: FastAPI):
    """Starts the background catalog refresh when
    CATALOG_RUNTIME_RECONCILIATION_ENABLED is true and stops it on shutdown.
    With the flag off (the default) this does nothing at all. A problem
    starting the worker is logged and never prevents the server from serving
    queries -- the pipeline then just has the static catalog."""
    from app.catalog import runtime as catalog_runtime

    worker_runtime = None
    settings = get_settings()
    _log_opensearch_readiness(settings)
    if settings.catalog_runtime_reconciliation_enabled:
        try:
            worker_runtime = catalog_runtime.get_runtime(settings)
            worker_runtime.start_worker()
        except Exception:
            logger.exception("Could not start the catalog refresh worker; continuing without it.")
            worker_runtime = None
    try:
        yield
    finally:
        if worker_runtime is not None:
            worker_runtime.stop_worker()


app = adk_web_server.get_fast_api_app(lifespan=_catalog_runtime_lifespan)

# ---- security: API key + CORS ----------------------------------------------------------
# Both settings existed in config.py but nothing enforced them, so every route
# (including the admin reload) was open. Now:
#   * API_KEY set    -> every route except the health probes needs header X-API-Key
#                       (compared in constant time).
#   * API_KEY unset  -> open (local development), and a warning is logged once.
#   * CORS_ALLOW_ORIGINS -> applied for real. The default "*" is only sensible locally.
_PUBLIC_PATHS = {"/readyz", "/health"}
_startup_settings = get_settings()
if not _startup_settings.api_key:
    logger.warning("API_KEY is not set: every endpoint is unauthenticated. Set API_KEY before exposing this service.")


@app.middleware("http")
async def _require_api_key(request: Request, call_next):
    expected = get_settings().api_key
    if not expected or request.method == "OPTIONS" or request.url.path in _PUBLIC_PATHS:
        return await call_next(request)
    supplied = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        return JSONResponse(status_code=401, content={"detail": "Missing or invalid API key."})
    return await call_next(request)


# Added AFTER the auth middleware so it wraps it: browser preflight (OPTIONS)
# requests get their CORS headers without needing a key.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_startup_settings.cors_allow_origins,
    allow_credentials=_startup_settings.cors_allow_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Backwards compatible API schemas
class QueryRequest(BaseModel):
    question: str
    session_id: Optional[str] = None


class QueryResponse(BaseModel):
    result: Any
    session_id: Optional[str] = None


class ConfirmAlertRequest(BaseModel):
    session_id: str
    confirm: bool = True


@app.get("/readyz")
async def readyz():
    return {"status": "ready", "skill_name": "observability-query-builder"}


@app.get("/api/v1/capabilities")
async def capabilities():
    import dataclasses
    from app.agent import root_agent
    skill_index = root_agent._get_skill_index()
    return {
        "skill_name": skill_index.metadata.name,
        "skill_version": skill_index.metadata.version,
        "routing_rows": [
            dataclasses.asdict(row)
            for row in skill_index.routing_rows
        ],
    }


@app.get("/api/v1/opensearch/status")
async def opensearch_status():
    """Read-only operational view of the OpenSearch part: is the registry valid (which also
    proves each family's skill file exists), which families are enabled, which analyses each
    supports, the query limits in force, and what the field-catalog cache currently holds.
    Never queries OpenSearch itself."""
    import dataclasses
    from app.opensearch_catalog import runtime as os_runtime
    from app.opensearch_catalog.context import Limits
    from app.opensearch_catalog.registry import RegistryError
    settings = get_settings()
    info: dict = {"registry_path": str(os_runtime.registry_path(settings))}
    try:
        reg = os_runtime.get_registry(settings)
        info["registry_valid"] = True
        info["registry_version"] = reg.registry_version
        info["patterns"] = [{"pattern_id": p.pattern_id, "index_pattern": p.index_pattern, "enabled": p.enabled,
                             "domain_reference": p.domain_reference, "max_window": p.max_window,
                             "analyses": sorted(getattr(p, "analyses", {}) or {})}
                            for p in reg.patterns]
    except RegistryError as e:
        info["registry_valid"] = False
        info["registry_error"] = str(e)
    info["limits"] = dataclasses.asdict(Limits.from_settings(settings))
    info["field_catalogs"] = os_runtime.field_cache_snapshots()
    info["result_cache"] = os_runtime.result_cache_snapshot(settings)
    return info


@app.post("/api/v1/admin/reload-skill")
async def reload_skill():
    from app.agent import root_agent
    from app.skill_index import SkillIndexError
    try:
        root_agent.reload_skill()
    except SkillIndexError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Failed to reload skill: {str(e)}")
        
    skill_index = root_agent._get_skill_index()
    # The OpenSearch index registry is cached for the life of the process; reload
    # it too, so an edited index_registry.json takes effect without a restart. A
    # broken registry is reported here (and every OpenSearch request then fails
    # closed until it is fixed); it does not undo the skill reload above.
    from app.opensearch_catalog import runtime as os_runtime
    return {
        "skill_name": skill_index.metadata.name,
        "skill_version": skill_index.metadata.version,
        "routing_rows": len(skill_index.routing_rows),
        "opensearch_registry": await asyncio.to_thread(os_runtime.reload_registry_status, get_settings()),
    }


@app.post("/api/v1/query", response_model=QueryResponse)
async def query(body: QueryRequest, request: Request) -> QueryResponse:
    runner = await adk_web_server.get_runner_async("app")
    session_id = body.session_id
    
    if session_id:
        session = await adk_web_server.session_service.get_session(
            app_name="app", user_id="default_user", session_id=session_id
        )
        if not session:
            raise HTTPException(
                status_code=410,
                detail="This session has expired or doesn't exist. Please ask the original question again."
            )
    else:
        import uuid
        session_id = "s-" + str(uuid.uuid4())

    events = []
    try:
        async for event in runner.run_async(
            user_id="default_user",
            session_id=session_id,
            new_message=types.Content(parts=[types.Part(text=body.question)]),
        ):
            events.append(event)
    except Exception:
        logger.exception("Agent run failed")
        raise HTTPException(status_code=502, detail="The query pipeline failed unexpectedly. Please try again.")

    if not events:
        raise HTTPException(status_code=502, detail="The query pipeline failed unexpectedly. Please try again.")

    last_event = events[-1]
    text_content = "".join(part.text for part in last_event.content.parts if part.text)
    
    try:
        response_data = json.loads(text_content)
    except Exception:
        return QueryResponse(
            result={"status": "error", "explanation": text_content},
            session_id=None
        )

    if "error" in response_data:
        error_msg = response_data["error"]
        if "took longer than" in error_msg.lower() or "timeout" in error_msg.lower():
            raise HTTPException(status_code=504, detail=error_msg)
        else:
            raise HTTPException(status_code=502, detail=error_msg)

    result = response_data.get("result")
    resp_session_id = response_data.get("session_id")
    if resp_session_id:
        return QueryResponse(result=result, session_id=session_id)
    return QueryResponse(result=result, session_id=None)


@app.post("/api/v1/alerts/confirm")
async def confirm_alert(body: ConfirmAlertRequest):
    settings = get_settings()
    session = await adk_web_server.session_service.get_session(
        app_name="app", user_id="default_user", session_id=body.session_id
    )
    if not session:
        raise HTTPException(
            status_code=410,
            detail="This session has expired or doesn't exist."
        )

    # The feature flag is re-checked on EVERY confirm, not only when no proposal exists:
    # turning the feature off must stop creation even for a proposal made earlier.
    if body.confirm and not settings.alert_rule_creation_enabled:
        raise HTTPException(
            status_code=403,
            detail="Alert-rule creation is currently disabled on this deployment."
        )
    pending_proposal = session.state.get("pending_alert_proposal")
    if not pending_proposal:
        raise HTTPException(
            status_code=409,
            detail="This session does not contain a pending alert proposal."
        )

    if not body.confirm:
        from google.adk.events.event import Event
        import uuid
        discard_event = Event(
            invocation_id="p-" + str(uuid.uuid4()),
            author="app",
            state={
                "pending_alert_proposal": None,
                "previous_question": None,
                "previous_result": None,
            }
        )
        await adk_web_server.session_service.append_event(session=session, event=discard_event)
        return {"status": "discarded"}

    from app import grafana_client
    alert_rule = pending_proposal.get("alert_rule")
    try:
        # Blocking HTTP call to Grafana: keep it off the event loop.
        outcome = await asyncio.to_thread(
            grafana_client.create_alert_rule,
            title=alert_rule["title"],
            condition_query=alert_rule["condition_query"],
            comparison_operator=alert_rule["comparison"]["operator"],
            threshold=alert_rule["comparison"]["threshold"],
            for_duration=alert_rule["for_duration"],
            folder_uid=settings.grafana_default_folder_uid or alert_rule.get("folder"),
            datasource_uid=settings.grafana_default_datasource_uid or alert_rule.get("datasource_uid"),
            grafana_url=settings.grafana_url,
            service_account_token=settings.grafana_service_account_token,
        )

        # Clear the proposal only when it is finished with (created, or a rule with this
        # identity already exists). If Grafana was unreachable or misconfigured the
        # proposal is KEPT so the person can simply confirm again once it is fixed.
        if outcome.status in ("success", "conflict"):
            from google.adk.events.event import Event
            import uuid
            clear_event = Event(
                invocation_id="p-" + str(uuid.uuid4()),
                author="app",
                state={
                    "pending_alert_proposal": None,
                    "previous_question": None,
                    "previous_result": None,
                }
            )
            await adk_web_server.session_service.append_event(session=session, event=clear_event)

        if outcome.status == "success":
            return {
                "status": "created",
                "rule_uid": outcome.rule_uid,
                "deeplink": outcome.deeplink,
            }
        elif outcome.status == "configuration_error":
            raise HTTPException(status_code=500, detail=f"Grafana configuration error: {outcome.error}")
        elif outcome.status == "endpoint_unreachable":
            raise HTTPException(status_code=502, detail=f"Grafana unreachable: {outcome.error}")
        elif outcome.status == "conflict":
            raise HTTPException(status_code=409, detail=f"Conflict: {outcome.error}")
        else:
            raise HTTPException(status_code=500, detail=f"Failed to create alert rule: {outcome.error}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to create alert rule")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


if __name__ == "__main__":
    import os
    # AGENT_PORT lets the server move off 8000 when another local service already holds it.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("AGENT_PORT", "8000")))
