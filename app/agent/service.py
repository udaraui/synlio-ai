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
    def __init__(self, key: str, worker_model: Optional[str] = None, orchestrator_model: Optional[str] = None):
        self.worker_model = worker_model or os.getenv("WORKER_MODEL", "google/gemini-2.5-flash-lite")
        self.orchestrator_model = orchestrator_model or os.getenv("ORCHESTRATOR_MODEL", "typesafe/jev-1.13")

        # Worker LLM: streaming=False prevents OpenRouter SSE stream hangs on tool calling
        self.worker_llm = ChatOpenAI(
            model=self.worker_model,
            api_key=key,
            temperature=0,
            base_url="https://openrouter.ai/api/v1",
            streaming=False
        )

        # Formatter LLM: streaming=True enables token-by-token streaming to the UI
        self.formatter_llm = ChatOpenAI(
            model=self.worker_model,
            api_key=key,
            temperature=0,
            base_url="https://openrouter.ai/api/v1",
            streaming=True
        )
        
        self.orchestrator_llm = ChatOpenAI(
            model=self.orchestrator_model,
            api_key=key,
            temperature=0,
            base_url="https://openrouter.ai/api/v1",
            streaming=False
        )

        self.llm = self.worker_llm
        
        # Build and compile the graph
        self.agent_executor = create_graph(
            llm=self.worker_llm,
            orchestrator_llm=self.orchestrator_llm,
            formatter_llm=self.formatter_llm
        )

    async def arun(
        self,
        query: str,
        conversation_id: Optional[str] = None,
        user_id: Optional[int] = None,
        user_email: Optional[str] = None,
        company_id: Optional[int] = None,
        scope: Optional[dict] = None,
    ) -> AgentResult:
        config = {"configurable": {"thread_id": conversation_id or "default"}}
        input_state = {
            "messages": [("user", query)],
            "user_id": user_id,
            "user_email": user_email,
            "company_id": company_id,
            "scope": scope,
            "error_message": None,
        }
        response = await self.agent_executor.ainvoke(input_state, config=config)
        output = response["messages"][-1].content
        if isinstance(output, list):
            text_parts = [block.get("text", "") for block in output if block.get("type") == "text"]
            output = "".join(text_parts)
        return AgentResult(data=str(output))

    async def astream_run(
        self,
        query: str,
        conversation_id: Optional[str] = None,
        user_id: Optional[int] = None,
        user_email: Optional[str] = None,
        company_id: Optional[int] = None,
        scope: Optional[dict] = None,
    ):
        from app.agent.state import global_token_usage
        import time
        global_token_usage.set([0, 0, 0])
        start_time = time.time()
        
        table_content = ""
        yield "__REPLACE__Synlio is thinking"
        
        formatter_started = False
        config = {"configurable": {"thread_id": conversation_id or "default"}}
        
        input_state = {
            "messages": [("user", query)],
            "user_id": user_id,
            "user_email": user_email,
            "company_id": company_id,
            "scope": scope,
            "error_message": None,
        }
        
        # stream_mode multiple yields (stream_mode, data) in newer langgraph
        async for item in self.agent_executor.astream(input_state, config=config, stream_mode=["messages"]):
            if isinstance(item, tuple) and len(item) == 2:
                mode, payload = item
                if mode == "messages":
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
