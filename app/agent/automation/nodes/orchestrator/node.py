import os
import re
import time
import json
import yaml
import httpx
from pathlib import Path
from typing import Dict, Any, Optional
from langchain_core.messages import HumanMessage
from app.agent.automation.state import AutomationState
from app.agent.telemetry import record_node_telemetry

_YAML_PATH = Path(__file__).parent / "prompt.yaml"
try:
    with open(_YAML_PATH, "r", encoding="utf-8") as f:
        _CONFIG = yaml.safe_load(f) or {}
except Exception as e:
    print(f"[WARN] Failed to load automation orchestrator prompt.yaml: {e}")
    _CONFIG = {}

ORCHESTRATOR_SYSTEM_PROMPT = _CONFIG.get("system_prompt", "Classify automation action and entity.")
ORCHESTRATOR_QUESTIONS = _CONFIG.get("questions", {})


def summarize_context_for_jev(context: Optional[Dict[str, Any]]) -> str:
    """Produces a concise, token-efficient 1-line summary of context for Jev decision prompts."""
    if not context or not isinstance(context, dict):
        return "None"

    space_name = context.get("taskSpaceName") or context.get("ticketSpaceName") or context.get("spaceName") or "-"
    space_id = context.get("taskSpaceId") or context.get("ticketSpaceId") or context.get("spaceId") or "-"
    space_prefix = context.get("taskSpacePrefix") or context.get("ticketSpacePrefix") or context.get("prefix") or ""
    page = context.get("page") or context.get("pageScopeKey") or "-"

    tasks = context.get("tasks") or context.get("taskList")
    tickets = context.get("tickets") or context.get("ticketList")

    parts = [f"Space: '{space_name}' (ID: {space_id}, Prefix: {space_prefix})"]
    if isinstance(tasks, list) and tasks:
        parts.append(f"Tasks in view: {len(tasks)}")
    if isinstance(tickets, list) and tickets:
        parts.append(f"Tickets in view: {len(tickets)}")
    if page != "-":
        parts.append(f"Page: {page}")

    return " | ".join(parts)


def make_automation_orchestrator_node(llm):
    """
    Dedicated Automation Orchestrator Node.
    Uses Jev (typesafe/jev-1.14 via OpenRouter Decisions API) to classify:
    1. action_type: CREATE_OR_CLONE (extract_delta) vs MUTATE_EXISTING (extract_mutation)
    2. entity_type: task (Project/Task/Subtask) vs ticket (Ticket/Bug/Issue)
    """
    async def automation_orchestrator_node(state: AutomationState) -> Dict[str, Any]:
        start_time = time.time()
        messages = state.get("messages", [])
        context = state.get("context") or {}
        pending_draft = state.get("pending_draft")

        # 1. Extract latest user message
        user_msg = ""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage) or getattr(msg, "type", "") in ("human", "user"):
                user_msg = str(getattr(msg, "content", msg))
                break
        user_msg_clean = user_msg.strip().lower()

        # 2. Extract work item codes (Ticket: direct numbers e.g. PTS-10 vs Task: letter segments e.g. PTS-P7, PTS-P7-T1)
        ticket_codes = [c.strip().upper() for c in re.findall(r'\b[A-Za-z0-9]+-\d+\b', user_msg) if not re.search(r'-P\d+', c, re.IGNORECASE)]
        task_codes = [c.strip().upper() for c in re.findall(r'\b[A-Za-z0-9]+-[A-Za-z]+[A-Za-z0-9_-]*\b', user_msg)]
        has_any_code = bool(ticket_codes or task_codes)

        # 3. Keyword scanning
        creation_verbs = [
            "create", "draft", "make a ticket", "make ticket", "new ticket",
            "add ticket", "file a bug", "file bug", "file ticket", "file a ticket",
            "raise ticket", "raise a ticket", "log ticket", "log a ticket",
            "clone", "duplicate", "copy", "same as", "create a task", "new task",
            "add task", "new project", "add project", "create project", "create a project"
        ]
        has_creation_intent = any(cv in user_msg_clean for cv in creation_verbs)

        mutation_verbs = [
            "move", "change status", "change the status", "change to", "update status", "update the status",
            "update ticket", "update task", "update", "assign", "reassign", "set severity", "set status",
            "set priority", "rename", "mark as", "mark ticket", "mark task", "close ticket", "close task",
            "resolve", "resolve ticket", "resolve task", "close", "reopen", "make", "make all",
            "set all", "all tickets", "all tasks", "tickets assigned", "tasks assigned", "tickets for", "tasks for",
            "move all", "reassign all", "inprogress", "inprogresss"
        ]
        has_mutation_intent = any(v in user_msg_clean for v in mutation_verbs)

        model_name = getattr(llm, "model_name", None) or os.getenv("ORCHESTRATOR_MODEL", "typesafe/jev-1.14")
        is_decisions_model = "jev" in model_name.lower() or "decisions" in model_name.lower()
        route_decision: Optional[str] = None
        entity_decision: Optional[str] = None
        decision_confidence = 1.0
        reason = ""

        # --- Fast-path checks (Deterministic) ---
        if state.get("mutation_payload") or state.get("automation_action") == "UPDATE_TICKET":
            route_decision = "MUTATE_EXISTING"
            reason = "State explicitly marked with mutation_payload or UPDATE_TICKET"
        elif pending_draft and not has_mutation_intent:
            route_decision = "CREATE_OR_CLONE"
            reason = "Pending draft present; user answering disambiguation or continuing draft"

        # --- Jev Decision Classifier (typesafe/jev-1.14 via OpenRouter Decisions API) ---
        if not route_decision and is_decisions_model and ORCHESTRATOR_QUESTIONS:
            raw_key = getattr(llm, "openai_api_key", None)
            if hasattr(raw_key, "get_secret_value"):
                api_key = raw_key.get_secret_value()
            else:
                api_key = str(raw_key) if raw_key else None
            if not api_key or api_key.startswith("***"):
                api_key = os.getenv("JEV_API_KEY")

            if api_key:
                try:
                    context_summary = summarize_context_for_jev(context)
                    payload = {
                        "model": model_name,
                        "state": f"User: {user_msg}\nContext: {context_summary}\n",
                        "questions": ORCHESTRATOR_QUESTIONS,
                    }
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            "https://openrouter.ai/api/alpha/decisions",
                            headers={
                                "Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json",
                            },
                            json=payload,
                            timeout=15.0,
                        )
                    if resp.status_code == 200:
                        data = resp.json()
                        answers = data.get("answers", {})
                        jev_action = answers.get("action_type", {}).get("choice")
                        jev_entity = answers.get("entity_type", {}).get("choice")
                        jev_probs = answers.get("action_type", {}).get("probabilities", {})
                        if jev_action in ("CREATE_OR_CLONE", "MUTATE_EXISTING"):
                            route_decision = jev_action
                            decision_confidence = float(jev_probs.get(jev_action, 0.95))
                            reason = f"Jev Decision API classified as {jev_action}"
                        if jev_entity in ("TASK", "TICKET"):
                            entity_decision = jev_entity.lower()
                except Exception as e:
                    print(f"[WARN] Jev automation router failed, using fallback heuristics: {e}")

        # --- Fallback Heuristics if Jev was not invoked or failed ---
        if not route_decision:
            if has_creation_intent and not (has_any_code and has_mutation_intent):
                route_decision = "CREATE_OR_CLONE"
                reason = "Heuristic: Creation intent matched without conflicting mutation verbs"
            elif has_mutation_intent:
                route_decision = "MUTATE_EXISTING"
                reason = "Heuristic: Explicit mutation verbs present"
            elif has_any_code:
                route_decision = "MUTATE_EXISTING"
                reason = "Heuristic: Referenced existing work item code without creation verbs"
            else:
                route_decision = "CREATE_OR_CLONE"
                reason = "Heuristic default fallback"

        # --- Multi-Layer Cascade for entity_type ---
        mentioned_items = context.get("mentionedItems") or []
        agent = state.get("agent") or context.get("agent")

        # 1. Direct User Keyword Check (Highest Priority)
        user_mentions_ticket = bool(re.search(r'\b(?:tickets?|bugs?|issues?|defects?|incidents?)\b', user_msg_clean))
        user_mentions_task = bool(re.search(r'\b(?:tasks?|subtasks?|projects?|initiatives?|milestones?)\b', user_msg_clean))

        if user_mentions_ticket and not user_mentions_task:
            resolved_entity = "ticket"
        elif user_mentions_task and not user_mentions_ticket:
            resolved_entity = "task"
        elif task_codes and not ticket_codes:
            resolved_entity = "task"
        elif ticket_codes and not task_codes:
            resolved_entity = "ticket"
        elif mentioned_items:
            m_type = (mentioned_items[0].get("type") or "").lower()
            resolved_entity = "task" if m_type in ("task", "project") else "ticket"
        elif agent in ("ticket_analytics", "ticket"):
            resolved_entity = "ticket"
        elif agent in ("project_analytics", "task", "project"):
            resolved_entity = "task"
        elif entity_decision:
            resolved_entity = entity_decision
        elif context.get("taskSpaceId") or context.get("task_space_id"):
            resolved_entity = "task"
        elif context.get("ticketSpaceId") or context.get("ticket_space_id"):
            resolved_entity = "ticket"
        else:
            resolved_entity = "task" if any(w in user_msg_clean for w in ["task", "project", "initiative", "folder"]) else "ticket"

        latency_ms = int((time.time() - start_time) * 1000)

        # Standardized console log
        print(f"[AUTOMATION_ROUTER] Route chosen: {route_decision} | Latency: {latency_ms}ms | Model: {model_name}")
        print(f"  Entity:     {resolved_entity.upper()}")
        print(f"  Confidence: {decision_confidence:.2f}")
        print(f"  Reason:     {reason}")

        record_node_telemetry(
            node="automation_router",
            latency_ms=latency_ms,
            status="SUCCESS",
            model=model_name,
            output_data={
                "route": route_decision,
                "entityType": resolved_entity,
                "confidence": decision_confidence,
                "reason": reason,
            },
            extra_summary=f"Route: {route_decision} | Entity: {resolved_entity.upper()} (Confidence: {decision_confidence:.2f})",
        )

        return {
            "automation_action": route_decision,
            "entity_type": resolved_entity,
        }

    return automation_orchestrator_node
