import os
from typing import Optional
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from pydantic import BaseModel

from app.agent.state import AgentState
from app.agent.graph import create_graph

load_dotenv()
api_key = os.getenv("LLM_API_KEY")

class AgentResult(BaseModel):
    data: str

class InsightAgent:
    def __init__(self, key: str):
        self.llm = ChatOpenAI(
            model="openai/gpt-4o-mini",
            # model="nvidia/nemotron-3-nano-30b-a3b",
            # model="typesafe/jev-1.13",
            api_key=key,
            temperature=0,
            base_url="https://openrouter.ai/api/v1",
            streaming=True
        )
        
        self.orchestrator_llm = ChatOpenAI(
            model="typesafe/jev-1.13",
            api_key=os.getenv("JEFF_API_KEY", key),
            temperature=0,
            base_url="https://openrouter.ai/api/v1",
            streaming=True
        )
        
        # Build and compile the graph
        self.agent_executor = create_graph(self.llm, self.orchestrator_llm)

    async def arun(self, query: str) -> AgentResult:
        response = await self.agent_executor.ainvoke({"messages": [("user", query)]})
        output = response["messages"][-1].content
        if isinstance(output, list):
            text_parts = [block.get("text", "") for block in output if block.get("type") == "text"]
            output = "".join(text_parts)
        return AgentResult(data=str(output))

    async def astream_run(self, query: str):
        from app.agent.state import global_token_usage
        import time
        global_token_usage.set([0])
        start_time = time.time()
        
        table_content = ""
        yield "__REPLACE__Synlio is thinking"
        
        formatter_started = False
        
        # stream_mode multiple yields (stream_mode, data) in newer langgraph
        async for item in self.agent_executor.astream({"messages": [("user", query)]}, stream_mode=["updates", "messages"]):
            if isinstance(item, tuple) and len(item) == 2:
                mode, payload = item
                if mode == "updates":
                    for node_name, node_state in payload.items():
                        if node_name == "orchestrator":
                            plan = node_state.get("execution_plan", [])
                            if plan:
                                yield "__REPLACE__Figuring out where to look"
                        elif node_name == "parallel_workers":
                            plan = node_state.get("execution_plan", [])
                            if plan:
                                domains = [d.replace('_analytics', 's') for d in plan]
                                yield f"__REPLACE__Looking through {', '.join(domains)}"
                elif mode == "messages":
                    chunk, metadata = payload
                    if getattr(chunk, "type", "") == "tool" and chunk.content:
                        if "Query returned zero rows" not in chunk.content:
                            table_content = chunk.content
                            
                    node = metadata.get("langgraph_node")
                    if node == "formatter":
                        content = chunk.content
                        if isinstance(content, list):
                            parsed = [c.get("text", "") if isinstance(c, dict) else c for c in content]
                            content = "".join(parsed)
                        if content:
                            print(content, end="", flush=True)
                            if not formatter_started:
                                yield f"__FINAL__{content}"
                                formatter_started = True
                            else:
                                yield content
            else:
                # Fallback if old langgraph API
                pass
                    
        # If the formatter was bypassed, return the raw table data
        if not formatter_started:
            if table_content:
                yield f"__FINAL__{table_content}"
            else:
                yield "__FINAL__Execution completed with no data."
        else:
            pass
        elapsed = time.time() - start_time
        total_tokens = global_token_usage.get()[0]
        print(f"\n[REQUEST TOTALS] Time: {elapsed:.2f}s | Tokens: {total_tokens}\n")
# Initialize the global agent object for FastAPI to import
insight_agent = None
if api_key:
    try:
        insight_agent = InsightAgent(api_key)
    except Exception as e:
        print(f"Error initializing InsightAgent: {e}")
