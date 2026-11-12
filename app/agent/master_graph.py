import os
import time
import asyncio
from typing import Optional, Dict, Any, List
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_openai import ChatOpenAI
from langchain_core.messages import ToolMessage, HumanMessage, AIMessage

from app.agent.state import AgentState
from app.agent.telemetry import record_node_telemetry
from app.agent.tools import query_project_data, query_ticket_data, query_resource_data
from app.agent.gate_keeper import make_gate_keeper_node
from app.agent.dispatcher import make_dispatcher_node
from app.agent.automation import make_automation_graph
from app.agent.analytics import (
    make_analytics_graph,
    make_orchestrator_node,
    make_worker_node,
    make_formatter_node,
    make_tool_executor_node,
    route_after_orchestrator,
    route_after_domain_worker,
    route_after_tool_executor,
    VALID_DOMAINS,
)


def route_after_gate_keeper(state: AgentState) -> str:
    """
    Evaluates gate_keeper output:
    - If prompt injection flagged -> abort directly to END.
    - Otherwise -> proceed to the dedicated dispatcher node.
    """
    is_injection = state.get("is_injection", False)
    score = state.get("injection_score", 0.0)
    target = END if is_injection else "dispatcher"
    print(f"[ROUTER: GATEKEEPER] Target: {target} | Safety Score: {score:.2f} (Violation: {is_injection})")
    return target


def route_after_dispatcher(state: AgentState) -> str:
    """
    Evaluates dispatcher node output:
    - If intent is 'automation' -> route to automation subgraph.
    - If intent is 'analytics' -> route to orchestrator (or preset domain worker).
    - Otherwise (e.g. cancellation or general_chat) -> terminate at END.
    """
    intent = state.get("intent", "analytics")
    confidence = state.get("intent_confidence", 1.0)

    if intent == "automation":
        target = "automation"
    elif intent == "analytics":
        raw_workers = state.get("active_workers") or []
        active_workers = [w for w in raw_workers if w in VALID_DOMAINS]
        if active_workers:
            target = active_workers if len(active_workers) > 1 else active_workers[0]
        else:
            target = "orchestrator"
    else:
        target = END

    conf_str = f"Confidence: {confidence:.2f} | " if isinstance(confidence, (int, float)) else ""
    print(f"[ROUTER: DISPATCHER] Intent: {intent} | {conf_str}Target: {target}")
    return target


def create_master_graph(
    llm: ChatOpenAI,
    orchestrator_llm: Optional[ChatOpenAI] = None,
    formatter_llm: Optional[ChatOpenAI] = None,
):
    """
    Compiles the Master Graph with Gatekeeper Always First, Dispatcher,
    and both Automation and Analytics subgraphs:
    
    START -> gate_keeper -> dispatcher -> [automation | analytics (orchestrator -> workers -> tools -> formatter)] -> END
    """
    if orchestrator_llm is None:
        orchestrator_llm = llm
    if formatter_llm is None:
        formatter_llm = llm

    tools_by_name = {
        "query_project_data": query_project_data,
        "query_ticket_data": query_ticket_data,
        "query_resource_data": query_resource_data,
    }

    # 1. Gatekeeper & Dispatcher Nodes
    gate_keeper = make_gate_keeper_node(model_name=os.getenv("GATE_KEEPER_MODEL", "meta-llama/llama-3.1-8b-instruct"))
    dispatcher = make_dispatcher_node(orchestrator_llm)

    # 2. Automation Subgraph as a node
    automation_subgraph = make_automation_graph(llm, orchestrator_llm=orchestrator_llm)

    # 3. Analytics Nodes
    orchestrator = make_orchestrator_node(orchestrator_llm)
    project_worker = make_worker_node(llm, "project_analytics", query_project_data)
    ticket_worker = make_worker_node(llm, "ticket_analytics", query_ticket_data)
    resource_worker = make_worker_node(llm, "resource_analytics", query_resource_data)
    tool_executor = make_tool_executor_node(tools_by_name)
    formatter = make_formatter_node(formatter_llm)

    # Build Master Graph
    builder = StateGraph(AgentState)

    builder.add_node("gate_keeper", gate_keeper)
    builder.add_node("dispatcher", dispatcher)
    builder.add_node("automation", automation_subgraph)
    builder.add_node("orchestrator", orchestrator)
    builder.add_node("project_analytics", project_worker)
    builder.add_node("ticket_analytics", ticket_worker)
    builder.add_node("resource_analytics", resource_worker)
    builder.add_node("tool_executor", tool_executor)
    builder.add_node("formatter", formatter)

    # Master Routing Edges
    builder.add_edge(START, "gate_keeper")

    builder.add_conditional_edges(
        "gate_keeper",
        route_after_gate_keeper,
        {
            END: END,
            "dispatcher": "dispatcher",
        },
    )

    builder.add_conditional_edges(
        "dispatcher",
        route_after_dispatcher,
        {
            "automation": "automation",
            "orchestrator": "orchestrator",
            "project_analytics": "project_analytics",
            "ticket_analytics": "ticket_analytics",
            "resource_analytics": "resource_analytics",
            END: END,
        },
    )

    # Automation Subgraph outputs directly to END
    builder.add_edge("automation", END)

    # Analytics Subgraph internal routing (Preserved 100%)
    builder.add_conditional_edges(
        "orchestrator",
        route_after_orchestrator,
        {
            END: END,
            "project_analytics": "project_analytics",
            "ticket_analytics": "ticket_analytics",
            "resource_analytics": "resource_analytics",
        },
    )

    for domain in VALID_DOMAINS:
        builder.add_conditional_edges(
            domain,
            route_after_domain_worker,
            {
                "tool_executor": "tool_executor",
                END: END,
            },
        )

    builder.add_conditional_edges(
        "tool_executor",
        route_after_tool_executor,
        {
            "orchestrator": "orchestrator",
            "formatter": "formatter",
        },
    )

    builder.add_edge("formatter", END)

    return builder.compile(checkpointer=MemorySaver())
