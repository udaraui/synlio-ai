from langgraph.graph import StateGraph, START, END
from app.agent.automation.state import AutomationState
from app.agent.automation.nodes.orchestrator import make_automation_orchestrator_node
from app.agent.automation.nodes.extract_delta import make_extract_delta_node
from app.agent.automation.nodes.finalize import make_finalize_node
from app.agent.automation.nodes.extract_mutation import make_extract_mutation_node


def route_after_automation_orchestrator(state: AutomationState) -> str:
    """
    Routes from automation_orchestrator node to either:
    - 'extract_mutation' if user request is mutating an existing ticket or task
    - 'extract_delta' if user request is drafting/creating/cloning a ticket or task
    """
    action = state.get("automation_action", "CREATE_OR_CLONE")
    target = "extract_mutation" if action == "MUTATE_EXISTING" else "extract_delta"
    entity = state.get("entity_type", "task")
    print(f"[ROUTER: AUTOMATION] Target: {target} | Action: {action} | Entity: {entity}")
    return target


def route_automation_start(state: AutomationState) -> str:
    """Compatibility entrypoint if called directly as a conditional edge."""
    return route_after_automation_orchestrator(state)


def should_proceed_after_extract_delta(state: AutomationState) -> str:
    """If human inner loop is needed (incomplete draft), do not proceed after this node. Otherwise finalize."""
    is_complete = state.get("is_complete", False)
    target = "finalize" if is_complete else END
    print(f"[ROUTER: EXTRACT_DELTA] Target: {target} | Draft Complete: {is_complete}")
    return target


def make_automation_graph(llm, orchestrator_llm=None):
    """
    Compiles the Automation Subgraph:
    START -> automation_orchestrator -> [extract_mutation -> END] | [extract_delta -> finalize/END]
    """
    if orchestrator_llm is None:
        orchestrator_llm = llm

    workflow = StateGraph(AutomationState)

    automation_orchestrator_node = make_automation_orchestrator_node(orchestrator_llm)
    extract_delta_node = make_extract_delta_node(llm)
    finalize_node = make_finalize_node(llm)
    extract_mutation_node = make_extract_mutation_node(llm)

    workflow.add_node("automation_orchestrator", automation_orchestrator_node)
    workflow.add_node("extract_delta", extract_delta_node)
    workflow.add_node("finalize", finalize_node)
    workflow.add_node("extract_mutation", extract_mutation_node)

    # Edge from START to dedicated automation orchestrator
    workflow.add_edge(START, "automation_orchestrator")

    # Routing conditional edge from orchestrator
    workflow.add_conditional_edges(
        "automation_orchestrator",
        route_after_automation_orchestrator,
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
