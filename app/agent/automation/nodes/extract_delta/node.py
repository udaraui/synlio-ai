import os
import time
import json
import yaml
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from app.agent.automation.state import AutomationState
from app.agent.automation.schema import TicketDraft
from app.agent.telemetry import record_node_telemetry

_YAML_PATH = Path(__file__).parent / "prompt.yaml"
try:
    with open(_YAML_PATH, "r", encoding="utf-8") as f:
        _CONFIG = yaml.safe_load(f) or {}
except Exception as e:
    print(f"[WARN] Failed to load extract_delta prompt.yaml: {e}")
    _CONFIG = {}

EXTRACT_DELTA_PROMPT_TEMPLATE = _CONFIG.get("system_prompt", "Extract ticket parameters.")


def make_extract_delta_node(llm):
    """
    Creates the extract_delta node.
    Extracts ticket attributes, infers types/severity, resolves dates, and merges deltas.
    """
    async def extract_delta_node(state: AutomationState) -> Dict[str, Any]:
        start_time = time.time()
        messages = state.get("messages", [])
        pending_draft = state.get("pending_draft") or {}
        scope = state.get("scope") or {}
        context = state.get("context") or {}

        # Conversation history for delta understanding
        recent_chat = []
        for msg in messages[-6:]:
            role = "User" if isinstance(msg, HumanMessage) else "Assistant"
            recent_chat.append(f"{role}: {msg.content}")
        chat_str = "\n".join(recent_chat)

        allowed_spaces = scope.get("allowed_spaces") or []
        current_date = context.get("currentDate") or datetime.utcnow().strftime("%Y-%m-%d")
        timezone = context.get("timezone") or "UTC"

        # Format system prompt safely without KeyError from JSON example braces
        system_content = (
            EXTRACT_DELTA_PROMPT_TEMPLATE
            .replace("{existing_draft_json}", json.dumps(pending_draft) if pending_draft else "None")
            .replace("{context_json}", json.dumps(context))
            .replace("{allowed_spaces_json}", json.dumps(allowed_spaces))
            .replace("{current_date}", str(current_date))
            .replace("{timezone}", str(timezone))
        )

        extracted_data = {}
        try:
            prompt_msgs = [
                SystemMessage(content=system_content),
                HumanMessage(content=f"Conversation:\n{chat_str}\n\nExtract the updated ticket draft:"),
            ]
            response = await llm.ainvoke(prompt_msgs)
            content = response.content.strip()

            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:].strip()

            extracted_data = json.loads(content)
        except Exception as e:
            print(f"[WARN] extract_delta parsing error: {e}")
            extracted_data = dict(pending_draft)

        # Determine if extracted_data is multiple tickets or single ticket
        raw_drafts = []
        if isinstance(extracted_data, list):
            raw_drafts = extracted_data
        elif isinstance(extracted_data, dict):
            if "drafts" in extracted_data and isinstance(extracted_data["drafts"], list):
                raw_drafts = extracted_data["drafts"]
            elif "tickets" in extracted_data and isinstance(extracted_data["tickets"], list):
                raw_drafts = extracted_data["tickets"]
            else:
                raw_drafts = [extracted_data]
        else:
            raw_drafts = [dict(pending_draft)] if (pending_draft and isinstance(pending_draft, dict)) else []

        # If there's an existing single pending_draft and extracted single draft, merge deltas
        if len(raw_drafts) == 1 and pending_draft and isinstance(pending_draft, dict) and "name" in pending_draft:
            merged = dict(pending_draft)
            for k, v in raw_drafts[0].items():
                if v is not None:
                    merged[k] = v
            raw_drafts = [merged]

        clean_drafts: List[Dict[str, Any]] = []
        all_missing_fields: List[str] = []
        needs_fields: List[Dict[str, Any]] = []

        for item in raw_drafts:
            if not isinstance(item, dict):
                continue
            try:
                draft_obj = TicketDraft(**item)
                clean = draft_obj.model_dump(exclude_none=False)
            except Exception:
                clean = item

            if not clean.get("name"):
                all_missing_fields.append("name")

            if not clean.get("ticketSpaceName") and not clean.get("ticketSpaceId"):
                if "ticketSpace" not in all_missing_fields:
                    all_missing_fields.append("ticketSpace")

            clean_drafts.append(clean)

        if "ticketSpace" in all_missing_fields:
            needs_fields.append({
                "field": "ticketSpace",
                "label": "Ticket Space",
                "options": allowed_spaces,
            })

        is_complete = len(all_missing_fields) == 0

        if not is_complete:
            summary_text = (
                "Which ticket space should I place the tickets?"
                if len(clean_drafts) > 1
                else "Which ticket space should I place the ticket?"
            )
        else:
            if len(clean_drafts) > 1:
                summary_text = f"I've prepared {len(clean_drafts)} ticket drafts for you.\nPlease review the details before proceeding."
            else:
                title = clean_drafts[0].get("name") if clean_drafts else "your ticket"
                space = clean_drafts[0].get("ticketSpaceName") if clean_drafts else None
                if space:
                    summary_text = f"I've prepared your ticket '{title}' in the '{space}' space.\nPlease review the details before proceeding."
                else:
                    summary_text = f"Ticket draft ready for '{title}'."

        elapsed = time.time() - start_time
        record_node_telemetry(
            node="automation_extract_delta",
            latency_ms=int(elapsed * 1000),
            status="SUCCESS",
            output_data={
                "draft": clean_drafts[0] if clean_drafts else {},
                "drafts": clean_drafts,
                "is_complete": is_complete,
                "missing_fields": all_missing_fields,
            },
        )

        primary_draft = clean_drafts[0] if clean_drafts else {}
        return {
            "draft": primary_draft,
            "drafts": clean_drafts,
            "pending_draft": clean_drafts if len(clean_drafts) > 1 else primary_draft,
            "is_complete": is_complete,
            "missing_fields": all_missing_fields,
            "needs_fields": needs_fields,
            "summary": summary_text,
            "automation_summary": summary_text,
            "messages": [AIMessage(content=summary_text)],
        }

    return extract_delta_node
