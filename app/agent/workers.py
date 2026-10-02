import json
import time
from typing import Literal
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from app.agent.state import AgentState, global_token_usage
from app.agent.prompts import ORCHESTRATOR_SYSTEM_PROMPT, FORMATTER_PROMPT, get_worker_prompt

def print_usage(node_name: str, response, elapsed: float = 0.0):
    total = 0
    try:
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            usage = getattr(response, "response_metadata", {}).get("token_usage", {})
        
        model_name = getattr(response, "response_metadata", {}).get("model_name", "unknown")
        
        # LangChain streaming bug: merges string metadata across chunks, doubling the model name
        if isinstance(model_name, str):
            half = len(model_name) // 2
            if half > 0 and model_name[:half] == model_name[half:]:
                model_name = model_name[:half]
        
        if usage:
            in_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
            out_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
            total = usage.get("total_tokens", 0)
            time_str = f" | Time: {elapsed:.2f}s" if elapsed > 0 else ""
            print(f"Token Usage - In: {in_tokens} | Out: {out_tokens} | Total: {total}{time_str}")
            global_token_usage.get()[0] += total
            
            # Log to CSV
            import os
            import csv
            from datetime import datetime
            
            csv_file = "llm_benchmark_logs.csv"
            file_exists = os.path.isfile(csv_file)
            
            with open(csv_file, mode='a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["Timestamp", "Node", "Model", "In Tokens", "Out Tokens", "Total Tokens", "Time (s)"])
                
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    node_name,
                    model_name,
                    in_tokens,
                    out_tokens,
                    total,
                    f"{elapsed:.2f}"
                ])
    except Exception as e:
        print(f"Error logging usage: {e}")
    return total

class OrchestrationPlan(BaseModel):
    execution_plan: list[Literal["project_analytics", "ticket_analytics", "resource_analytics"]] = Field(
        description="The sequence of domains/modules to route this query to, in order, based on the semantic layer."
    )

def make_orchestrator_node(llm: ChatOpenAI):
    async def orchestrator_node(state: AgentState, config: RunnableConfig):
        messages = state.get("messages", [])
        last_message = messages[-1].content if messages else ""
        # Raw text output for maximum speed (no JSON or Pydantic overhead)
        try:
            print("[ORCHESTRATOR]=======================================================")
            print("Analyzing prompt and extracting semantic context...")
            start_time = time.time()
            result = await llm.ainvoke([
                SystemMessage(content=ORCHESTRATOR_SYSTEM_PROMPT),
                HumanMessage(content=str(last_message))
            ], config=config)
            
            raw_text = result.content.strip()
            
            # Simple text parsing: e.g. 'project_analytics, ticket_analytics'
            domains = []
            for d in ["project_analytics", "ticket_analytics", "resource_analytics"]:
                if d in raw_text:
                    domains.append(d)
            
            print(f"Execution Plan: {domains}")
            elapsed = time.time() - start_time
            print_usage("ORCHESTRATOR", result, elapsed)
            print()
            
            return {
                "execution_plan": domains,
                "current_step": 0,
                "domain_results": {}
            }
        except Exception as e:
            print(f"Error parsing orchestration plan: {e}")
            return {
                "execution_plan": [],
                "current_step": 0,
                "domain_results": {}
            }
    return orchestrator_node

def make_worker_node(llm: ChatOpenAI, domain: str, tool_func):
    async def worker_node(state: AgentState, config: RunnableConfig):
        domain_results = state.get("domain_results", {})
        
        system_content = get_worker_prompt(domain)
        if domain_results:
            system_content += f"\n\nPREVIOUS DOMAIN RESULTS: {json.dumps(domain_results)}"
        
        # Bind the specialized tool and force it to be called
        llm_with_tools = llm.bind_tools([tool_func], tool_choice="any")
        
        # Extract only the last user query to save tokens
        human_msgs = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        user_query = human_msgs[-1].content if human_msgs else ""
        
        messages = [SystemMessage(content=system_content), HumanMessage(content=str(user_query))]
        
        print(f"[{domain.upper()}]==================================================")
        print("Generating query...")
        start_time = time.time()
        response = await llm_with_tools.ainvoke(messages, config=config)
        elapsed = time.time() - start_time
        
        # Log the output tool calls or response
        if getattr(response, "tool_calls", None):
            print(f"Tool Called: {response.tool_calls[0]['name']}")
            print(f"Arguments: {response.tool_calls[0]['args']}")
        else:
            print(f"Output: {response.content[:200]}...")
            
        print_usage(domain, response, elapsed)
        print()
        
        return {"messages": [response]}
    return worker_node

def make_formatter_node(llm: ChatOpenAI):
    async def formatter_node(state: AgentState, config: RunnableConfig):
        human_msgs = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        user_query = human_msgs[-1].content if human_msgs else ""
        domain_results = state.get("domain_results", {})
        execution_plan = state.get("execution_plan", [])
        
        print("[FORMATTER]==========================================")
        print("Formatting final response...")
        start_time = time.time()
        
        if not execution_plan and not domain_results:
            response_text = "I'm sorry, I couldn't understand your request or map it to a specific analytics domain. Could you please rephrase?"
            response = AIMessage(content=response_text)
            print("Completed! (Fallback Error)")
            print()
            return {"messages": [response]}
        
        # print(f"DEBUG - Domain Results going to Formatter: {json.dumps(domain_results)[:500]}")
        content = FORMATTER_PROMPT + f"\n\nUSER QUERY: {user_query}\n\nDOMAIN RESULTS: {json.dumps(domain_results)}"
        
        response = await llm.ainvoke([
            SystemMessage(content=content),
            HumanMessage(content=str(user_query))
        ], config=config)
        elapsed = time.time() - start_time
        
        print("Completed!")
        print_usage("FORMATTER", response, elapsed)
        print()
        
        return {"messages": [response]}
    return formatter_node
