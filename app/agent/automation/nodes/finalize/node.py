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

        if len(drafts) > 1:
            clean_drafts = []
            for d in drafts:
                try:
                    clean_drafts.append(TicketDraft(**d).model_dump(exclude_none=False))
                except Exception:
                    clean_drafts.append(d)
            summary_text = f"I've prepared {len(clean_drafts)} tickets for you. Please review the details before proceeding."
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
                "messages": [AIMessage(content=summary_text)],
            }
        else:
            draft = drafts[0] if drafts else (state.get("draft") or {})
            title = draft.get("name") or "your ticket"
            space = draft.get("ticketSpaceName") or "Default Space"
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
                "messages": [AIMessage(content=summary_text)],
            }

    return finalize_node
