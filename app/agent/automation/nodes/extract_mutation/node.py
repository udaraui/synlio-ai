import re
import time
import json
import yaml
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from app.agent.automation.state import AutomationState
from app.agent.automation.schema import MutationPayload, TicketMutationChanges, SemanticQueryPlan, QueryFilterCondition
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
        prompt_context = {
            k: v
            for k, v in context.items()
            if k not in ("tickets", "tasks", "ticketList", "taskList", "data")
        }

        system_content = (
            EXTRACT_MUTATION_PROMPT_TEMPLATE
            .replace("{context_json}", json.dumps(prompt_context))
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

        # Blacklist common hyphenated English words that are NOT task or ticket codes
        CODE_BLACKLIST = {
            "HIGH-PRIORITY", "LOW-PRIORITY", "MEDIUM-PRIORITY", "CRITICAL-PRIORITY",
            "IN-PROGRESS", "TO-DO", "TO-START", "IN-REVIEW", "SUB-TASK", "BUG-FIX",
            "USER-FRIENDLY", "HIGH-SEVERITY", "LOW-SEVERITY", "MEDIUM-SEVERITY"
        }

        # Deterministic regex extraction of ticket and task codes
        # Ticket code: Prefix hyphen and ONLY digits (e.g. PTS-23, TSU-5, SSD-85)
        # Task code: Prefix hyphen, level letter, and digits (e.g. PTS-P2, TSU-P10, PRJ-P7, QTE-E2-S2)
        ticket_codes = [c.strip().upper() for c in re.findall(r'\b[A-Za-z0-9]+-\d+\b', user_msg) if c.strip().upper() not in CODE_BLACKLIST]
        task_codes = [c.strip().upper() for c in re.findall(r'\b[A-Za-z0-9]+(?:-[A-Za-z]+\d+)+\b', user_msg) if c.strip().upper() not in CODE_BLACKLIST]

        # Check for partial task codes like -E2-S2 or E2-S2 when space prefix is known
        space_prefix = (
            context.get("taskSpacePrefix")
            or context.get("prefix")
            or context.get("task_space_prefix")
            or context.get("ticketSpacePrefix")
            or ""
        )
        partial_task_codes = re.findall(r'(?:^|\s)-?([A-Za-z]+\d+(?:-[A-Za-z]+\d+)+)\b', user_msg)
        for ptc in partial_task_codes:
            full_code = f"{space_prefix.upper()}-{ptc.upper()}" if space_prefix else ptc.upper()
            if full_code not in task_codes and ptc.upper() not in CODE_BLACKLIST:
                task_codes.append(full_code)

        # Check for multi-ticket references like "TSU-20 and 21" or "TSU-20 and TSU-21"
        and_number_match = re.search(r'\b([A-Za-z0-9]+)-(\d+)\s+(?:and|&|,)\s+(?:[A-Za-z0-9]+-)?(\d+)\b', user_msg, re.IGNORECASE)
        if and_number_match:
            pfx = and_number_match.group(1).upper()
            c1 = f"{pfx}-{and_number_match.group(2)}"
            c2 = f"{pfx}-{and_number_match.group(3)}"
            if c1 not in ticket_codes: ticket_codes.append(c1)
            if c2 not in ticket_codes: ticket_codes.append(c2)

        user_msg_codes = ticket_codes + task_codes
        if user_msg_codes:
            targets = []
            seen_targets = set()
            for c in user_msg_codes:
                if c not in seen_targets:
                    seen_targets.add(c)
                    targets.append(c)
        else:
            targets = [str(t).strip().upper() for t in (extracted_data.get("targets") or []) if str(t).strip() and str(t).strip().upper() not in CODE_BLACKLIST]

        raw_changes = extracted_data.get("changes") or {}
        clean_changes = {}
        for k, v in raw_changes.items():
            if v is not None and str(v).strip() != "" and str(v).lower() != "null":
                clean_changes[k] = v

        # Initialize filter_data early so it is always in scope for severity and status detection
        filter_data = dict(extracted_data.get("filter") or {})

        # Deterministic extraction of target status if missed by LLM (strictly from current message, never history)
        user_text_lower = user_msg.lower()
        is_bulk_all = bool(
            re.search(r'\b(?:all|every)\b.*?\b(?:tasks?|tickets?|items?|projects?|bugs?|issues?)\b', user_text_lower)
            or filter_data.get("all")
        )
        if is_bulk_all:
            filter_data["all"] = True

        # Check if user message explicitly references an item by title/name
        # e.g. "change status of Ticker implementation to in progress", "rename dbt refresh issue as 'DBT job issue'",
        # "ssss task assign to me", "update start date of validate customer import 12 days before the end date"
        title_ref_match = None
        if not is_bulk_all:
            title_ref_match = re.search(
                r'\b(?:change\s+status\s+of|update\s+status\s+of|status\s+of|rename|update|move|mark|assign\s+(?:task\s+|ticket\s+)?)\s*([A-Za-z0-9_\s-]+?)\s+(?:to|as|into|status)\b',
                user_msg,
                re.IGNORECASE,
            )
            if not title_ref_match:
                # Pattern: "<title> task assign to <user>" or "<title> assign to <user>"
                title_ref_match = re.search(
                    r'\b([A-Za-z0-9_\s-]+?)\s+(?:task\s+|ticket\s+)?assign(?:\s+it)?\s+to\s+([A-Za-z0-9_\s-]+)\b',
                    user_msg,
                    re.IGNORECASE,
                )
            if not title_ref_match:
                # Pattern: "update start/due date of <title> to/before/after <date>"
                title_ref_match = re.search(
                    r'\b(?:update\s+(?:start|due|end)?\s*date\s+of|start\s+date\s+of|due\s+date\s+of)\s+([A-Za-z0-9_\s-]+?)(?:\s+\d+|\s+to|\s+before|\s+after|$)',
                    user_msg,
                    re.IGNORECASE,
                )

        def _is_valid_title_candidate(cand: str) -> bool:
            c_low = cand.strip().lower()
            if not c_low or c_low in ["all", "the", "this", "my", "tasks", "tickets"]:
                return False
            if re.match(r'^(?:all|every)\b', c_low):
                return False
            if re.search(r'\b(?:tickets?|tasks?|priority|severity)\b', c_low):
                return False
            return True

        has_title_reference = bool(
            (title_ref_match and _is_valid_title_candidate(title_ref_match.group(1)))
            or filter_data.get("query")
        )
        if has_title_reference and not filter_data.get("query") and title_ref_match:
            cand = title_ref_match.group(1).strip()
            if _is_valid_title_candidate(cand):
                filter_data["query"] = cand

        # Check if user query contains any filter criteria (query, bulk, unassigned, status, severity)
        has_filter_criteria = bool(
            is_bulk_all
            or has_title_reference
            or filter_data.get("query")
            or filter_data.get("status")
            or filter_data.get("severity")
            or filter_data.get("assignee")
            or "unassigned" in user_text_lower
            or "unassign" in user_text_lower
            or "unresolved" in user_text_lower
            or "priority" in user_text_lower
        )

        # Clear any LLM-hallucinated targets when the user gave filter criteria and no explicit codes
        if not user_msg_codes and has_filter_criteria:
            targets = []

        # Only check history if current user message has NO codes, is NOT a bulk/filter query, and is NOT a title reference
        if not targets and not has_filter_criteria:
            hist_ticket_codes = [c.strip().upper() for c in re.findall(r'\b[A-Za-z0-9]+-\d+\b', chat_str) if c.strip().upper() not in CODE_BLACKLIST]
            hist_task_codes = [c.strip().upper() for c in re.findall(r'\b[A-Za-z0-9]+(?:-[A-Za-z]+\d+)+\b', chat_str) if c.strip().upper() not in CODE_BLACKLIST]
            all_hist = hist_ticket_codes + hist_task_codes
            if all_hist:
                targets = [all_hist[-1]]

        # Fallback to context ticketCode if still no target was parsed from text and not a filter query
        if not targets and not has_filter_criteria:
            ctx_code = context.get("ticketCode") or context.get("code") or context.get("ticket_code")
            if ctx_code and str(ctx_code).strip().upper() not in CODE_BLACKLIST:
                targets = [str(ctx_code).strip().upper()]

        # Deterministic extraction of target status if missed by LLM (strictly from current message, never history)
        if not clean_changes.get("statusName") and not clean_changes.get("status"):
            status_mappings = [
                (r'\b(?:to|as|into|set to|mark as|mark|make)\b.*?\bin\s*progress+s*\b|\bin\s*progress+s*\b', "In Progress"),
                (r'\b(?:to|as|into|set to|mark as|mark|make)\b.*?\bdone\b|\bmark\s+(?:as\s+)?done\b|\bcompleted?\b', "Done"),
                (r'\b(?:to|as|into|set to|mark as|mark|make)\b.*?\bin\s*review\b', "In Review"),
                (r'\b(?:to|as|into|set to|mark as|mark|make)\b.*?\bto\s*start\b|\bto-start\b', "To Start"),
                (r'\b(?:to|as|into|set to|mark as|mark|make)\b.*?\bto\s*do\b|\btodo\b', "To Do"),
                (r'\b(?:to|as|into|set to|mark as|mark|make)\b.*?\bclosed?\b|\bclose ticket\b', "Closed"),
                (r'\b(?:to|as|into|set to|mark as|mark|make)\b.*?\bopen\b|\breopen\b', "Open"),
                (r'\b(?:to|as|into|set to|mark as|mark|make)\b.*?\bresolved?\b|\bresolve\b', "Resolved"),
            ]
            for pattern, status_val in status_mappings:
                if re.search(pattern, user_text_lower):
                    clean_changes["statusName"] = status_val
                    break

        # Preserve "To Do" when the user explicitly asked for To Do / TODO rather than To Start
        if re.search(r'\b(?:to\s*do|todo)\b', user_text_lower) and not re.search(r'\bto[-\s]*start\b', user_text_lower):
            if clean_changes.get("statusName") == "To Start":
                clean_changes["statusName"] = "To Do"
            if clean_changes.get("status") == "To Start":
                clean_changes["status"] = "To Do"

        # Deterministic extraction of target queue if missed by LLM
        if not clean_changes.get("queueName") and not clean_changes.get("queue"):
            queue_match = re.search(r'\b(?:queue|dept|department)\s+(?:to|as|into|=|change to)?\s*([A-Za-z0-9_-]+)\b', user_text_lower)
            if queue_match:
                q_val = queue_match.group(1).strip()
                if q_val and q_val.lower() not in ["the", "a", "an", "all", "status", "to", "as", "in"]:
                    clean_changes["queueName"] = q_val.capitalize()

        # Deterministic extraction of target ticket type if missed by LLM
        if not clean_changes.get("ticketTypeName") and not clean_changes.get("ticketType") and not clean_changes.get("type"):
            type_match = re.search(
                r'\b(?:ticket\s*type|type)\s*(?:of\s+[A-Za-z0-9_-]+\s*)?(?:to|as|into|=)\s*([A-Za-z0-9_\s-]+?)(?:\s+(?:for|in|on|at|with)\b|$)',
                user_text_lower
            )
            if not type_match:
                type_match = re.search(
                    r'\b(?:change|set|switch|update|convert|make)\b.*?\b(?:ticket\s*type|type)\b.*?\b(?:to|as|into)\s+([A-Za-z0-9_\s-]+?)(?:\s+(?:for|in|on|at|with)\b|$)',
                    user_text_lower
                )
            if type_match:
                t_val = type_match.group(1).strip()
                if t_val and t_val.lower() not in ["the", "a", "an", "all", "status", "to", "as", "in", "it"]:
                    # Normalize known casing if matched
                    known_types = {"bug": "Bug", "crn": "CRN", "service request": "Service Request", "maintenance": "Maintenance", "task": "Task", "incident": "Incident"}
                    clean_changes["ticketTypeName"] = known_types.get(t_val.lower(), t_val.capitalize())

        # Extract both target severity change AND source filter severity
        for sev in ["critical", "high", "medium", "low"]:
            is_setting_severity = bool(
                re.search(rf'\b(?:severity|priority)\b.*?\b(?:to|as|=|into)\s+{sev}\b', user_text_lower)
                or re.search(rf'\b(?:to|as|into|mark\s+as|set\s+to|change\s+to)\s+{sev}\b', user_text_lower)
            )
            is_filter_severity = bool(
                re.search(rf'\b{sev}[-\s]+(?:severity|priority)\b', user_text_lower)
                or re.search(rf'\b(?:all|every|open|unresolved)\s+{sev}\b', user_text_lower)
                or re.search(rf'\b(?:tickets?|tasks?|items?|bugs?|issues?)\s+(?:with|in|having|of)\s+(?:(?:severity|priority)\s+)?{sev}(?:\s+(?:severity|priority))?\b', user_text_lower)
            )
            if is_setting_severity:
                if not clean_changes.get("severityName") and not clean_changes.get("severity"):
                    clean_changes["severityName"] = sev.capitalize()
            elif is_filter_severity:
                if not filter_data.get("severity") and not filter_data.get("severityName"):
                    filter_data["severity"] = sev.capitalize()
                if str(clean_changes.get("severityName") or "").strip().lower() == sev:
                    clean_changes.pop("severityName", None)
                if str(clean_changes.get("severity") or "").strip().lower() == sev:
                    clean_changes.pop("severity", None)

        # Filter extraction for unassigned tickets/tasks
        if re.search(r'\ball\s+(?:unassigned|unassign)\b', user_text_lower) or "unassign" in user_text_lower or "unassigned" in user_text_lower:
            if "assign to" not in user_text_lower or re.search(r'\ball\s+unassign(?:ed)?\s+(?:tickets?|tasks?)\b', user_text_lower):
                filter_data["assignee"] = "Unassigned"

        # Filter extraction for status filters in query
        if not filter_data.get("status"):
            if "unresolved" in user_text_lower or "not resolved" in user_text_lower:
                filter_data["status"] = "unresolved"
            else:
                status_filter_match = re.search(
                    r'\b(?:all\s+)?(open|closed|resolved|todo|to\s*do|in\s*progress)\s+(?:priority\s+)?(?:tickets?|tasks?|items?)\b',
                    user_text_lower
                )
                if status_filter_match:
                    st = status_filter_match.group(1).strip()
                    status_map = {"open": "Open", "closed": "Closed", "resolved": "Resolved", "todo": "To Do", "to do": "To Do", "in progress": "In Progress"}
                    filter_data["status"] = status_map.get(st.lower(), st.capitalize())

        # Refresh has_filter_criteria after deterministic extraction
        has_filter_criteria = bool(
            has_filter_criteria
            or filter_data.get("severity")
            or filter_data.get("severityName")
            or filter_data.get("status")
            or filter_data.get("assignee")
            or filter_data.get("query")
        )
        if not user_msg_codes and has_filter_criteria:
            targets = []

        # Hierarchy Immutability Guard: Never allow modifying hierarchy level of an existing task
        # Note: Check ONLY current user message (never chat history) and require explicit level-altering action
        has_hierarchy_alteration_intent = bool(
            re.search(
                r'\b(?:change\s+(?:the\s+)?(?:hierarchy|level)|change\s+level\s+of|set\s+(?:the\s+)?level|switch\s+(?:the\s+)?level|'
                r'convert\s+(?:.*?\s+)?(?:in)?to\s+(?:a\s+)?(?:project|milestone|task|subtask|folder)|'
                r'turn\s+(?:.*?\s+)?into\s+(?:a\s+)?(?:project|milestone|task|subtask|folder)|'
                r'make\s+(?:.*?\s+)?(?:a\s+|into\s+(?:a\s+)?)(?:project|milestone|task|subtask|folder)|'
                r'promote\s+(?:.*?\s+)?(?:to|into)\s+(?:a\s+)?(?:project|milestone|task)|'
                r'demote\s+(?:.*?\s+)?(?:to|into)\s+(?:a\s+)?(?:task|subtask))\b',
                user_msg,
                re.IGNORECASE
            )
        )
        is_hierarchy_refusal = has_hierarchy_alteration_intent
        if is_hierarchy_refusal:
            refusal_summary = "Hierarchy levels are permanently fixed upon task creation and cannot be modified."
            elapsed = time.time() - start_time
            record_node_telemetry(
                node="automation_extract_mutation",
                latency_ms=int(elapsed * 1000),
                status="HIERARCHY_MUTATION_REFUSED",
                output_data={"action": "refusal", "reason": "hierarchy_immutable"},
            )
            return {
                "mutation_payload": {
                    "action": "UPDATE_TASK",
                    "status": "REFUSED",
                    "entityType": "task",
                    "targets": targets,
                    "changes": {},
                    "summary": refusal_summary,
                    "warnings": [refusal_summary],
                    "attemptedHierarchyChange": True,
                },
                "automation_action": "UPDATE_TASK",
                "summary": refusal_summary,
                "automation_summary": refusal_summary,
                "messages": [AIMessage(content=refusal_summary)],
            }

        agent = state.get("agent") or (state.get("active_workers")[0] if state.get("active_workers") else None)
        is_ticket_agent = agent in ("ticket_analytics", "ticket")
        is_task_agent = agent in ("project_analytics", "task", "project")

        has_target_task_codes = any(bool(re.match(r'^[A-Za-z0-9]+-[A-Za-z]+', t)) for t in targets)
        has_target_ticket_codes = any(bool(re.match(r'^[A-Za-z0-9]+-\d+$', t)) for t in targets)

        state_entity_type = state.get("entity_type")
        if state_entity_type == "task":
            is_task = True
        elif state_entity_type == "ticket":
            is_task = False
        elif has_target_task_codes and not has_target_ticket_codes:
            is_task = True
        elif has_target_ticket_codes and not has_target_task_codes:
            is_task = False
        elif is_ticket_agent:
            is_task = False
        elif is_task_agent:
            is_task = True
        else:
            is_task = (
                extracted_data.get("entityType") == "task"
                or extracted_data.get("action") == "UPDATE_TASK"
                or bool(context.get("task_space_id") or context.get("taskSpaceId"))
                or any(w in user_text_lower for w in ["task", "project", "initiative", "folder", "milestone", "subtask"])
            )
        action_name = "UPDATE_TASK" if is_task else "UPDATE_TICKET"
        entity_name = "task" if is_task else "ticket"

        # Guard: if assignee was extracted in changes but user query indicates filter (e.g. "assigned to X"), move to filter
        if "assigned to" in user_text_lower or "tickets for" in user_text_lower or "tasks for" in user_text_lower:
            if clean_changes.get("assigneeName") and not filter_data.get("assignee"):
                filter_data["assignee"] = clean_changes.pop("assigneeName")

        # Extract source status if user query has "in todo status", "with status To Do", etc.
        if not filter_data.get("status"):
            source_status_match = re.search(
                r'\b(?:in|with|having)\s+(?:status\s+)?(to\s*do|todo|to\s*start|to-start|in\s*progress|in\s*review|done|completed|closed|open|resolved)(?:\s+status)?\b',
                user_text_lower
            )
            if source_status_match:
                matched_raw = source_status_match.group(1).strip()
                status_map = {
                    "to do": "To Do", "todo": "To Do",
                    "to start": "To Start", "to-start": "To Start",
                    "in progress": "In Progress",
                    "in review": "In Review",
                    "done": "Done", "completed": "Done",
                    "closed": "Closed", "open": "Open", "resolved": "Resolved"
                }
                filter_data["status"] = status_map.get(matched_raw, matched_raw.title())

        # Defer bulk / filtered resolution without explicit codes to the backend DB query across the entire space
        # (rather than truncating to paginated page-1 items in context).
        has_any_filter = bool(
            is_bulk_all
            or filter_data.get("status")
            or filter_data.get("assignee")
            or filter_data.get("severity")
            or filter_data.get("severityName")
            or filter_data.get("priority")
            or filter_data.get("ticketType")
            or filter_data.get("type")
            or filter_data.get("queue")
            or filter_data.get("dept")
            or filter_data.get("department")
            or filter_data.get("query")
        )
        if not user_msg_codes and has_any_filter:
            targets = []

        # Fuzzy target title / name extraction if no explicit code targets and no structured filter found
        if not targets and not is_bulk_all:
            fuzzy_name = extracted_data.get("target_name") or extracted_data.get("target_title") or extracted_data.get("target") or filter_data.get("query")
            if fuzzy_name and not filter_data.get("query"):
                filter_data["query"] = str(fuzzy_name).strip()
            if not filter_data.get("query") and not has_any_filter:
                title_match = re.search(r'\b(?:update|change|move|mark)\s+(?:task\s+|ticket\s+)?([A-Za-z0-9\s_-]+?)\s+(?:to|as|into|status)\b', user_msg, re.IGNORECASE)
                if title_match:
                    candidate_name = title_match.group(1).strip()
                    if _is_valid_title_candidate(candidate_name):
                        filter_data["query"] = candidate_name

        is_children_scope = bool(re.search(r'\b(?:all\s+)?(?:child(?:ren)?|subtasks?|tasks?)\s+of\b', user_msg, re.IGNORECASE))

        summary = extracted_data.get("summary")
        # Discard hallucinated hierarchy refusal summary on non-refused requests
        if summary and "hierarchy levels are permanently fixed" in summary.lower() and not is_hierarchy_refusal:
            summary = None

        status_val = clean_changes.get("statusName") or clean_changes.get("status")
        assignee_val = clean_changes.get("assigneeName") or clean_changes.get("assignee")
        sev_val = clean_changes.get("severityName") or clean_changes.get("severity")
        type_val = clean_changes.get("ticketTypeName") or clean_changes.get("ticketType")
        queue_val = clean_changes.get("queueName") or clean_changes.get("queue")
        date_val = clean_changes.get("startDate") or clean_changes.get("dueDate")

        change_parts = []
        if status_val:
            change_parts.append(f"status to **{status_val}**")
        if assignee_val:
            change_parts.append(f"assignee to **{assignee_val}**")
        if sev_val:
            change_parts.append(f"priority to **{sev_val}**")
        if type_val:
            change_parts.append(f"type to **{type_val}**")
        if queue_val:
            change_parts.append(f"queue to **{queue_val}**")
        if date_val:
            change_parts.append("dates")
        if not change_parts and clean_changes:
            change_parts = [f"{k.replace('Name', '')} to **{v}**" for k, v in clean_changes.items()]
        change_desc = ", ".join(change_parts)

        # Overwrite summary if empty, if LLM mistakenly asked for ticket/task code despite targets/filters,
        # or if LLM returned a raw code dump / past-tense claim for many targets
        is_question_asking_target = bool(
            summary and re.search(r'\bwhich\s+(?:task|ticket|item|project|one)s?\b', summary, re.IGNORECASE)
        )
        should_rebuild_summary = (
            not summary
            or (targets and is_question_asking_target)
            or (not targets and has_any_filter and bool(change_parts))
            or len(targets) > 3
        )
        if should_rebuild_summary:
            if targets:
                target_str = ", ".join(f"**{t}**" for t in targets)
                suffix = f" — setting {change_desc}" if change_desc else ""
                if is_children_scope and status_val:
                    summary = f"I've prepared updates for all child tasks of {target_str} — setting status to **{status_val}**. Please review and confirm below."
                elif len(targets) == 1:
                    summary = f"I've prepared the update for {target_str}{suffix}. Please review and confirm below."
                elif len(targets) <= 3:
                    summary = f"I've prepared updates for {len(targets)} {entity_name}s ({target_str}){suffix}. Please review and confirm below."
                else:
                    summary = f"I've prepared updates for {len(targets)} {entity_name}s{suffix}. Please review and confirm below."
            elif has_any_filter and change_desc:
                filter_bits = []
                sev_f = filter_data.get("severity") or filter_data.get("severityName") or filter_data.get("priority")
                st_f = filter_data.get("status")
                asg_f = filter_data.get("assignee")
                if sev_f:
                    filter_bits.append(f"{sev_f}-priority")
                if st_f:
                    filter_bits.append(str(st_f))
                if asg_f:
                    filter_bits.append(f"assigned to {asg_f}" if str(asg_f).lower() != "unassigned" else "unassigned")
                filter_label = " ".join(filter_bits)
                noun = f"{filter_label} {entity_name}s" if filter_label else f"matching {entity_name}s"
                summary = f"I've prepared updates for {noun} — setting {change_desc}. Please review and confirm below."
            elif filter_data.get("query"):
                summary = f"Which {entity_name} matching \"{filter_data['query']}\" would you like to update?"
            elif filter_data.get("assignee"):
                summary = f"Which {entity_name}(s) assigned to **{filter_data['assignee']}** would you like to update?"
            else:
                summary = f"Which {entity_name} would you like to update? Please provide the {entity_name} code (e.g. { 'PRJ-10' if is_task else 'PTS-12' })."

        # ── AI Query Builder: Generate Structured Semantic Query Plan ──
        query_conditions = []
        user_lower = user_msg.lower()

        # 1. Explicit Target Code Conditions
        if targets:
            query_conditions.append({
                "field": "code",
                "operator": "in",
                "value": [str(t).upper() for t in targets]
            })

        # 2. Status Conditions (translates natural query terms to base category + name constraints)
        status_filter = filter_data.get("status")
        if status_filter:
            st = str(status_filter).strip()
            st_lower = st.lower()
            if st_lower in ("unresolved", "not resolved", "active"):
                query_conditions.append({
                    "field": "statusBase",
                    "operator": "neq",
                    "value": "FINISHED"
                })
                query_conditions.append({
                    "field": "statusName",
                    "operator": "not_in",
                    "value": ["Resolved", "Closed", "Done", "Cancelled", "Completed"]
                })
            elif st_lower in ("open", "todo", "to do", "to start", "to-start", "new", "backlog"):
                query_conditions.append({
                    "field": "statusBase",
                    "operator": "in",
                    "value": ["TOSTART"]
                })
            elif st_lower in ("in progress", "ongoing", "processing", "in review"):
                query_conditions.append({
                    "field": "statusBase",
                    "operator": "in",
                    "value": ["PROCESSING"]
                })
            elif st_lower in ("done", "closed", "resolved", "completed"):
                query_conditions.append({
                    "field": "statusBase",
                    "operator": "in",
                    "value": ["FINISHED"]
                })
            else:
                query_conditions.append({
                    "field": "statusName",
                    "operator": "ilike",
                    "value": f"%{st}%"
                })

        # 3. Assignee Conditions
        assignee_filter = filter_data.get("assignee")
        if assignee_filter:
            av = str(assignee_filter).strip()
            av_lower = av.lower()
            if av_lower in ("unassigned", "unassign", "none", "no one") or filter_data.get("unassigned"):
                query_conditions.append({
                    "field": "assignee",
                    "operator": "is_null",
                    "value": True
                })
            elif av_lower in ("me", "myself", "i", "self"):
                query_conditions.append({
                    "field": "assignee",
                    "operator": "eq",
                    "value": "me"
                })
            else:
                query_conditions.append({
                    "field": "assignee",
                    "operator": "ilike",
                    "value": f"%{av}%"
                })

        # 4. Severity / Priority Conditions
        sev_filter = filter_data.get("severity") or filter_data.get("severityName") or filter_data.get("priority")
        if sev_filter:
            query_conditions.append({
                "field": "severity",
                "operator": "ilike",
                "value": f"%{str(sev_filter).strip()}%"
            })

        # 5. Ticket Type Conditions
        type_filter = filter_data.get("ticketType") or filter_data.get("type")
        if type_filter and not is_task:
            query_conditions.append({
                "field": "ticketType",
                "operator": "ilike",
                "value": f"%{str(type_filter).strip()}%"
            })

        # 6. Queue / Department Conditions
        queue_filter = filter_data.get("queue") or filter_data.get("dept") or filter_data.get("department")
        if queue_filter and not is_task:
            query_conditions.append({
                "field": "queue",
                "operator": "ilike",
                "value": f"%{str(queue_filter).strip()}%"
            })

        # 7. Name / Search / Fuzzy Query Conditions
        query_filter = filter_data.get("query") or filter_data.get("title") or filter_data.get("name") or filter_data.get("search")
        if query_filter:
            query_conditions.append({
                "field": "name",
                "operator": "fuzzy",
                "value": str(query_filter).strip()
            })

        # Determine Target Scope
        is_bulk = (
            filter_data.get("all") is True
            or bool(re.search(r'\b(?:all|every)\b', user_lower))
            or len(targets) > 1
        )
        if is_children_scope:
            target_scope = "children"
        elif is_bulk:
            target_scope = "all"
        elif len(targets) == 1:
            target_scope = "single"
        else:
            target_scope = "all" if (status_filter or assignee_filter or sev_filter) and "all" in user_lower else "select"

        query_plan = {
            "entityType": entity_name,
            "targetScope": target_scope,
            "logic": "AND",
            "conditions": query_conditions,
            "mutation": clean_changes,
            "summary": summary
        }

        mutation_payload = {
            "action": action_name,
            "status": "READY" if targets else "NEEDS_INPUT",
            "entityType": entity_name,
            "targets": targets,
            "explicitTargets": bool(user_msg_codes),
            "targetScope": target_scope,
            "includeChildren": is_children_scope,
            "filter": filter_data if filter_data else None,
            "queryPlan": query_plan,
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
