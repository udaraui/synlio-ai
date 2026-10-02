from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langchain_openai import ChatOpenAI

from app.agent.state import AgentState
from app.agent.tools import query_project_data, query_ticket_data, query_resource_data
from app.agent.workers import make_orchestrator_node, make_worker_node, make_formatter_node

def advance_step_node(state: AgentState):
    messages = state.get("messages", [])
    last_message = messages[-1] if messages else None
    
    domain_results = state.get("domain_results", {}) or {}
    plan = state.get("execution_plan", [])
    current = state.get("current_step", 0)
    
    if current < len(plan) and last_message and last_message.type == "tool":
        domain = plan[current]
        try:
            # Parse the JSON string so it doesn't get double-escaped later
            import json
            domain_results[domain] = json.loads(last_message.content)
        except Exception:
            domain_results[domain] = last_message.content
            
    return {
        "current_step": current + 1,
        "domain_results": domain_results
    }

def create_graph(llm: ChatOpenAI):
    # 1. Build Nodes
    orchestrator = make_orchestrator_node(llm)
    project_worker = make_worker_node(llm, "project_analytics", query_project_data)
    ticket_worker = make_worker_node(llm, "ticket_analytics", query_ticket_data)
    resource_worker = make_worker_node(llm, "resource_analytics", query_resource_data)
    formatter = make_formatter_node(llm)
    
    # LangGraph's ToolNode for executing the bound tools
    tool_executor = ToolNode([query_project_data, query_ticket_data, query_resource_data])
    
    # 2. Build Graph
    builder = StateGraph(AgentState)
    
    builder.add_node("orchestrator", orchestrator)
    builder.add_node("project_agent", project_worker)
    builder.add_node("ticket_agent", ticket_worker)
    builder.add_node("resource_agent", resource_worker)
    builder.add_node("tool_executor", tool_executor)
    builder.add_node("advance_step", advance_step_node)
    builder.add_node("formatter", formatter)
    
    # 3. Define Edges and Routing
    builder.add_edge(START, "orchestrator")
    
    def route_domain(state: AgentState):
        plan = state.get("execution_plan", [])
        current = state.get("current_step", 0)
        
        if current < len(plan):
            domain = plan[current]
            if domain == "ticket_analytics":
                return "ticket_agent"
            elif domain == "resource_analytics":
                return "resource_agent"
            return "project_agent"
        # return END
        return "formatter"
    builder.add_conditional_edges("orchestrator", route_domain)
    
    def route_worker_action(state: AgentState):
        messages = state.get("messages", [])
        last_message = messages[-1] if messages else None
        if last_message and getattr(last_message, "tool_calls", None):
            return "tool_executor"
        return "advance_step"
        
    builder.add_conditional_edges("project_agent", route_worker_action)
    builder.add_conditional_edges("ticket_agent", route_worker_action)
    builder.add_conditional_edges("resource_agent", route_worker_action)
    
    # After tool execution, go advance the step
    builder.add_edge("tool_executor", "advance_step")
    
    # After advancing, loop back to the router to check the next domain
    builder.add_conditional_edges("advance_step", route_domain)
    
    builder.add_edge("formatter", END)
    
    # Compile and return the executable graph
    return builder.compile()
