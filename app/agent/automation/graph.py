from langgraph.graph import StateGraph, START, END
from app.agent.automation.state import AutomationState
from app.agent.automation.nodes.extract_delta import make_extract_delta_node
from app.agent.automation.nodes.finalize import make_finalize_node


def should_proceed_after_extract_delta(state: AutomationState) -> str:
    """If human inner loop is needed (incomplete draft), do not proceed after this node. Otherwise finalize."""
    if state.get("is_complete", False):
        return "finalize"
    return END


def make_automation_graph(llm):
    """
    Compiles the Automation Subgraph:
    START -> extract_delta -> [finalize | END]
    """
    workflow = StateGraph(AutomationState)

    extract_delta_node = make_extract_delta_node(llm)
    finalize_node = make_finalize_node(llm)

    workflow.add_node("extract_delta", extract_delta_node)
    workflow.add_node("finalize", finalize_node)

    workflow.add_edge(START, "extract_delta")
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
