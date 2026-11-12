import time
import json
import yaml
from pathlib import Path
from typing import Dict, Any
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from app.agent.automation.state import AutomationState
from app.agent.automation.schema import TicketDraft
from app.agent.telemetry import record_node_telemetry

_YAML_PATH = Path(__file__).parent / "prompt.yaml"
try:
    with open(_YAML_PATH, "r", encoding="utf-8") as f:
        _CONFIG = yaml.safe_load(f) or {}
except Exception as e:
    print(f"[WARN] Failed to load finalize prompt.yaml: {e}")
    _CONFIG = {}

FINALIZE_PROMPT_TEMPLATE = _CONFIG.get("system_prompt", "Confirm ticket ready.")


def make_finalize_node(llm):
    """
    Creates the finalize node.
    Validates complete draft with Pydantic and generates confirmation message.
    """
    async def finalize_node(state: AutomationState) -> Dict[str, Any]:
        start_time = time.time()
        drafts = state.get("drafts")
        if not drafts:
            pending = state.get("pending_draft")
            if isinstance(pending, list) and len(pending) > 1:
                drafts = pending
            elif state.get("draft"):
                drafts = [state.get("draft")]
            else:
                drafts = []

        agent = state.get("agent") or (state.get("active_workers")[0] if state.get("active_workers") else None)
        is_ticket_agent = agent in ("ticket_analytics", "ticket")
        is_task_agent = agent in ("project_analytics", "task", "project")

        if is_ticket_agent:
            is_task = False
        elif is_task_agent:
            is_task = True
        else:
            is_task = (
                state.get("entity_type") == "task"
                or state.get("automation_action") == "CREATE_TASK"
                or any(isinstance(d, dict) and d.get("entityType") == "task" for d in drafts)
            )

        if len(drafts) > 1:
            clean_drafts = []
            for d in drafts:
                if is_task:
                    item = dict(d)
                    item["entityType"] = "task"
                    clean_drafts.append(item)
                else:
                    try:
                        clean_drafts.append(TicketDraft(**d).model_dump(exclude_none=False))
                    except Exception:
                        clean_drafts.append(d)

            entity_word = "tasks" if is_task else "tickets"
            summary_text = f"I've prepared {len(clean_drafts)} {entity_word} for you. Please review the details before proceeding."
            primary_draft = clean_drafts[0] if clean_drafts else {}
            elapsed = time.time() - start_time
            record_node_telemetry(
                node="automation_finalize",
                latency_ms=int(elapsed * 1000),
                status="SUCCESS",
                output_data={"drafts_count": len(clean_drafts), "summary": summary_text},
            )
            return {
                "draft": primary_draft,
                "drafts": clean_drafts,
                "summary": summary_text,
                "automation_summary": summary_text,
                "entity_type": "task" if is_task else "ticket",
                "automation_action": "CREATE_TASK" if is_task else "CREATE_TICKET",
                "messages": [AIMessage(content=summary_text)],
            }
        else:
            draft = drafts[0] if drafts else (state.get("draft") or {})
            title = draft.get("name") or ("your task" if is_task else "your ticket")
            space = draft.get("taskSpaceName") if is_task else (draft.get("ticketSpaceName") or "Default Space")
            source_ticket_code = draft.get("sourceTicketCode")
            source_task_code = draft.get("sourceTaskCode")

            if is_task:
                clean_draft = dict(draft)
                clean_draft["entityType"] = "task"
                if source_task_code:
                    summary_text = f"I've prepared your draft based on **{source_task_code}**. Please review the details before proceeding."
                elif space:
                    summary_text = f"I've prepared your task draft '{title}' in '{space}'. Please review the details before proceeding."
                else:
                    summary_text = f"I've prepared your task draft '{title}'. Please review the details before proceeding."
            elif source_ticket_code:
                summary_text = f"I've prepared a new ticket draft based on **{source_ticket_code}**. Please review the details before proceeding."
                try:
                    clean_draft = TicketDraft(**draft).model_dump(exclude_none=False)
                except Exception:
                    clean_draft = draft
            else:
                summary_text = f"I've prepared your ticket '{title}' in the '{space}' space. Please review the details before proceeding."
                try:
                    clean_draft = TicketDraft(**draft).model_dump(exclude_none=False)
                except Exception:
                    clean_draft = draft

            elapsed = time.time() - start_time
            record_node_telemetry(
                node="automation_finalize",
                latency_ms=int(elapsed * 1000),
                status="SUCCESS",
                output_data={"draft": clean_draft, "summary": summary_text},
            )

            return {
                "draft": clean_draft,
                "drafts": [clean_draft],
                "summary": summary_text,
                "automation_summary": summary_text,
                "entity_type": "task" if is_task else "ticket",
                "automation_action": "CREATE_TASK" if is_task else "CREATE_TICKET",
                "messages": [AIMessage(content=summary_text)],
            }

    return finalize_node
