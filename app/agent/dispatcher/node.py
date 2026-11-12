import os
import re
import time
import json
import yaml
import httpx
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
DISPATCHER_QUESTIONS = _CONFIG.get("questions", {})

CANCEL_KEYWORDS = {"cancel", "never mind", "nevermind", "forget it", "stop", "discard"}

# Greeting/help queries that should always return a friendly static response
GREETING_PATTERNS = re.compile(
    r'^\s*('
    r'hi+|hello+|hey+|howdy|greetings|'
    r'what\s+can\s+you\s+(do|help\s+me\s+with|help\s+with)|'
    r'how\s+can\s+you\s+help|'
    r'what\s+(are\s+you|do\s+you\s+do)|'
    r'tell\s+me\s+about\s+yourself|'
    r'help\s*$|'
    r'what\'?s?\s+your\s+purpose|'
    r'introduce\s+yourself|'
    r'capabilities|what\s+can\s+i\s+(ask|do)|'
    r'(good\s+)?(morning|afternoon|evening)'
    r')\s*[!?.]*\s*$',
    re.IGNORECASE,
)

GREETING_RESPONSE_TICKET = (
    "Hi! I'm your **Ticket Analyst** — here's what I can help you with:\n\n"
    "**🔍 Analytics & Queries**\n"
    "- Show open/closed tickets in a space\n"
    "- Filter tickets by severity, assignee, type, or queue\n"
    "- Ticket workload and SLA summaries\n\n"
    "**⚡ Automation**\n"
    "- Create, update, or bulk-update tickets\n"
    "- Change status, severity, assignee, type, or queue on tickets\n"
    "- Assign high-priority tickets to a team member\n\n"
    "Just type what you'd like to do and I'll take it from there!"
)

GREETING_RESPONSE_PROJECT = (
    "Hi! I'm your **Project Analyst** — here's what I can help you with:\n\n"
    "**🔍 Analytics & Queries**\n"
    "- Show tasks, milestones, or projects in a space\n"
    "- Filter by status, severity, assignee, or hierarchy level\n"
    "- Workload and progress summaries\n\n"
    "**⚡ Automation**\n"
    "- Create, update, or bulk-update tasks\n"
    "- Change status, severity, assignee, or dates on tasks\n"
    "- Move all tasks of a certain priority to a new status\n\n"
    "Just type what you'd like to do and I'll take it from there!"
)

GREETING_RESPONSE_GENERAL = (
    "Hi! I'm your **Co-Pilot** — here's what I can help you with:\n\n"
    "**🔍 Analytics & Queries**\n"
    "- Show open/closed tickets or tasks in a space\n"
    "- Filter by status, severity, assignee, type, or queue\n"
    "- Workload, SLA, and progress summaries\n\n"
    "**⚡ Automation**\n"
    "- Create, update, or bulk-update tickets and tasks\n"
    "- Change status, severity, assignee, type, dates, and more\n"
    "- Assign high-priority tickets or move all tasks to a new status\n\n"
    "Just type what you'd like to do and I'll take it from there!"
)

UNAVAILABLE_MSG = (
    "Project Analytics, Ticket Analytics, and Project Automation are temporarily unavailable while system updates are underway. "
    "Only Resource Analytics and Ticket Automation are currently available."
)

FULL_SCREEN_AUTOMATION_MSG = (
    "Automations cannot be performed here.\n"
    "Please navigate to the ypur workspace page and use the Co-Pilot to proceed."
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
        # 0. Greeting / Help Intent — return friendly static response immediately
        # Prevents "Execution completed with no data" for conversational queries
        # -------------------------------------------------------------
        if GREETING_PATTERNS.match(user_msg.strip()) and not pending_draft:
            agent_type_greet = state.get("agent") or (context.get("agent") if isinstance(context, dict) else None)
            if agent_type_greet:
                agent_type_greet = str(agent_type_greet).lower().strip()
            if agent_type_greet in ("ticket_analytics", "ticket"):
                greeting_reply = GREETING_RESPONSE_TICKET
            elif agent_type_greet in ("project_analytics", "project", "task"):
                greeting_reply = GREETING_RESPONSE_PROJECT
            else:
                greeting_reply = GREETING_RESPONSE_GENERAL
            elapsed = time.time() - start_time
            record_node_telemetry(
                node="dispatcher",
                latency_ms=int(elapsed * 1000),
                status="GREETING_HANDLED",
                output_data={"intent": "general_chat", "reason": "greeting_or_help_query"},
            )
            return {
                "intent": "general_chat",
                "intent_confidence": 1.0,
                "messages": [AIMessage(content=greeting_reply)],
            }

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

            # If user message contains explicit mutation actions WITH a ticket code, or explicit move/status verbs, override pending_draft
            has_explicit_ticket_code = bool(re.search(r'\b[A-Za-z]{2,10}-\d+\b', user_msg))
            is_mutation_override = has_explicit_ticket_code or any(
                v in user_msg_clean
                for v in (
                    "move ticket", "change status", "update status", "update ticket", "close ticket", "reopen ticket"
                )
            )

            if is_mutation_override:
                elapsed = time.time() - start_time
                record_node_telemetry(
                    node="dispatcher",
                    latency_ms=int(elapsed * 1000),
                    status="MUTATION_OVERRIDE",
                    output_data={"intent": "automation", "reason": "mutation_overrides_pending_draft"},
                )
                return {
                    "intent": "automation",
                    "pending_draft": None,
                    "draft": None,
                    "needs_fields": None,
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
        # 1b. Document Attachment Check in Side Panel
        # -------------------------------------------------------------
        attachments = state.get("attachments") or []
        if attachments and chat_mode == "panel":
            if not any(k in user_msg_clean for k in ("chart", "graph", "trend", "metric", "statistic")):
                elapsed = time.time() - start_time
                record_node_telemetry(
                    node="dispatcher",
                    latency_ms=int(elapsed * 1000),
                    status="DOCUMENT_AUTOMATION_ROUTE",
                    output_data={"intent": "automation", "reason": "attachment_present"},
                )
                return {"intent": "automation"}

        # -------------------------------------------------------------
        # 1c. Check if user is replying with a bare ticket or task code
        # -------------------------------------------------------------
        is_bare_code = bool(re.match(r'^[A-Za-z0-9]+-(?:[A-Za-z]+\d+|\d+)$', user_msg.strip(), re.IGNORECASE))
        if is_bare_code and len(messages) >= 2:
            prev_ai_msg = messages[-2]
            prev_content = str(getattr(prev_ai_msg, "content", "")).lower()
            if any(w in prev_content for w in ("which ticket", "which task", "which item", "provide the ticket code", "provide the task code", "assign to", "update")):
                elapsed = time.time() - start_time
                record_node_telemetry(
                    node="dispatcher",
                    latency_ms=int(elapsed * 1000),
                    status="CODE_REPLY_AUTOMATION_ROUTE",
                    output_data={"intent": "automation", "reason": "code_reply_to_question"},
                )
                return {"intent": "automation"}

        # -------------------------------------------------------------
        # 1d. Domain Guardrails (Option 2: Strict Domain Boundary)
        # -------------------------------------------------------------
        agent_type = state.get("agent") or (context.get("agent") if isinstance(context, dict) else None)
        if agent_type:
            agent_type = str(agent_type).lower().strip()

        ticket_keywords = {
            "ticket", "tickets", "bug", "bugs", "defect", "defects", "issue", "issues", "incident", "incidents"
        }
        task_keywords = {
            "task", "tasks", "project", "projects", "milestone", "milestones", "subtask", "subtasks", "initiative", "initiatives"
        }

        # Check for explicit word presence using word boundaries
        has_ticket_kw = any(bool(re.search(rf'\b{kw}\b', user_msg_clean)) for kw in ticket_keywords)
        has_task_kw = any(bool(re.search(rf'\b{kw}\b', user_msg_clean)) for kw in task_keywords)

        # Check for explicit code patterns
        # Ticket code: Prefix hyphen and ONLY digits (e.g. PTS-23, TSU-5)
        # Task code: Prefix hyphen, level letter prefix, then digits (e.g. PTS-P2, TSU-P10, PRJ-T3)
        # IMPORTANT: The alpha segment MUST be followed by at least one digit to count as a task code.
        # This prevents hyphenated words like "MEDIUM-priority" or "MEDIMU-priority" from being
        # misidentified as task codes (they have no trailing digit after the alpha letters).
        has_ticket_code = bool(re.search(r'\b[A-Za-z0-9]+-\d+\b', user_msg) and not re.search(r'\b[A-Za-z0-9]+-[A-Za-z]+\d+\b', user_msg))
        has_task_code = bool(re.search(r'\b[A-Za-z0-9]+-[A-Za-z]+\d+\b', user_msg))

        is_pure_ticket_query = (has_ticket_kw or has_ticket_code) and not (has_task_kw or has_task_code)
        is_pure_task_query = (has_task_kw or has_task_code) and not (has_ticket_kw or has_ticket_code)

        if agent_type in ("project_analytics", "project", "task") and is_pure_ticket_query:
            elapsed = time.time() - start_time
            redirect_msg = (
                "I am your **Project Analyst**, specialized in tasks, milestones, and projects. "
                "To manage or analyze tickets and bugs, please switch to the **Ticket Analyst** from the agent selector "
                "or navigate to the Ticket Management page."
            )
            record_node_telemetry(
                node="dispatcher",
                latency_ms=int(elapsed * 1000),
                status="DOMAIN_GUARDRAIL_REDIRECT",
                output_data={"agent": agent_type, "redirect_to": "ticket_analytics", "query": user_msg},
            )
            return {
                "intent": "general_chat",
                "intent_confidence": 1.0,
                "messages": [AIMessage(content=redirect_msg)],
            }

        if agent_type in ("ticket_analytics", "ticket") and is_pure_task_query:
            elapsed = time.time() - start_time
            redirect_msg = (
                "I am your **Ticket Analyst**, specialized in tickets, bugs, and issues. "
                "To manage or analyze tasks and projects, please switch to the **Project Analyst** from the agent selector "
                "or navigate to the Task Management page."
            )
            record_node_telemetry(
                node="dispatcher",
                latency_ms=int(elapsed * 1000),
                status="DOMAIN_GUARDRAIL_REDIRECT",
                output_data={"agent": agent_type, "redirect_to": "project_analytics", "query": user_msg},
            )
            return {
                "intent": "general_chat",
                "intent_confidence": 1.0,
                "messages": [AIMessage(content=redirect_msg)],
            }

        # -------------------------------------------------------------
        # 2. Check Feature Flag
        # -------------------------------------------------------------
        automation_enabled = os.getenv("AUTOMATION_ENABLED", "true").lower() in ("true", "1", "yes")
        if not automation_enabled:
            return {"intent": "analytics"}

        # -------------------------------------------------------------
        # 3. Categorical Intent Classification via LLM (Jev Decisions or Chat)
        # -------------------------------------------------------------
        intent = "analytics"
        confidence = 1.0
        model_name = getattr(llm, "model_name", None) or os.getenv("ORCHESTRATOR_MODEL", "typesafe/jev-1.14")
        is_decisions_model = "jev" in model_name.lower() or "decisions" in model_name.lower()

        try:
            if is_decisions_model and DISPATCHER_QUESTIONS:
                raw_key = getattr(llm, "openai_api_key", None)
                if hasattr(raw_key, "get_secret_value"):
                    api_key = raw_key.get_secret_value()
                else:
                    api_key = str(raw_key) if raw_key else None
                if not api_key or api_key.startswith("***"):
                    api_key = os.getenv("JEV_API_KEY")

                payload = {
                    "model": model_name,
                    "state": f"User: {user_msg}\n",
                    "questions": DISPATCHER_QUESTIONS,
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
                    decision = data.get("answers", {}).get("intent", {}).get("choice")
                    probs = data.get("answers", {}).get("intent", {}).get("probabilities", {})
                    if decision in ("automation", "analytics", "general_chat"):
                        intent = decision
                        confidence = float(probs.get(decision, 0.95))
                else:
                    raise Exception(f"Decisions API returned status {resp.status_code}: {resp.text}")
            else:
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
                confidence = float(parsed.get("confidence", 0.90))
                if intent not in ("automation", "analytics", "general_chat"):
                    intent = "analytics"

        except Exception as e:
            # Fallback heuristic if LLM call encounters network/API error
            action_verbs = (
                "create", "add", "make", "draft", "file", "log", "new ticket", "open a ticket", "new task",
                "move", "change", "update", "assign", "reassign", "set", "rename", "mark", "close", "reopen",
                "priority", "severity", "status", "queue"
            )
            if any(v in user_msg_clean for v in action_verbs) and "report" not in user_msg_clean:
                intent = "automation"
            else:
                intent = "analytics"
            confidence = 0.80

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
                    "intent_confidence": confidence,
                    "messages": [AIMessage(content=FULL_SCREEN_AUTOMATION_MSG)],
                }

            # Check ticket creation permissions (Permission 103) ONLY for ticket creation requests
            is_ticket_request = any(k in user_msg_clean for k in ("ticket", "bug", "issue"))
            if is_ticket_request and scope.get("can_create_ticket") is False:
                elapsed = time.time() - start_time
                record_node_telemetry(
                    node="dispatcher",
                    latency_ms=int(elapsed * 1000),
                    status="PERMISSION_DENIED",
                    output_data={"intent": "refusal", "permission": "103_missing"},
                )
                return {
                    "intent": "general_chat",
                    "intent_confidence": confidence,
                    "messages": [
                        AIMessage(content="You do not have permission to create tickets. Please contact your workspace administrator to request ticket creation access.")
                    ],
                }

            return {
                "intent": "automation",
                "intent_confidence": confidence,
            }

        elapsed = time.time() - start_time
        record_node_telemetry(
            node="dispatcher",
            latency_ms=int(elapsed * 1000),
            status="SUCCESS",
            output_data={"intent": intent, "userQuery": user_msg, "confidence": confidence},
        )
        return {
            "intent": intent,
            "intent_confidence": confidence,
        }

    return dispatcher_node
