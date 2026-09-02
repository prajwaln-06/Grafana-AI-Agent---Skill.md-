import json
import logging
import uvicorn
from fastapi import FastAPI, HTTPException, Request
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
from app.skill_index import SkillIndex

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

app = adk_web_server.get_fast_api_app()


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
        # These are deliberately non-secret runtime values. They let callers
        # verify the configuration held by this server process (which can
        # differ from a newly started shell after an .env edit).
        "feature_flags": {
            "alert_rule_creation_enabled": get_settings().alert_rule_creation_enabled,
            "dependent_query_resolution_enabled": get_settings().dependent_query_resolution_enabled,
        },
    }


@app.post("/api/v1/admin/reload-skill")
async def reload_skill():
    import dataclasses
    from app.agent import root_agent
    from app.skill_index import SkillIndexError
    try:
        root_agent.reload_skill()
    except SkillIndexError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Failed to reload skill: {str(e)}")
        
    skill_index = root_agent._get_skill_index()
    return {
        "skill_name": skill_index.metadata.name,
        "skill_version": skill_index.metadata.version,
        "routing_rows": len(skill_index.routing_rows),
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
    except Exception as e:
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

    # The flag gates the WRITE boundary itself, not merely proposal
    # generation. A proposal can outlive a configuration change in session
    # state, so checking it only when the proposal is generated would allow
    # an old proposal to create a Grafana rule after the capability was
    # disabled. Discarding remains available while disabled.
    if not settings.alert_rule_creation_enabled:
        raise HTTPException(
            status_code=403,
            detail="Alert-rule creation is currently disabled on this deployment.",
        )

    from app import grafana_client
    alert_rule = pending_proposal.get("alert_rule")
    try:
        outcome = grafana_client.create_alert_rule(
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
    port = int(os.environ.get("PORT", getattr(get_settings(), "api_port", 8008)))
    uvicorn.run("app.api.main:app", host="0.0.0.0", port=port, reload=True)
