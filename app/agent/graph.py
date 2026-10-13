import os
import asyncio
from typing import cast
from langgraph.graph import StateGraph, START, END
from langchain_openai import ChatOpenAI
from langchain_core.messages import ToolMessage

from app.agent.state import AgentState
from app.agent.tools import query_project_data, query_ticket_data, query_resource_data
from app.agent.workers import make_orchestrator_node, make_worker_node, make_formatter_node

def create_graph(llm: ChatOpenAI, orchestrator_llm: ChatOpenAI = None):
    if orchestrator_llm is None:
        orchestrator_llm = llm
        
    orchestrator = make_orchestrator_node(orchestrator_llm)
    
    # Domain Workers
    project_worker = make_worker_node(llm, "project_analytics", query_project_data)
    ticket_worker = make_worker_node(llm, "ticket_analytics", query_ticket_data)
    resource_worker = make_worker_node(llm, "resource_analytics", query_resource_data)
    
    workers_map = {
        "project_analytics": project_worker,
        "ticket_analytics": ticket_worker,
        "resource_analytics": resource_worker
    }
    
    async def parallel_workers_node(state: AgentState, config):
        active_workers = state.get("active_workers", [])
        if not active_workers:
            return {"messages": []}
            
        tasks = [workers_map[domain](state, config) for domain in active_workers if domain in workers_map]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_messages = []
        for res in results:
            if isinstance(res, dict) and "messages" in res:
                all_messages.extend(res["messages"])
        return {"messages": all_messages}
        
    tools_by_name = {
        "query_project_data": query_project_data,
        "query_ticket_data": query_ticket_data,
        "query_resource_data": query_resource_data,
    }

    async def custom_tool_executor_node(state: AgentState):
        messages = state.get("messages", [])
        active_workers = state.get("active_workers", [])
        
        recent_count = len(active_workers) if active_workers else 1
        recent_messages = messages[-recent_count:]
        
        # Check if the user requested the full list (or used force) in their latest message
        human_msgs = [m for m in messages if getattr(m, "type", "") == "human"]
        latest_user_query = str(human_msgs[-1].content).lower() if human_msgs and human_msgs[-1].content else ""
        full_list_keywords = ["force", "full list", "complete list", "all records", "bring all data", "show all", "view all", "see all", "all data", "all tasks", "all tickets"]
        user_wants_force = any(k in latest_user_query for k in full_list_keywords)
        
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
            
            # If user explicitly specified force in their prompt, ensure force=True is passed
            if user_wants_force:
                args["force"] = True
            
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

        def _has_valid_query_results(msgs):
            if not msgs:
                return False
            for m in msgs:
                c = (getattr(m, "content", "") or "").strip().lower()
                if not c:
                    continue
                if not (
                    "zero rows" in c
                    or "records[0]: []" in c
                    or "[0]: []" in c
                    or "error" in c
                    or "issue while trying" in c
                    or ("tool" in c and "not found" in c)
                ):
                    return True
            return False

        max_rounds = int(os.getenv("QUERY_ROUND_COUNT", 2))
        current_round = state.get("current_round", 1) or 1
        has_valid = _has_valid_query_results(tool_messages)

        if not has_valid and current_round < max_rounds:
            failure_reasons = []
            for tm in tool_messages:
                failure_reasons.append(f"{tm.name}: {tm.content}")
            feedback = "; ".join(failure_reasons) if failure_reasons else "Query returned no results."

            print(f"[RETRY] Query returned no results or failed: {feedback}")
            print(f"[RETRY] Running another round from the top (Round {current_round + 1} of {max_rounds})...\n")

            return {
                "messages": list(tool_messages),
                "current_round": current_round + 1,
                "retry_feedback": feedback,
                "needs_retry": True
            }
        else:
            if not has_valid and current_round >= max_rounds:
                print(f"[RETRY] Reached maximum query rounds ({max_rounds}). Forwarding to Formatter.\n")
            return {
                "messages": list(tool_messages),
                "needs_retry": False
            }

    formatter = make_formatter_node(llm)
    
    # -------------------------------------------------------------------------
    # Graph Construction (4 Nodes)
    # -------------------------------------------------------------------------
    builder = StateGraph(AgentState)
    builder.add_node("orchestrator", orchestrator)
    builder.add_node("workers", parallel_workers_node)
    builder.add_node("tool_executor", custom_tool_executor_node)
    builder.add_node("formatter", formatter)
    
    builder.add_edge(START, "orchestrator")
    
    # -------------------------------------------------------------------------
    # Routing Functions & Conditional Edges
    # -------------------------------------------------------------------------
    def route_after_orchestrator(state: AgentState) -> str:
        active_workers = state.get("active_workers", [])
        return "workers" if active_workers else "formatter"

    builder.add_conditional_edges(
        "orchestrator",
        route_after_orchestrator,
        {
            "workers": "workers",
            "formatter": "formatter",
        },
    )

    def route_after_workers(state: AgentState) -> str:
        active_workers = state.get("active_workers", [])
        messages = state.get("messages", [])
        recent_count = len(active_workers) if active_workers else 1
        recent_messages = messages[-recent_count:]

        # Check if ANY worker generated tool calls
        for msg in recent_messages:
            if getattr(msg, "tool_calls", None):
                return "tool_executor"
        return "formatter"

    builder.add_conditional_edges(
        "workers",
        route_after_workers,
        {
            "tool_executor": "tool_executor",
            "formatter": "formatter",
        },
    )

    def route_after_tool_executor(state: AgentState) -> str:
        if state.get("needs_retry"):
            return "orchestrator"
        return "formatter"

    builder.add_conditional_edges(
        "tool_executor",
        route_after_tool_executor,
        {
            "orchestrator": "orchestrator",
            "formatter": "formatter",
        },
    )
    builder.add_edge("formatter", END)

    from langgraph.checkpoint.memory import MemorySaver
    checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)
