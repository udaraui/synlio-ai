from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langchain_openai import ChatOpenAI

from app.agent.state import AgentState
from app.agent.tools import query_project_data, query_ticket_data, query_resource_data
from app.agent.workers import make_orchestrator_node, make_worker_node, make_formatter_node

def extract_results_node(state: AgentState):
    messages = state.get("messages", [])
    domain_results = {}
    
    tool_map = {
        "query_project_data": "project_analytics",
        "query_ticket_data": "ticket_analytics",
        "query_resource_data": "resource_analytics"
    }
    
    for msg in messages:
        if msg.type == "tool" and msg.name in tool_map:
            domain = tool_map[msg.name]
            try:
                import json
                domain_results[domain] = json.loads(msg.content)
            except Exception:
                domain_results[domain] = msg.content
                
    return {"domain_results": domain_results}

def create_graph(llm: ChatOpenAI, orchestrator_llm: ChatOpenAI = None):
    if orchestrator_llm is None:
        orchestrator_llm = llm
        
    orchestrator = make_orchestrator_node(orchestrator_llm)
    
    # Wrap workers for parallel execution
    project_worker = make_worker_node(llm, "project_analytics", query_project_data)
    ticket_worker = make_worker_node(llm, "ticket_analytics", query_ticket_data)
    resource_worker = make_worker_node(llm, "resource_analytics", query_resource_data)
    
    workers_map = {
        "project_analytics": project_worker,
        "ticket_analytics": ticket_worker,
        "resource_analytics": resource_worker
    }
    
    async def parallel_workers_node(state: AgentState, config):
        plan = state.get("execution_plan", [])
        if not plan:
            return {"messages": []}
            
        import asyncio
        tasks = [workers_map[domain](state, config) for domain in plan if domain in workers_map]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_messages = []
        for res in results:
            if isinstance(res, dict) and "messages" in res:
                all_messages.extend(res["messages"])
        return {"messages": all_messages}
        
    formatter = make_formatter_node(llm)
    tool_executor = ToolNode([query_project_data, query_ticket_data, query_resource_data])
    
    builder = StateGraph(AgentState)
    builder.add_node("orchestrator", orchestrator)
    builder.add_node("parallel_workers", parallel_workers_node)
    builder.add_node("tool_executor", tool_executor)
    builder.add_node("extract_results", extract_results_node)
    builder.add_node("formatter", formatter)
    
    builder.add_edge(START, "orchestrator")
    
    def route_after_orchestrator(state: AgentState):
        plan = state.get("execution_plan", [])
        if not plan:
            return "formatter"
        return "parallel_workers"
    builder.add_conditional_edges("orchestrator", route_after_orchestrator)
    
    def route_worker_action(state: AgentState):
        messages = state.get("messages", [])
        last_messages = messages[-len(state.get("execution_plan", [])):] if state.get("execution_plan") else []
        
        # Check if ANY of the generated messages from parallel_workers have tool_calls
        for msg in last_messages:
            if getattr(msg, "tool_calls", None):
                return "tool_executor"
        return "extract_results"
        
    builder.add_conditional_edges("parallel_workers", route_worker_action)
    builder.add_edge("tool_executor", "extract_results")
    builder.add_edge("extract_results", "formatter")
    builder.add_edge("formatter", END)
    
    return builder.compile()
