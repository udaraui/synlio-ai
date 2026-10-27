import os
import time
import json
import yaml
from pathlib import Path
from typing import Dict, Any
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from app.agent.state import AgentState
from app.agent.telemetry import record_node_telemetry

_YAML_PATH = Path(__file__).parent / "prompt.yaml"
try:
    with open(_YAML_PATH, "r", encoding="utf-8") as f:
        _CONFIG = yaml.safe_load(f) or {}
except Exception as e:
    print(f"[WARN] Failed to load dispatcher prompt.yaml from {_YAML_PATH}: {e}")
    _CONFIG = {}

DISPATCHER_SYSTEM_PROMPT = _CONFIG.get("system_prompt", "Classify intent as 'automation', 'analytics', or 'general_chat'.")

CANCEL_KEYWORDS = {"cancel", "never mind", "nevermind", "forget it", "stop", "discard"}

UNAVAILABLE_MSG = (
    "Project Analytics, Ticket Analytics, and Project Automation are temporarily unavailable while system updates are underway. "
    "Only Resource Analytics and Ticket Automation are currently available."
)

FULL_SCREEN_AUTOMATION_MSG = (
    "Automations cannot be performed here.\n"
    "Please navigate to the appropriate workspace page and use the Co-Pilot to proceed."
)

TASK_AUTOMATION_MSG = (
    "Task Automation is currently not supported in the Ticket side panel. Only Ticket Automation is currently available here. To file a ticket, bug, or issue, please let me know what ticket you would like to create."
)


def make_dispatcher_node(llm):
    """
    Creates the Dispatcher Node.
    Runs immediately after gate_keeper (when safety is verified).
    Determines whether to route to 'automation' or 'analytics'.
    """
    async def dispatcher_node(state: AgentState) -> Dict[str, Any]:
        start_time = time.time()
        messages = state.get("messages", [])
        pending_draft = state.get("pending_draft")
        scope = state.get("scope") or {}
        context = state.get("context") or {}

        # Resolve UI screen mode ("panel" vs "full")
        chat_mode = (
            state.get("chat_mode")
            or (context.get("chatMode") if isinstance(context, dict) else None)
            or (context.get("mode") if isinstance(context, dict) else None)
            or "panel"
        )

        # Extract latest user message
        user_msg = ""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                user_msg = msg.content
                break

        user_msg_clean = user_msg.strip().lower()

        # -------------------------------------------------------------
        # 1. Deterministic Continuation Check (pending_draft exists)
        # -------------------------------------------------------------
        if pending_draft:
            if chat_mode == "full":
                return {
                    "intent": "general_chat",
                    "pending_draft": None,
                    "draft": None,
                    "needs_fields": None,
                    "messages": [AIMessage(content=FULL_SCREEN_AUTOMATION_MSG)],
                }

            # Check for user cancellation escape hatch
            if any(k in user_msg_clean for k in CANCEL_KEYWORDS):
                elapsed = time.time() - start_time
                record_node_telemetry(
                    node="dispatcher",
                    latency_ms=int(elapsed * 1000),
                    status="DRAFT_CANCELLED",
                    output_data={"intent": "general_chat", "action": "cancel_draft"},
                )
                return {
                    "intent": "general_chat",
                    "pending_draft": None,
                    "draft": None,
                    "needs_fields": None,
                    "messages": [AIMessage(content="I've cancelled that draft. What else can I help you with?")],
                }

            elapsed = time.time() - start_time
            record_node_telemetry(
                node="dispatcher",
                latency_ms=int(elapsed * 1000),
                status="CONTINUATION_BYPASS",
                output_data={"intent": "automation", "reason": "pending_draft_present"},
            )
            return {"intent": "automation"}

        # -------------------------------------------------------------
        # 2. Check Feature Flag
        # -------------------------------------------------------------
        automation_enabled = os.getenv("AUTOMATION_ENABLED", "true").lower() in ("true", "1", "yes")
        if not automation_enabled:
            return {"intent": "analytics"}

        # -------------------------------------------------------------
        # 3. Categorical Intent Classification via LLM
        # -------------------------------------------------------------
        intent = "analytics"
        try:
            prompt_msgs = [
                SystemMessage(content=DISPATCHER_SYSTEM_PROMPT),
                HumanMessage(content=f"Classify this request:\n\"{user_msg}\""),
            ]
            response = await llm.ainvoke(prompt_msgs)
            content = response.content.strip()

            # Clean JSON markdown if wrapped
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:].strip()

            parsed = json.loads(content)
            intent = parsed.get("intent", "analytics").lower()
            if intent not in ("automation", "analytics", "general_chat"):
                intent = "analytics"

        except Exception as e:
            # Fallback heuristic: check for action verbs
            action_verbs = ("create", "add", "make", "draft", "file a bug", "new ticket", "open a ticket", "new task")
            if any(v in user_msg_clean for v in action_verbs) and "report" not in user_msg_clean:
                intent = "automation"
            else:
                intent = "analytics"

        # -------------------------------------------------------------
        # 4. Handle Automation Guardrails & Domain Restrictions
        # -------------------------------------------------------------
        if intent == "automation":
            # Block automation in full-screen chat; only allow in side panel
            if chat_mode == "full":
                elapsed = time.time() - start_time
                record_node_telemetry(
                    node="dispatcher",
                    latency_ms=int(elapsed * 1000),
                    status="FULL_CHAT_AUTOMATION_BLOCKED",
                    output_data={"intent": "refusal", "reason": "full_chat_mode"},
                )
                return {
                    "intent": "general_chat",
                    "messages": [AIMessage(content=FULL_SCREEN_AUTOMATION_MSG)],
                }

            has_ticket_keyword = any(k in user_msg_clean for k in ("ticket", "bug", "issue"))

            # Check if user attempts Task Automation in the Ticket side panel
            is_task_request = any(
                phrase in user_msg_clean
                for phrase in [
                    "task automation",
                    "create a task",
                    "create task",
                    "add a task",
                    "add task",
                    "new task",
                    "log a task",
                    "log task",
                    "file a task",
                    "draft a task",
                    "make a task",
                    "task space",
                    "root task",
                ]
            )
            if is_task_request and not has_ticket_keyword:
                elapsed = time.time() - start_time
                record_node_telemetry(
                    node="dispatcher",
                    latency_ms=int(elapsed * 1000),
                    status="TASK_AUTOMATION_NOT_SUPPORTED",
                    output_data={"intent": "refusal", "reason": "task_automation_in_ticket_panel"},
                )
                return {
                    "intent": "general_chat",
                    "messages": [AIMessage(content=TASK_AUTOMATION_MSG)],
                }

            # Check if user attempts Project Automation (temporarily unavailable)
            is_project_request = any(
                phrase in user_msg_clean
                for phrase in [
                    "project automation",
                    "create a project",
                    "create project",
                    "add a project",
                    "add project",
                    "new project",
                    "make a project",
                    "project milestone",
                    "create a sprint",
                    "new sprint",
                ]
            )
            if is_project_request and not has_ticket_keyword:
                elapsed = time.time() - start_time
                record_node_telemetry(
                    node="dispatcher",
                    latency_ms=int(elapsed * 1000),
                    status="UNAVAILABLE_DOMAIN",
                    output_data={"intent": "refusal", "reason": "project_automation_unavailable"},
                    error_message=UNAVAILABLE_MSG,
                )
                return {
                    "intent": "general_chat",
                    "messages": [AIMessage(content=UNAVAILABLE_MSG)],
                }

            # Check ticket creation permissions (Permission 103)
            if scope.get("can_create_ticket") is False:
                elapsed = time.time() - start_time
                record_node_telemetry(
                    node="dispatcher",
                    latency_ms=int(elapsed * 1000),
                    status="PERMISSION_DENIED",
                    output_data={"intent": "refusal", "permission": "103_missing"},
                )
                return {
                    "intent": "general_chat",
                    "messages": [
                        AIMessage(content="You do not have permission to create tickets. Please contact your workspace administrator to request ticket creation access.")
                    ],
                }

            return {"intent": "automation"}

        elapsed = time.time() - start_time
        record_node_telemetry(
            node="dispatcher",
            latency_ms=int(elapsed * 1000),
            status="SUCCESS",
            output_data={"intent": intent, "userQuery": user_msg},
        )
        return {"intent": intent}

    return dispatcher_node
