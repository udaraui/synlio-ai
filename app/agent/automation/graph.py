import re
from langgraph.graph import StateGraph, START, END
from langchain_core.messages import HumanMessage
from app.agent.automation.state import AutomationState
from app.agent.automation.nodes.extract_delta import make_extract_delta_node
from app.agent.automation.nodes.finalize import make_finalize_node
from app.agent.automation.nodes.extract_mutation import make_extract_mutation_node


def route_automation_start(state: AutomationState) -> str:
    """
    Routes from START to either:
    - 'extract_mutation' if user request is mutating an existing ticket or task
    - 'extract_delta' if user request is drafting/creating a new ticket
    """
    if state.get("mutation_payload") or state.get("automation_action") == "UPDATE_TICKET":
        return "extract_mutation"

    messages = state.get("messages", [])
    user_msg = ""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage) or getattr(msg, "type", "") in ("human", "user"):
            user_msg = msg.content
            break

    user_msg_clean = user_msg.strip().lower()

    # 1. Prioritize creation/drafting intent over mutation
    creation_verbs = [
        "create", "draft", "make a ticket", "make ticket", "new ticket",
        "add ticket", "file a bug", "file bug", "file ticket", "file a ticket",
        "raise ticket", "raise a ticket", "log ticket", "log a ticket",
        "clone", "duplicate", "copy", "same as"
    ]
    has_creation_intent = any(cv in user_msg_clean for cv in creation_verbs)
    has_ticket_code = bool(re.search(r'\b[A-Za-z0-9]+-\d+\b', user_msg))
    has_task_code = bool(re.search(r'\b[A-Za-z0-9]+-[A-Za-z]+[A-Za-z0-9_-]*\b', user_msg))
    has_any_code = has_ticket_code or has_task_code

    mutation_verbs = [
        "move", "change status", "change the status", "update status", "update the status",
        "update ticket", "update task", "update", "assign", "reassign", "set severity", "set status",
        "set priority", "rename", "mark as", "mark ticket", "mark task", "close ticket", "close task",
        "set all", "all tickets", "all tasks", "tickets assigned", "tasks assigned", "tickets for", "tasks for",
        "move all", "reassign all"
    ]
    has_mutation_intent = any(v in user_msg_clean for v in mutation_verbs)

    # 1. CRITICAL: If pending_draft is present and user did NOT issue an explicit mutation verb,
    # the user is answering a disambiguation question or continuing the creation draft (e.g. selecting a project code badge)!
    if state.get("pending_draft") and not has_mutation_intent:
        return "extract_delta"

    # 2. Prioritize creation/drafting intent over mutation
    if has_creation_intent and not (has_any_code and has_mutation_intent):
        return "extract_delta"

    # 3. Check for explicit mutation verbs
    if has_mutation_intent:
        return "extract_mutation"

    # 4. If user references a specific ticket or task code without creation keywords and without an active draft
    if has_any_code:
        return "extract_mutation"

    return "extract_delta"

    return "extract_delta"

def should_proceed_after_extract_delta(state: AutomationState) -> str:
    """If human inner loop is needed (incomplete draft), do not proceed after this node. Otherwise finalize."""
    if state.get("is_complete", False):
        return "finalize"
    return END


def make_automation_graph(llm):
    """
    Compiles the Automation Subgraph:
    START -> [extract_mutation -> END] | [extract_delta -> finalize/END]
    """
    workflow = StateGraph(AutomationState)

    extract_delta_node = make_extract_delta_node(llm)
    finalize_node = make_finalize_node(llm)
    extract_mutation_node = make_extract_mutation_node(llm)

    workflow.add_node("extract_delta", extract_delta_node)
    workflow.add_node("finalize", finalize_node)
    workflow.add_node("extract_mutation", extract_mutation_node)

    workflow.add_conditional_edges(
        START,
        route_automation_start,
        {
            "extract_mutation": "extract_mutation",
            "extract_delta": "extract_delta",
        }
    )

    workflow.add_edge("extract_mutation", END)

    workflow.add_conditional_edges(
        "extract_delta",
        should_proceed_after_extract_delta,
        {
            "finalize": "finalize",
            END: END,
        }
    )
    workflow.add_edge("finalize", END)

    return workflow.compile()
