import os
import re
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
        user_msg = ""
        for msg in reversed(messages):
            if not user_msg and (isinstance(msg, HumanMessage) or getattr(msg, "type", "") in ("human", "user")):
                user_msg = str(getattr(msg, "content", msg))
        for msg in messages[-6:]:
            role = "User" if isinstance(msg, HumanMessage) else "Assistant"
            recent_chat.append(f"{role}: {msg.content}")
        chat_str = "\n".join(recent_chat)

        user_msg_lower = user_msg.lower()
        user_msg_clean = user_msg.strip()
        is_clone_intent = any(w in user_msg_lower for w in ["clone", "duplicate", "copy", "same as"])

        # Check mentionedItems from context first (highest accuracy)
        mentioned_items = context.get("mentionedItems") or []
        mentioned_code = context.get("mentionedCode") or (mentioned_items[0].get("code") if mentioned_items else None)

        # Detect cloning / duplicating existing ticket, task, or project code (e.g. "duplicate PTS-P7" or "same as PTS-11")
        clone_match = re.search(
            r'\b(?:same\s+as|clone|duplicate|copy\s+of|based\s+on|like)\s+(?:the\s+)?(?:project\s+|task\s+|ticket\s+)?([A-Za-z0-9_-]+-(?:P\d+|\d+(?:-\w+)*))\b',
            f"{user_msg} {chat_str}",
            re.IGNORECASE,
        )
        found_code = None
        if not clone_match and is_clone_intent:
            any_code_match = re.search(r'\b([A-Za-z0-9_-]+-(?:P\d+|\d+(?:-\w+)*))\b', user_msg, re.IGNORECASE)
            if any_code_match:
                found_code = any_code_match.group(1).upper()

        source_code = (clone_match.group(1).upper() if clone_match else None) or found_code or (mentioned_code.upper() if mentioned_code and is_clone_intent else None)

        is_project_code = bool(
            (source_code and bool(re.search(r'-P\d+$', source_code, re.IGNORECASE)))
            or context.get("isProject")
            or any(m.get("isProject") for m in mentioned_items if m.get("code") == source_code)
            or ("project" in user_msg_lower and is_clone_intent)
        )

        allowed_spaces = scope.get("allowed_spaces") or []
        current_date = context.get("currentDate") or datetime.utcnow().strftime("%Y-%m-%d")
        timezone = context.get("timezone") or "UTC"

        code_match = re.search(r'\b([A-Za-z0-9_-]+-P\d+)\b', user_msg, re.IGNORECASE)
        has_task_words = any(w in user_msg_lower for w in ["task", "subtask", "sub-task", "project", "initiative", "folder", "milestone"])
        has_ticket_words = any(k in user_msg_lower for k in ["ticket", "bug", "issue"])
        has_task_code = bool(re.search(r'\b[A-Za-z0-9]+-[A-Za-z]+[A-Za-z0-9_-]*\b', user_msg))
        has_ticket_code = bool(re.search(r'\b[A-Za-z0-9]+-\d+\b', user_msg))

        # Check domain from agent preset, pending draft, context, or message
        agent = state.get("agent") or (state.get("active_workers")[0] if state.get("active_workers") else None)
        is_ticket_agent = agent in ("ticket_analytics", "ticket")
        is_task_agent = agent in ("project_analytics", "task", "project")
        pending_list = pending_draft if isinstance(pending_draft, list) else ([pending_draft] if isinstance(pending_draft, dict) else [])
        has_task_in_pending = any(
            isinstance(item, dict) and (
                item.get("entityType") == "task" or
                item.get("parentTaskId") or
                item.get("parentTaskName") or
                item.get("hierarchyLevelSequence")
            )
            for item in pending_list
        )

        # Resolve space from active page context if available
        context_ticket_space_name = (
            context.get("ticket_space_name")
            or context.get("ticketSpaceName")
            or (context.get("spaceName") if context.get("ticket_space_id") or context.get("ticketSpaceId") else None)
        )
        context_ticket_space_id = (
            context.get("ticket_space_id")
            or context.get("ticketSpaceId")
        )

        context_task_space_name = (
            context.get("task_space_name")
            or context.get("taskSpaceName")
            or (context.get("spaceName") if context.get("task_space_id") or context.get("taskSpaceId") else None)
        )
        context_task_space_id = (
            context.get("task_space_id")
            or context.get("taskSpaceId")
        )

        # Determine domain authority:
        state_entity_type = state.get("entity_type")
        if is_task_agent:
            # In Project Analyst, strictly task domain unless user explicitly targeted a ticket code
            is_task_domain = not (has_ticket_code and not has_task_code)
        elif is_ticket_agent:
            # In Ticket Analyst, strictly ticket domain unless user explicitly targeted a task code
            is_task_domain = (has_task_code and not has_ticket_code)
        elif state_entity_type == "task":
            is_task_domain = True
        elif state_entity_type == "ticket":
            is_task_domain = False
        else:
            is_task_domain = (
                has_task_in_pending
                or has_task_words
                or bool(context_task_space_id or context_task_space_name)
                or str(context.get("page") or "").lower() in ["task", "task-management", "tasks"]
                or str(context.get("pageScopeKey") or "").lower() in ["task", "task-management", "tasks"]
            ) and not has_ticket_words

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

        instruction = "Extract the updated task draft (or drafts if multiple items exist):" if is_task_domain else "Extract the updated ticket draft (or drafts if multiple items exist):"
        if attachments:
            if is_task_domain:
                instruction = (
                    "DOCUMENT TASK EXTRACTION INSTRUCTIONS:\n"
                    "The user has uploaded one or more documents above. The document may be in ANY format (CSV, spreadsheet, table, Word doc, PDF, or text notes).\n"
                    "1. Read and analyze the entire document content carefully.\n"
                    "2. If the document is tabular or contains multiple items (such as CSV rows, task lists, deliverables, or action items), you MUST extract EVERY SINGLE item/row as a distinct task draft. For example, if there are 10 items/rows, your JSON array MUST contain all 10 task objects!\n"
                    "3. Map fields intelligently from whatever headers, columns, or keys are used in the document:\n"
                    "   - 'name': The task name, title, summary, or action item (MANDATORY).\n"
                    "   - 'description': Detailed description, specifications, or notes.\n"
                    "   - 'assigneeName': Assigned person or owner if specified.\n"
                    "   - 'statusName': Status (e.g. To Do, In Progress, In Review, Done) if specified.\n"
                    "   - 'severityName': Priority or severity (e.g. Low, Medium, High, Urgent, Critical) if specified.\n"
                    "   - 'startDate': Start date (YYYY-MM-DD) if specified or inferred.\n"
                    "   - 'dueDate': Due date / End date (YYYY-MM-DD) if specified or inferred.\n"
                    "   - 'estimateEffort': Estimated effort or hours if specified.\n"
                    "   - 'parentTaskName': Parent project or parent task name if specified in hierarchy.\n"
                    "   - 'entityType': 'task'\n"
                    "4. Return ONLY a valid JSON array of task objects: [ { \"name\": \"...\", \"description\": \"...\", \"assigneeName\": \"...\", \"severityName\": \"...\", \"statusName\": \"...\", \"startDate\": \"...\", \"dueDate\": \"...\", \"entityType\": \"task\" }, ... ]. Do NOT include conversational filler text."
                )
            else:
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
                    "   - 'entityType': 'ticket'\n"
                    "4. Return ONLY a valid JSON array of ticket objects: [ { \"name\": \"...\", \"description\": \"...\", \"assigneeName\": \"...\", \"severityName\": \"...\", \"statusName\": \"...\", \"ticketTypeName\": \"...\", \"entityType\": \"ticket\" }, ... ]. Do NOT include conversational filler text."
                )

        user_msg_lower = user_msg.lower()
        user_msg_clean = user_msg.strip()
        code_match = re.search(r'\b([A-Za-z0-9_-]+-P\d+)\b', user_msg, re.IGNORECASE)
        is_create_as_new = any(p in user_msg_lower for p in ["create as new", "new project", "as a new project", "create new project", "create as a new project"])
        matched_space = next(
            (s for s in allowed_spaces if isinstance(s, dict) and s.get("name", "").strip().lower() == user_msg_clean.lower()),
            None
        )

        is_direct_disambiguation = (
            bool(pending_list)
            and not attachments
            and (is_create_as_new or code_match or ":" in user_msg_clean or matched_space is not None)
        )

        extracted_data = {}
        if is_direct_disambiguation:
            extracted_data = [dict(d) for d in pending_list] if isinstance(pending_draft, list) else dict(pending_draft)
        else:
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

        # Determine if extracted_data is multiple tickets/tasks or single item
        raw_drafts = []
        if isinstance(extracted_data, list):
            raw_drafts = extracted_data
        elif isinstance(extracted_data, dict):
            if "drafts" in extracted_data and isinstance(extracted_data["drafts"], list):
                raw_drafts = extracted_data["drafts"]
            elif "tickets" in extracted_data and isinstance(extracted_data["tickets"], list):
                raw_drafts = extracted_data["tickets"]
            elif "tasks" in extracted_data and isinstance(extracted_data["tasks"], list):
                raw_drafts = extracted_data["tasks"]
            else:
                raw_drafts = [extracted_data]
        else:
            raw_drafts = [dict(pending_draft)] if (pending_draft and isinstance(pending_draft, dict)) else []

        user_msg_clean = user_msg.strip().lower()
        matched_space = next(
            (s for s in allowed_spaces if isinstance(s, dict) and s.get("name", "").strip().lower() == user_msg_clean),
            None
        )

        # If there's an existing single pending_draft and extracted single draft, merge deltas
        if len(pending_list) == 1 and len(raw_drafts) == 1 and isinstance(pending_list[0], dict) and "name" in pending_list[0]:
            merged = dict(pending_list[0])
            for k, v in raw_drafts[0].items():
                if v is not None:
                    # Never overwrite task entityType with ticket
                    if k == "entityType" and merged.get("entityType") == "task":
                        continue
                    # Never overwrite task title with the parent option name/code selected by the user
                    if k == "name" and (code_match or ":" in user_msg_clean or user_msg_clean.upper() in str(v).upper()):
                        continue
                    merged[k] = v
            # If pending_list[0] already had parentTask info (e.g. populated when clicking option badge), preserve it!
            if pending_list[0].get("parentTaskId") and not merged.get("parentTaskId"):
                merged["parentTaskId"] = pending_list[0]["parentTaskId"]
            if pending_list[0].get("parentTaskName") and not merged.get("parentTaskName"):
                merged["parentTaskName"] = pending_list[0]["parentTaskName"]
            if pending_list[0].get("parentTaskCode") and not merged.get("parentTaskCode"):
                merged["parentTaskCode"] = pending_list[0]["parentTaskCode"]
            if pending_list[0].get("hierarchyLevelSequence") and not merged.get("hierarchyLevelSequence"):
                merged["hierarchyLevelSequence"] = pending_list[0]["hierarchyLevelSequence"]
            raw_drafts = [merged]
        # If pending_draft was already a list of multiple items:
        elif len(pending_list) > 1:
            if len(raw_drafts) == 1:
                # LLM returned a common update (e.g. space name or priority) for the batch
                single_delta = raw_drafts[0]
                updated_list = []
                for existing in pending_list:
                    merged = dict(existing)
                    for k, v in single_delta.items():
                        if v is not None:
                            # Keep each item's distinct name & description
                            if k in ("name", "description") and existing.get(k):
                                continue
                            if k == "assigneeName" and existing.get("assigneeName") and existing.get("assigneeName") != "me":
                                continue
                            if k == "entityType" and existing.get("entityType") == "task":
                                continue
                            merged[k] = v
                    updated_list.append(merged)
                raw_drafts = updated_list
            elif len(raw_drafts) == len(pending_list):
                updated_list = []
                for i, existing in enumerate(pending_list):
                    merged = dict(existing)
                    for k, v in raw_drafts[i].items():
                        if v is not None:
                            if k == "entityType" and existing.get("entityType") == "task":
                                continue
                            if k in ("name", "description") and existing.get(k):
                                continue
                            merged[k] = v
                    # Preserve parent relations from pending_draft if not updated
                    if existing.get("name"):
                        merged["name"] = existing["name"]
                    if existing.get("parentTaskId") and not merged.get("parentTaskId"):
                        merged["parentTaskId"] = existing["parentTaskId"]
                    if existing.get("parentTaskName") and not merged.get("parentTaskName"):
                        merged["parentTaskName"] = existing["parentTaskName"]
                    if existing.get("parentTaskCode") and not merged.get("parentTaskCode"):
                        merged["parentTaskCode"] = existing["parentTaskCode"]
                    if existing.get("isCreateNewLevel1"):
                        merged["isCreateNewLevel1"] = True
                        merged["hierarchyLevelSequence"] = 1
                    updated_list.append(merged)
                raw_drafts = updated_list

        user_msg_lower = user_msg.lower()
        user_msg_clean = user_msg.strip()
        code_match = re.search(r'\b([A-Za-z0-9_-]+-P\d+)\b', user_msg, re.IGNORECASE)
        is_create_as_new = any(p in user_msg_lower for p in ["create as new", "new project", "as a new project", "create new project", "create as a new project"])

        if is_create_as_new and raw_drafts:
            referenced_parent = next((d.get("parentTaskName") for d in raw_drafts if d.get("parentTaskName")), None)
            if referenced_parent and not any(d.get("name", "").strip().lower() == referenced_parent.strip().lower() for d in raw_drafts):
                new_project = {
                    "name": referenced_parent,
                    "isCreateNewLevel1": True,
                    "hierarchyLevelSequence": 0,
                    "entityType": "task",
                    "taskSpaceId": raw_drafts[0].get("taskSpaceId"),
                    "taskSpaceName": raw_drafts[0].get("taskSpaceName"),
                }
                raw_drafts.insert(0, new_project)
            elif not any(d.get("isCreateNewLevel1") for d in raw_drafts):
                if len(raw_drafts) > 1:
                    new_project = {
                        "name": "New Project",
                        "isCreateNewLevel1": True,
                        "hierarchyLevelSequence": 0,
                        "entityType": "task",
                        "taskSpaceId": raw_drafts[0].get("taskSpaceId"),
                        "taskSpaceName": raw_drafts[0].get("taskSpaceName"),
                    }
                    raw_drafts.insert(0, new_project)
                else:
                    raw_drafts[0]["hierarchyLevelSequence"] = 0
                    raw_drafts[0]["parentTaskId"] = None
                    raw_drafts[0]["parentTaskName"] = None
                    raw_drafts[0]["isCreateNewLevel1"] = True

            root_name = raw_drafts[0].get("name")
            for i in range(1, len(raw_drafts)):
                if not raw_drafts[i].get("parentTaskId"):
                    raw_drafts[i]["parentTaskName"] = raw_drafts[i].get("parentTaskName") or root_name
                    raw_drafts[i]["hierarchyLevelSequence"] = 1
        elif (code_match or ":" in user_msg_clean) and raw_drafts:
            for d in raw_drafts:
                if not d.get("parentTaskId") and not d.get("parentTaskName"):
                    d["parentTaskName"] = user_msg_clean
                    d["hierarchyLevelSequence"] = 1
        for item in raw_drafts:
            if isinstance(item, dict):
                if is_task_domain:
                    item["entityType"] = "task"
                    if item.get("ticketSpaceName") and not item.get("taskSpaceName"):
                        item["taskSpaceName"] = item.pop("ticketSpaceName")
                    if item.get("ticketSpaceId") and not item.get("taskSpaceId"):
                        item["taskSpaceId"] = item.pop("ticketSpaceId")
                    if not item.get("taskSpaceName") and context_task_space_name:
                        item["taskSpaceName"] = context_task_space_name
                    if not item.get("taskSpaceId") and context_task_space_id:
                        item["taskSpaceId"] = context_task_space_id
                    if matched_space:
                        item["taskSpaceName"] = matched_space["name"]
                        item["taskSpaceId"] = matched_space.get("id")
                else:
                    item["entityType"] = "ticket"
                    item.pop("parentTaskId", None)
                    item.pop("parentTaskName", None)
                    item.pop("parentTaskCode", None)
                    item.pop("hierarchyLevelSequence", None)
                    item.pop("hierarchyLevelName", None)
                    item.pop("isCreateNewLevel1", None)
                    if item.get("taskSpaceName") and not item.get("ticketSpaceName"):
                        item["ticketSpaceName"] = item.pop("taskSpaceName")
                    if item.get("taskSpaceId") and not item.get("ticketSpaceId"):
                        item["ticketSpaceId"] = item.pop("taskSpaceId")
                    if not item.get("ticketSpaceName") and context_ticket_space_name:
                        item["ticketSpaceName"] = context_ticket_space_name
                    if not item.get("ticketSpaceId") and context_ticket_space_id:
                        item["ticketSpaceId"] = context_ticket_space_id
                    if matched_space:
                        item["ticketSpaceName"] = matched_space["name"]
                        item["ticketSpaceId"] = matched_space.get("id")

        # Detect whether drafting a task or a ticket
        is_task_intent = is_task_domain

        clean_drafts: List[Dict[str, Any]] = []
        all_missing_fields: List[str] = []
        needs_fields: List[Dict[str, Any]] = []

        for item in raw_drafts:
            if not isinstance(item, dict):
                continue

            if is_task_intent or item.get("entityType") == "task":
                item["entityType"] = "task"
                # If user explicitly intended top-level container (e.g. project, initiative, folder) or cloning a project
                is_explicit_root = is_project_code or any(w in user_msg_lower for w in ["create as new", "create as new project", "create a project", "create project", "new project", "add project", "make a project", "create an initiative", "create initiative", "new initiative", "add folder", "as a new project", "create as a new project"])
                has_batch_root = is_project_code or is_create_as_new or is_explicit_root or any(d.get("isCreateNewLevel1") for d in raw_drafts if isinstance(d, dict))
                if is_explicit_root or item.get("isCreateNewLevel1"):
                    item["hierarchyLevelSequence"] = 0
                    item["parentTaskId"] = None
                    item["parentTaskName"] = None
                    item["isCreateNewLevel1"] = True
                else:
                    # User asked for a task or subtask - never default to Level 1 Project!
                    has_batch_parent = bool(item.get("parentTaskName") and any(
                        isinstance(other, dict) and other != item and
                        (other.get("name") or "").strip().lower() == item.get("parentTaskName", "").strip().lower()
                        for other in raw_drafts
                    ))
                    if not has_batch_parent:
                        if not item.get("parentTaskId") and not item.get("parentTaskName"):
                            item["hierarchyLevelSequence"] = 1
                            item["hierarchyLevelName"] = "Task"
                    else:
                        item["hierarchyLevelSequence"] = 2
                        item["hierarchyLevelName"] = "Subtask"

                # If cloning from an existing task/project
                if source_code:
                    item["sourceTaskCode"] = source_code
                    item["cloneChildren"] = bool(re.search(r'all\s+(?:levels?\s+)?children|with\s+(?:all\s+)?children|all\s+tasks|subtasks', user_msg_lower))
                    mentioned_title = context.get("mentionedTitle")
                    if is_project_code or (source_code and bool(re.search(r'-P\d+$', source_code, re.IGNORECASE))):
                        item["name"] = f"{mentioned_title} (Copy)" if mentioned_title else f"{source_code} (Copy)"
                        item["isCreateNewLevel1"] = True
                        item["hierarchyLevelSequence"] = 0

                # Task space assignment from context
                if not item.get("taskSpaceName") and not item.get("taskSpaceId"):
                    if context_task_space_name:
                        item["taskSpaceName"] = context_task_space_name
                    if context_task_space_id:
                        item["taskSpaceId"] = context_task_space_id

                clean = dict(item)

                if not clean.get("name") and not clean.get("sourceTaskCode") and not clean.get("sourceTicketCode"):
                    all_missing_fields.append("name")

                if not clean.get("taskSpaceName") and not clean.get("taskSpaceId"):
                    if "taskSpace" not in all_missing_fields:
                        all_missing_fields.append("taskSpace")
                elif not has_batch_root and not attachments and not is_explicit_root and not clean.get("isCreateNewLevel1") and not clean.get("parentTaskId") and not clean.get("parentTaskName") and not clean.get("sourceTaskCode"):
                    # Task has a space but user hasn't selected parent project yet and didn't specify creating root
                    if "parentTask" not in all_missing_fields:
                        all_missing_fields.append("parentTask")

                clean_drafts.append(clean)
            else:
                # Standard ticket draft logic
                item["entityType"] = "ticket"
                if source_code and not item.get("sourceTicketCode"):
                    item["sourceTicketCode"] = source_code

                # If user asks to clone or copy without a title, assign placeholder that backend will update
                if source_code:
                    current_name = str(item.get("name") or "").strip().lower()
                    if not current_name or any(current_name.startswith(p) for p in ["same as", "clone", "copy of", "duplicate", "like"]):
                        item["name"] = f"{source_code} (clone)"

                try:
                    draft_obj = TicketDraft(**item)
                    clean = draft_obj.model_dump(exclude_none=False)
                    clean["entityType"] = "ticket"
                except Exception:
                    clean = dict(item)
                    clean["entityType"] = "ticket"

                if source_code and not clean.get("sourceTicketCode"):
                    clean["sourceTicketCode"] = source_code

                # Auto-assign ticket space from active page context if not already specified
                if not clean.get("ticketSpaceName") and not clean.get("ticketSpaceId"):
                    if context_ticket_space_name:
                        clean["ticketSpaceName"] = context_ticket_space_name
                    if context_ticket_space_id:
                        clean["ticketSpaceId"] = context_ticket_space_id

                # Only require name if not cloning from an existing ticket
                if not clean.get("name") and not clean.get("sourceTicketCode"):
                    all_missing_fields.append("name")

                # Only require space if not specified and not cloning
                if not clean.get("ticketSpaceName") and not clean.get("ticketSpaceId") and not clean.get("sourceTicketCode"):
                    if "ticketSpace" not in all_missing_fields:
                        all_missing_fields.append("ticketSpace")

                clean_drafts.append(clean)

        if "taskSpace" in all_missing_fields:
            needs_fields.append({
                "field": "taskSpace",
                "label": "Task Space",
                "options": allowed_spaces,
            })
        elif "parentTask" in all_missing_fields:
            needs_fields.append({
                "field": "parentTask",
                "label": "Select Project",
                "options": [],
            })
        elif "ticketSpace" in all_missing_fields:
            needs_fields.append({
                "field": "ticketSpace",
                "label": "Ticket Space",
                "options": allowed_spaces,
            })

        is_complete = len(all_missing_fields) == 0

        if not is_complete:
            if "taskSpace" in all_missing_fields:
                summary_text = (
                    "Which task space should I place the tasks in?"
                    if len(clean_drafts) > 1
                    else "Which task space should I place the task in?"
                )
            elif "parentTask" in all_missing_fields:
                root_name = str(clean_drafts[0].get("rootTaskLevelName") or context.get("rootTaskLevelName") or context.get("root_task_level_name") or "project")
                summary_text = f"Under what {root_name} should I create the tasks?" if len(clean_drafts) > 1 else f"Under what {root_name} should I create the task?"
            elif "ticketSpace" in all_missing_fields:
                summary_text = (
                    "Which ticket space should I place the tickets?"
                    if len(clean_drafts) > 1
                    else "Which ticket space should I place the ticket?"
                )
            elif "name" in all_missing_fields:
                is_task = (
                    is_task_intent
                    or any(d.get("entityType") == "task" for d in clean_drafts)
                    or any(d.get("parentTaskId") or d.get("parentTaskName") for d in clean_drafts)
                    or bool(context.get("task_space_id") or context.get("taskSpaceId"))
                )
                if is_task:
                    first_draft = clean_drafts[0] if clean_drafts else {}
                    parent_info = first_draft.get("parentTaskName") or first_draft.get("parentTaskCode")
                    space_info = first_draft.get("taskSpaceName")
                    if parent_info:
                        summary_text = (
                            f"Here is your task draft structured under **{parent_info}**. "
                            "The existing parent task is shown above for hierarchy context. You can edit the task title, adjust its schedule, priority, and assignee directly in the table below before saving."
                        )
                    elif space_info:
                        summary_text = (
                            f"Here is your task draft in **{space_info}**. "
                            "You can provide a title, adjust its schedule, priority, and assignee directly in the table below before saving."
                        )
                    else:
                        summary_text = (
                            "Here is your prepared task draft. "
                            "You can name the task and configure its schedule, status, priority, and assignee directly in the table below before saving."
                        )
                else:
                    summary_text = (
                        "Here is your prepared draft. "
                        "You can provide a title and review or adjust its details directly in the table below before saving."
                    )
            else:
                summary_text = f"Please provide the missing information: {', '.join(all_missing_fields)}."
        else:
            if is_task_intent:
                if source_code:
                    summary_text = f"I've prepared a draft duplicating **{source_code}** with all its child tasks. Please review the details before proceeding."
                elif len(clean_drafts) > 1:
                    space = clean_drafts[0].get("taskSpaceName") if clean_drafts else None
                    if space:
                        summary_text = f"I've prepared {len(clean_drafts)} task drafts for you in '{space}'. Please review the details before proceeding."
                    else:
                        summary_text = f"I've prepared {len(clean_drafts)} task drafts for you. Please review the details before proceeding."
                else:
                    title = clean_drafts[0].get("name") if clean_drafts else "your task"
                    space = clean_drafts[0].get("taskSpaceName") if clean_drafts else None
                    if space:
                        summary_text = f"I've prepared your task draft '{title}' in '{space}'. Please review the details before proceeding."
                    else:
                        summary_text = f"I've prepared your task draft '{title}'. Please review the details before proceeding."
            elif source_code:
                summary_text = f"I've prepared a new ticket draft based on **{source_code}**. Please review the details before proceeding."
            elif len(clean_drafts) > 1:
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
                "needs_fields": needs_fields,
            },
        )

        primary_draft = clean_drafts[0] if clean_drafts else {}
        action_name = "CREATE_TASK" if is_task_intent else "CREATE_TICKET"
        entity_name = "task" if is_task_intent else "ticket"
        return {
            "draft": primary_draft,
            "drafts": clean_drafts,
            "pending_draft": clean_drafts if len(clean_drafts) > 1 else primary_draft,
            "is_complete": is_complete,
            "missing_fields": all_missing_fields,
            "needs_fields": needs_fields,
            "summary": summary_text,
            "automation_summary": summary_text,
            "automation_action": action_name,
            "entity_type": entity_name,
            "messages": [AIMessage(content=summary_text)],
        }

    return extract_delta_node
