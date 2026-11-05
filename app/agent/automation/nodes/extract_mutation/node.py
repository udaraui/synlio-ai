import re
import time
import json
import yaml
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from app.agent.automation.state import AutomationState
from app.agent.automation.schema import MutationPayload, TicketMutationChanges
from app.agent.telemetry import record_node_telemetry

_YAML_PATH = Path(__file__).parent / "prompt.yaml"
try:
    with open(_YAML_PATH, "r", encoding="utf-8") as f:
        _CONFIG = yaml.safe_load(f) or {}
except Exception as e:
    print(f"[WARN] Failed to load extract_mutation prompt.yaml: {e}")
    _CONFIG = {}

EXTRACT_MUTATION_PROMPT_TEMPLATE = _CONFIG.get("system_prompt", "Extract ticket mutation parameters.")


def make_extract_mutation_node(llm):
    """
    Creates the extract_mutation node.
    Extracts target ticket codes and requested field changes (status, assignee, severity, type, title, description, etc.)
    and outputs a structured mutation_payload without blocking for user confirmation.
    """
    async def extract_mutation_node(state: AutomationState) -> Dict[str, Any]:
        start_time = time.time()
        messages = state.get("messages", [])
        scope = state.get("scope") or {}
        context = state.get("context") or {}

        # Conversation history (last 4 messages for context)
        recent_chat = []
        user_msg = ""
        for msg in reversed(messages):
            if not user_msg and (isinstance(msg, HumanMessage) or getattr(msg, "type", "") in ("human", "user")):
                user_msg = str(getattr(msg, "content", msg))
        for msg in messages[-4:]:
            role = "User" if isinstance(msg, HumanMessage) else "Assistant"
            recent_chat.append(f"{role}: {msg.content}")
        chat_str = "\n".join(recent_chat)

        allowed_spaces = scope.get("allowed_spaces") or []
        current_date = context.get("currentDate") or datetime.utcnow().strftime("%Y-%m-%d")

        system_content = (
            EXTRACT_MUTATION_PROMPT_TEMPLATE
            .replace("{context_json}", json.dumps(context))
            .replace("{allowed_spaces_json}", json.dumps(allowed_spaces))
            .replace("{current_date}", str(current_date))
        )

        extracted_data = {}
        llm_error = None
        try:
            prompt_msgs = [
                SystemMessage(content=system_content),
                HumanMessage(content=f"Conversation:\n{chat_str}\n\nExtract the requested ticket or task mutation:"),
            ]
            response = await llm.ainvoke(prompt_msgs)
            content = response.content.strip()

            # Clean JSON markdown if wrapped
            if "```" in content:
                parts = content.split("```")
                for part in parts:
                    clean_part = part.strip()
                    if clean_part.startswith("json"):
                        clean_part = clean_part[4:].strip()
                    try:
                        extracted_data = json.loads(clean_part)
                        break
                    except Exception:
                        continue
            else:
                extracted_data = json.loads(content)
        except Exception as e:
            print(f"[ERROR] extract_mutation LLM call failed: {e}")
            llm_error = str(e)
            extracted_data = {}

        # Deterministic regex extraction of ticket codes
        # 1. If user explicitly specified ticket code(s) in CURRENT message, use ONLY them!
        user_msg_codes = [c.strip().upper() for c in re.findall(r'\b[A-Za-z]{2,10}-\d+\b', user_msg)]
        if user_msg_codes:
            targets = []
            seen_targets = set()
            for c in user_msg_codes:
                if c not in seen_targets:
                    seen_targets.add(c)
                    targets.append(c)
        else:
            targets = [str(t).strip().upper() for t in (extracted_data.get("targets") or []) if str(t).strip()]
            if not targets:
                # Only check recent history if current user message has NO ticket codes
                history_codes = [c.strip().upper() for c in re.findall(r'\b[A-Za-z]{2,10}-\d+\b', chat_str)]
                if history_codes:
                    targets = [history_codes[-1]]

        # Fallback to context ticketCode if still no target was parsed from text
        if not targets:
            ctx_code = context.get("ticketCode") or context.get("code") or context.get("ticket_code")
            if ctx_code:
                targets = [str(ctx_code).strip().upper()]

        raw_changes = extracted_data.get("changes") or {}
        clean_changes = {}
        for k, v in raw_changes.items():
            if v is not None and str(v).strip() != "" and str(v).lower() != "null":
                clean_changes[k] = v

        # Deterministic extraction of target status if missed by LLM
        user_text_lower = f"{user_msg} {chat_str}".lower()
        if not clean_changes.get("statusName") and not clean_changes.get("status"):
            status_mappings = [
                (r'\b(?:to|as|into|set to)\s+(?:status\s+)?in\s*progress\b|\bmove\b.*\bto in progress\b', "In Progress"),
                (r'\b(?:to|as|into|set to)\s+(?:status\s+)?done\b|\bmark\s+(?:as\s+)?done\b|\bcompleted?\b', "Done"),
                (r'\b(?:to|as|into|set to)\s+(?:status\s+)?in\s*review\b', "In Review"),
                (r'\b(?:to|as|into|set to)\s+(?:status\s+)?to\s*start\b|\bto-start\b', "To Start"),
                (r'\b(?:to|as|into|set to)\s+(?:status\s+)?to\s*do\b|\btodo\b', "To Do"),
                (r'\b(?:to|as|into|set to)\s+(?:status\s+)?closed?\b|\bclose ticket\b', "Closed"),
                (r'\b(?:to|as|into|set to)\s+(?:status\s+)?open\b|\breopen\b', "Open"),
                (r'\b(?:to|as|into|set to)\s+(?:status\s+)?resolved\b|\bresolve\b', "Resolved"),
            ]
            for pattern, status_val in status_mappings:
                if re.search(pattern, user_text_lower):
                    clean_changes["statusName"] = status_val
                    break

        if not clean_changes.get("severityName") and not clean_changes.get("severity"):
            for sev in ["critical", "high", "medium", "low"]:
                if re.search(rf'\b(?:severity|priority)\b.*?\b(?:to|as|=)?\s*{sev}\b', user_text_lower) or re.search(rf'\b(?:to|as|mark as|set to)\s+{sev}\b', user_text_lower):
                    clean_changes["severityName"] = sev.capitalize()
                    break

        filter_data = extracted_data.get("filter") or {}
        # Guard: if assignee was extracted in changes but user query indicates filter (e.g. "assigned to X"), move to filter
        if "assigned to" in user_text_lower or "tickets for" in user_text_lower:
            if clean_changes.get("assigneeName") and not filter_data.get("assignee"):
                filter_data["assignee"] = clean_changes.pop("assigneeName")

        summary = extracted_data.get("summary")
        # Overwrite summary if empty or if LLM mistakenly asked for ticket code despite targets being resolved
        if not summary or (targets and "Which ticket would you like to update" in summary):
            if targets:
                status_val = clean_changes.get("statusName") or clean_changes.get("status")
                target_str = ", ".join(targets)
                if status_val:
                    summary = f"I've updated **{target_str}** to **{status_val}**."
                else:
                    changed_items = [f"{k}: {v}" for k, v in clean_changes.items()]
                    summary = f"I've updated **{target_str}**" + (f" ({', '.join(changed_items)})." if changed_items else ".")
            elif filter_data.get("assignee"):
                summary = f"Which ticket(s) assigned to **{filter_data['assignee']}** would you like to update?"
            else:
                summary = "Which ticket would you like to update? Please provide the ticket code (e.g. PTS-12)."

        mutation_payload = {
            "action": extracted_data.get("action", "UPDATE_TICKET") or "UPDATE_TICKET",
            "status": "READY" if targets else "NEEDS_INPUT",
            "entityType": extracted_data.get("entityType", "ticket"),
            "targets": targets,
            "filter": filter_data if filter_data else None,
            "changes": clean_changes,
            "options": [],
            "summary": summary,
            "warnings": [],
        }

        elapsed = time.time() - start_time
        if llm_error:
            node_status = "FALLBACK" if targets else "ERROR"
        else:
            node_status = "SUCCESS"

        record_node_telemetry(
            node="automation_extract_mutation",
            latency_ms=int(elapsed * 1000),
            status=node_status,
            error_message=llm_error,
            output_data={
                "targets": targets,
                "status": mutation_payload["status"],
                "has_filter": bool(filter_data),
                "changes_keys": list(clean_changes.keys()),
                "summary": summary,
                "llm_error": llm_error,
            },
        )

        return {
            "mutation_payload": mutation_payload,
            "automation_action": mutation_payload["action"],
            "summary": summary,
            "automation_summary": summary,
            "messages": [AIMessage(content=summary)],
        }

    return extract_mutation_node
