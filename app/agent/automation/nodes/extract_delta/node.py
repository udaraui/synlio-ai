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

        attachments = state.get("attachments") or []
        doc_str = ""
        if attachments:
            from app.utils.document_parser import extract_text_from_attachment
            doc_snippets = []
            for att in attachments:
                fname = att.get("filename") or att.get("name") or "Document"
                content = extract_text_from_attachment(att)
                if content:
                    doc_snippets.append(f"=== ATTACHED DOCUMENT: {fname} ===\n{content}\n=== END OF {fname} ===")
            if doc_snippets:
                doc_str = "\n\n" + "\n\n".join(doc_snippets)

        instruction = "Extract the updated ticket draft (or drafts if multiple items exist):"
        if attachments:
            instruction = (
                "DOCUMENT TICKET EXTRACTION INSTRUCTIONS:\n"
                "The user has uploaded one or more documents above. The document may be in ANY format (CSV, spreadsheet, table, Word doc, PDF, or text notes).\n"
                "1. Read and analyze the entire document content carefully.\n"
                "2. If the document is tabular or contains multiple items (such as CSV rows, task lists, or issue logs), you MUST extract EVERY SINGLE item/row as a distinct ticket draft. For example, if there are 10 items/rows, your JSON array MUST contain all 10 ticket objects!\n"
                "3. Map fields intelligently from whatever headers, columns, or keys are used in the document:\n"
                "   - 'name': The ticket title, summary, issue name, or task subject (MANDATORY).\n"
                "   - 'description': Detailed context, reproduction steps, or notes.\n"
                "   - 'assigneeName': Assigned person or owner if specified.\n"
                "   - 'statusName': Status (e.g. Open, In Progress, Resolved) if specified.\n"
                "   - 'severityName': Priority or severity (e.g. Low, Medium, High, Critical) if specified.\n"
                "   - 'ticketTypeName': Type (e.g. Bug, Feature, Task) if specified or inferred.\n"
                "4. Return ONLY a valid JSON array of ticket objects: [ { \"name\": \"...\", \"description\": \"...\", \"assigneeName\": \"...\", \"severityName\": \"...\", \"statusName\": \"...\", \"ticketTypeName\": \"...\" }, ... ]. Do NOT include conversational filler text."
            )

        extracted_data = {}
        try:
            prompt_msgs = [
                SystemMessage(content=system_content),
                HumanMessage(content=f"Conversation:\n{chat_str}{doc_str}\n\n{instruction}"),
            ]
            response = await llm.ainvoke(prompt_msgs)
            content = response.content.strip()

            # Robust JSON extraction from LLM response
            if "```" in content:
                parts = content.split("```")
                for part in parts:
                    clean_part = part.strip()
                    if clean_part.startswith("json"):
                        clean_part = clean_part[4:].strip()
                    if clean_part.startswith("{") or clean_part.startswith("["):
                        try:
                            extracted_data = json.loads(clean_part)
                            break
                        except Exception:
                            pass

            if not extracted_data:
                idx_brace = content.find("{")
                idx_bracket = content.find("[")
                start_idx = -1
                if idx_brace != -1 and idx_bracket != -1:
                    start_idx = min(idx_brace, idx_bracket)
                elif idx_brace != -1:
                    start_idx = idx_brace
                elif idx_bracket != -1:
                    start_idx = idx_bracket

                if start_idx != -1:
                    try:
                        extracted_data = json.loads(content[start_idx:].strip().rstrip("`"))
                    except Exception:
                        extracted_data = json.loads(content)
                else:
                    extracted_data = json.loads(content)
        except Exception as e:
            print(f"[WARN] extract_delta parsing error: {e}")
            extracted_data = dict(pending_draft) if isinstance(pending_draft, dict) else (pending_draft or {})

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
        # If pending_draft was already a list of multiple tickets:
        elif isinstance(pending_draft, list) and len(pending_draft) > 1:
            if len(raw_drafts) == 1:
                # LLM returned a common update (e.g. ticket space or priority) for the batch
                single_delta = raw_drafts[0]
                updated_list = []
                for existing in pending_draft:
                    merged = dict(existing)
                    for k, v in single_delta.items():
                        if v is not None:
                            # Keep each ticket's distinct name & description
                            if k in ("name", "description") and existing.get(k):
                                continue
                            if k == "assigneeName" and existing.get("assigneeName") and existing.get("assigneeName") != "me":
                                continue
                            merged[k] = v
                    updated_list.append(merged)
                raw_drafts = updated_list
            elif len(raw_drafts) == len(pending_draft):
                updated_list = []
                for i, existing in enumerate(pending_draft):
                    merged = dict(existing)
                    for k, v in raw_drafts[i].items():
                        if v is not None:
                            merged[k] = v
                    updated_list.append(merged)
                raw_drafts = updated_list

        clean_drafts: List[Dict[str, Any]] = []
        all_missing_fields: List[str] = []
        needs_fields: List[Dict[str, Any]] = []

        # Resolve ticket space from active page context if available
        context_space_name = (
            context.get("ticket_space_name")
            or context.get("ticketSpaceName")
            or (context.get("spaceName") if context.get("ticket_space_id") or context.get("ticketSpaceId") else None)
        )
        context_space_id = (
            context.get("ticket_space_id")
            or context.get("ticketSpaceId")
        )

        for item in raw_drafts:
            if not isinstance(item, dict):
                continue
            try:
                draft_obj = TicketDraft(**item)
                clean = draft_obj.model_dump(exclude_none=False)
            except Exception:
                clean = dict(item)

            # Auto-assign space from active page context if not already specified
            if not clean.get("ticketSpaceName") and not clean.get("ticketSpaceId"):
                if context_space_name:
                    clean["ticketSpaceName"] = context_space_name
                if context_space_id:
                    clean["ticketSpaceId"] = context_space_id

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
            if "ticketSpace" in all_missing_fields:
                summary_text = (
                    "Which ticket space should I place the tickets?"
                    if len(clean_drafts) > 1
                    else "Which ticket space should I place the ticket?"
                )
            elif "name" in all_missing_fields:
                summary_text = (
                    "Please provide titles for the tickets."
                    if len(clean_drafts) > 1
                    else "Please provide a title for the ticket."
                )
            else:
                summary_text = f"Please provide the missing information: {', '.join(all_missing_fields)}."
        else:
            if len(clean_drafts) > 1:
                space = clean_drafts[0].get("ticketSpaceName") if clean_drafts else None
                if space:
                    summary_text = f"I've prepared {len(clean_drafts)} ticket drafts for you in '{space}'. Please review the details before proceeding."
                else:
                    summary_text = f"I've prepared {len(clean_drafts)} ticket drafts for you. Please review the details before proceeding."
            else:
                title = clean_drafts[0].get("name") if clean_drafts else "your ticket"
                space = clean_drafts[0].get("ticketSpaceName") if clean_drafts else None
                if space:
                    summary_text = f"I've prepared your ticket '{title}' in the '{space}' space. Please review the details before proceeding."
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
