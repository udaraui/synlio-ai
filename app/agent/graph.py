import os
import time
import asyncio
from typing import Optional, Dict, Any, List
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_openai import ChatOpenAI
from langchain_core.messages import ToolMessage, HumanMessage
from app.agent.state import AgentState
from app.agent.telemetry import record_node_telemetry
from app.agent.tools import query_project_data, query_ticket_data, query_resource_data
from app.agent.workers import (
    make_gate_keeper_node,
    make_orchestrator_node,
    make_worker_node,
    make_formatter_node,
)

# =============================================================================
# 1. CONSTANTS & CONFIGURATION
# =============================================================================

QUERY_ROUND_COUNT_RAW = os.getenv("QUERY_ROUND_COUNT")
if not QUERY_ROUND_COUNT_RAW:
    print("[ENV ERROR] QUERY_ROUND_COUNT is not set in environment (.env)!")
MAX_QUERY_ROUNDS = int(QUERY_ROUND_COUNT_RAW) if QUERY_ROUND_COUNT_RAW else 1

FORCE_KEYWORDS = (
    "force", "full list", "complete list", "all records",
    "bring all data", "show all", "view all", "see all",
    "all data", "all tasks", "all tickets"
)

EMPTY_RESULT_SIGNALS = (
    "zero rows",
    "records[0]: []",
    "[0]: []",
    "error",
    "issue while trying",
)

VALID_DOMAINS = {"project_analytics", "ticket_analytics", "resource_analytics"}

# =============================================================================
# 2. PURE HELPER FUNCTIONS
# =============================================================================

def _detect_force_intent(messages: List[Any]) -> bool:
    """Detects if the user requested the full/unfiltered dataset in their latest prompt."""
    human_msgs = [m for m in messages if isinstance(m, HumanMessage) or getattr(m, "type", "") == "human"]
    if not human_msgs or not human_msgs[-1].content:
        return False
    latest_query = str(human_msgs[-1].content).lower()
    return any(k in latest_query for k in FORCE_KEYWORDS)

def _has_valid_query_results(tool_messages: List[ToolMessage]) -> bool:
    """Evaluates whether tool results contained valid data or if they were empty/errors."""
    if not tool_messages:
        return False
    for m in tool_messages:
        content = (getattr(m, "content", "") or "").strip().lower()
        if not content:
            continue
        if any(signal in content for signal in EMPTY_RESULT_SIGNALS) or ("tool" in content and "not found" in content):
            continue
        return True
    return False

# =============================================================================
# 3. TOOL EXECUTOR NODE FACTORY
# =============================================================================

def make_tool_executor_node(tools_by_name: Dict[str, Any]):
    """
    Creates the tool executor node that gathers tool calls from domain workers,
    injects authenticated company/scope/context, runs tools concurrently via asyncio,
    and manages the retry loop with telemetry tracking.
    """
    async def custom_tool_executor_node(state: AgentState):
        start_time = time.time()
        messages = state.get("messages", [])
        active_workers = state.get("active_workers") or []

        recent_count = len(active_workers) if active_workers else 1
        recent_messages = messages[-recent_count:]
        user_wants_force = _detect_force_intent(messages)

        all_tool_calls = []
        for msg in recent_messages:
            if getattr(msg, "tool_calls", None):
                for tc in msg.tool_calls:
                    all_tool_calls.append(tc)

        async def run_tool(tc):
            tool_name = tc.get("name")
            args = dict(tc.get("args", {}))
            tool_call_id = tc.get("id")
            tool_func = tools_by_name.get(tool_name)

            if user_wants_force:
                args["force"] = True

            # Inject authenticated security context into tool execution
            if state.get("company_id") is not None:
                args["company_id"] = state.get("company_id")
            if state.get("scope") is not None:
                args["scope"] = state.get("scope")
            if state.get("context") is not None:
                args["context"] = state.get("context")

            if tool_func:
                try:
                    result = await tool_func.ainvoke(args)
                except Exception:
                    try:
                        result = tool_func.invoke(args)
                    except Exception as inner_e:
                        result = f"Error executing {tool_name}: {inner_e}"
            else:
                result = f"Tool {tool_name} not found."

            return ToolMessage(
                content=str(result),
                name=tool_name,
                tool_call_id=tool_call_id
            )

        tool_messages = await asyncio.gather(*[run_tool(tc) for tc in all_tool_calls])
        current_round = state.get("current_round", 1) or 1
        has_valid = _has_valid_query_results(tool_messages)
        tool_elapsed = time.time() - start_time

        executed_names = [getattr(tm, "name", "") for tm in tool_messages]
        record_node_telemetry(
            node="tool_executor",
            latency_ms=int(tool_elapsed * 1000),
            in_token=0,
            out_token=0,
            cache_token=0,
            total_tokens=0,
            model=None,
            status="SUCCESS",
            input_data={"toolsToExecute": [{"name": tc.get("name"), "args": tc.get("args")} for tc in all_tool_calls]},
            output_data={
                "toolsExecuted": executed_names,
                "rowsReturned": len(tool_messages),
                "needsRetry": not has_valid and current_round < MAX_QUERY_ROUNDS,
                "dataSummary": f"Retrieved results across {len(tool_messages)} tool executions." if has_valid else "Query returned no results.",
            },
            extra_summary=f"Executed: {executed_names} ({len(tool_messages)} results)",
        )

        if not has_valid and current_round < MAX_QUERY_ROUNDS:
            failure_reasons = [f"{tm.name}: {tm.content}" for tm in tool_messages]
            feedback = "; ".join(failure_reasons) if failure_reasons else "Query returned no results."

            print(f"[RETRY] Query returned no results or failed: {feedback}")
            print(f"[RETRY] Running another round from the top (Round {current_round + 1} of {MAX_QUERY_ROUNDS})...\n")

            return {
                "messages": list(tool_messages),
                "current_round": current_round + 1,
                "retry_feedback": feedback,
                "needs_retry": True
            }
        else:
            if not has_valid and current_round >= MAX_QUERY_ROUNDS:
                print(f"[RETRY] Reached maximum query rounds ({MAX_QUERY_ROUNDS}). Forwarding to Formatter.\n")
            return {
                "messages": list(tool_messages),
                "needs_retry": False
            }

    return custom_tool_executor_node

# =============================================================================
# 4. ROUTING FUNCTIONS
# =============================================================================

def route_after_gate_keeper(state: AgentState):
    """
    Evaluates gate_keeper output:
    - If prompt injection flagged -> abort to END.
    - If active workers already preset -> bypass orchestrator directly to domain worker(s).
    - Otherwise -> proceed to orchestrator.
    """
    if state.get("is_injection"):
        print("[gate_keeper ROUTING] Malicious prompt flagged. Terminating graph directly at END.")
        return END
    raw_workers = state.get("active_workers") or []
    active_workers = [w for w in raw_workers if w in VALID_DOMAINS]
    if active_workers:
        print(f"\n[ORCHESTRATOR BYPASS] Active workers preset: {active_workers}. Direct dispatch to domain workers!")
        return active_workers if len(active_workers) > 1 else active_workers[0]
    return "orchestrator"

def route_after_orchestrator(state: AgentState):
    """Routes to active domain workers chosen by the orchestrator, or END if unclassified."""
    raw_workers = state.get("active_workers") or []
    active_workers = [w for w in raw_workers if w in VALID_DOMAINS]
    if not active_workers:
        return END
    return active_workers if len(active_workers) > 1 else active_workers[0]

def route_after_domain_worker(state: AgentState) -> str:
    """Routes domain worker output to tool_executor if tool calls exist, else terminates."""
    messages = state.get("messages", [])
    if messages:
        last_msg = messages[-1]
        if getattr(last_msg, "tool_calls", None):
            return "tool_executor"
    return END

def route_after_tool_executor(state: AgentState) -> str:
    """Routes to orchestrator if retry is needed, else proceeds to the formatter."""
    if state.get("needs_retry"):
        return "orchestrator"
    return "formatter"

# =============================================================================
# 5. GRAPH BUILDER & COMPILER
# =============================================================================

def create_graph(
    llm: ChatOpenAI, 
    orchestrator_llm: Optional[ChatOpenAI] = None, 
    formatter_llm: Optional[ChatOpenAI] = None
):
    """Compiles the 7-node LangGraph state graph with routing and memory checkpointing."""
    if orchestrator_llm is None:
        orchestrator_llm = llm
    if formatter_llm is None:
        formatter_llm = llm

    tools_by_name = {
        "query_project_data": query_project_data,
        "query_ticket_data": query_ticket_data,
        "query_resource_data": query_resource_data,
    }

    # Initialize Nodes
    gate_keeper = make_gate_keeper_node(orchestrator_llm)
    orchestrator = make_orchestrator_node(orchestrator_llm)
    project_worker = make_worker_node(llm, "project_analytics", query_project_data)
    ticket_worker = make_worker_node(llm, "ticket_analytics", query_ticket_data)
    resource_worker = make_worker_node(llm, "resource_analytics", query_resource_data)
    tool_executor = make_tool_executor_node(tools_by_name)
    formatter = make_formatter_node(formatter_llm)

    # Build Graph Nodes
    builder = StateGraph(AgentState)
    builder.add_node("gate_keeper", gate_keeper)
    builder.add_node("orchestrator", orchestrator)
    builder.add_node("project_analytics", project_worker)
    builder.add_node("ticket_analytics", ticket_worker)
    builder.add_node("resource_analytics", resource_worker)
    builder.add_node("tool_executor", tool_executor)
    builder.add_node("formatter", formatter)

    # Edges & Conditional Routing
    builder.add_edge(START, "gate_keeper")

    builder.add_conditional_edges(
        "gate_keeper",
        route_after_gate_keeper,
        {
            END: END,
            "orchestrator": "orchestrator",
            "project_analytics": "project_analytics",
            "ticket_analytics": "ticket_analytics",
            "resource_analytics": "resource_analytics",
        },
    )

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
