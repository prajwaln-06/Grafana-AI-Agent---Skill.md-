import json
import logging
from typing import AsyncGenerator

from google.adk.agents.base_agent import BaseAgent
from google.adk.events.event import Event
from google.adk.agents.invocation_context import InvocationContext

from app.pipeline import PipelineContext, run_pipeline
from app.config import get_settings
from app import executor
from app.skill_index import SkillIndex

logger = logging.getLogger(__name__)


class ObservabilityQueryBuilderAgent(BaseAgent):
    _skill_index: SkillIndex | None = None

    def _get_skill_index(self) -> SkillIndex:
        if self._skill_index is None:
            settings = get_settings()
            self._skill_index = SkillIndex.load(settings.skills_root)
        return self._skill_index

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        settings = get_settings()
        skill_index = self._get_get_skill_index_or_reload()

        # 1. Retrieve the question from the user message
        question = ""
        if ctx.user_content and ctx.user_content.parts:
            question = "".join(part.text for part in ctx.user_content.parts if part.text)

        session_state = ctx.session.state or {}

        # 2. Check for pending alert confirmation first
        pending_proposal = session_state.get("pending_alert_proposal")
        clean_question = question.strip().lower()

        if pending_proposal and clean_question in ("yes", "confirm", "y", "no", "discard", "cancel", "n"):
            if clean_question in ("yes", "confirm", "y"):
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
                    if outcome.status == "success":
                        result = {
                            "status": "created",
                            "rule_uid": outcome.rule_uid,
                            "deeplink": outcome.deeplink,
                        }
                    else:
                        result = {"error": f"Grafana error ({outcome.status}): {outcome.error}"}
                except Exception as e:
                    logger.exception("Failed to create alert rule")
                    result = {"error": f"Unexpected error: {str(e)}"}
            else:
                result = {"status": "discarded"}

            # Clear state and return confirmation outcome
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                message=json.dumps({"result": result, "session_id": None}),
                state={
                    "pending_alert_proposal": None,
                    "previous_question": None,
                    "previous_result": None,
                }
            )
            return

        # 3. Check for clarification turn
        previous_question = session_state.get("previous_question")
        previous_result = session_state.get("previous_result")

        if previous_question and previous_result:
            pipeline_context = PipelineContext(
                previous_question=previous_question,
                previous_result=previous_result,
                clarification_answer=question,
            )
        else:
            pipeline_context = PipelineContext()

        # 4. Run the pipeline
        try:
            import asyncio
            contract = await asyncio.wait_for(
                run_pipeline(question, skill_index, settings, context=pipeline_context),
                timeout=settings.pipeline_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.error("Pipeline timed out after %.0fs", settings.pipeline_timeout_seconds)
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                message=json.dumps({
                    "error": f"The query pipeline took longer than {settings.pipeline_timeout_seconds:.0f}s and was aborted. Please try again."
                }),
            )
            return
        except Exception:
            logger.exception("Pipeline failed for question: %s", question)
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                message=json.dumps({"error": "The query pipeline failed unexpectedly. Please try again."}),
            )
            return

        # 5. Check if it needs clarification or confirmation
        needs_clar = self._needs_clarification(contract)
        needs_conf = self._needs_confirmation(contract)

        if needs_clar:
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                message=json.dumps({"result": contract, "session_id": ctx.session.id}),
                state={
                    "previous_question": question,
                    "previous_result": contract,
                }
            )
            return

        if needs_conf:
            proposal_entry = None
            if contract.get("status") == "alert_rule_proposed":
                proposal_entry = contract
            elif contract.get("mode") == "multi":
                for entry in contract.get("results", []):
                    if entry.get("status") == "alert_rule_proposed":
                        proposal_entry = entry
                        break

            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                message=json.dumps({"result": contract, "session_id": ctx.session.id}),
                state={
                    "pending_alert_proposal": proposal_entry,
                    "previous_question": question,
                    "previous_result": contract,
                }
            )
            return

        # 6. Execute contract
        executed = executor.execute_contract(contract, settings)
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            message=json.dumps({"result": executed, "session_id": None}),
            state={
                "previous_question": None,
                "previous_result": None,
                "pending_alert_proposal": None,
            }
        )

    def _get_get_skill_index_or_reload(self) -> SkillIndex:
        return self._get_skill_index()

    def reload_skill(self) -> None:
        settings = get_settings()
        new_index = SkillIndex.load(settings.skills_root)
        self._skill_index = new_index

    def _needs_clarification(self, contract: dict) -> bool:
        if self._entry_needs_clarification(contract):
            return True
        if contract.get("mode") == "multi":
            return any(self._entry_needs_clarification(entry) for entry in contract.get("results", []))
        return False

    def _entry_needs_clarification(self, entry: dict) -> bool:
        if entry.get("status") == "ambiguous_metric":
            return True
        if entry.get("status") == "declined" and entry.get("reason") == "parameter_requires_clarification":
            return True
        return False

    def _needs_confirmation(self, contract: dict) -> bool:
        if self._entry_needs_confirmation(contract):
            return True
        if contract.get("mode") == "multi":
            return any(self._entry_needs_confirmation(entry) for entry in contract.get("results", []))
        return False

    def _entry_needs_confirmation(self, entry: dict) -> bool:
        return entry.get("status") == "alert_rule_proposed"


root_agent = ObservabilityQueryBuilderAgent(
    name="app",
    description="Observability natural language query construction and execution agent.",
)
