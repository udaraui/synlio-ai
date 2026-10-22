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
        self.orchestrator_model = orchestrator_model or os.getenv("ORCHESTRATOR_MODEL", "typesafe/jev-1.14")

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
        agent: Optional[str] = None,
        context: Optional[dict] = None,
        scope: Optional[dict] = None,
    ) -> AgentResult:
        config = {"configurable": {"thread_id": conversation_id or "default"}}
        active_workers = [agent] if agent else None
        input_state = {
            "messages": [("user", query)],
            "user_id": user_id,
            "user_email": user_email,
            "company_id": company_id,
            "scope": scope,
            "context": context,
            "active_workers": active_workers,
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
        request_id: Optional[str] = None,
        chat_mode: Optional[str] = "panel",
        user_id: Optional[int] = None,
        user_email: Optional[str] = None,
        user_name: Optional[str] = None,
        company_id: Optional[int] = None,
        agent: Optional[str] = None,
        context: Optional[dict] = None,
        scope: Optional[dict] = None,
    ):
        import uuid
        import time
        from app.agent.state import global_token_usage
        from app.agent.telemetry import init_telemetry, get_telemetry_tracker, dispatch_audit_telemetry

        init_telemetry()
        if not request_id:
            request_id = str(uuid.uuid4())

        global_token_usage.set([0, 0, 0])
        start_time = time.time()
        final_response_parts = []
        
        domain_display_names = {
            "resource_analytics": "resource workload",
            "project_analytics": "projects",
            "ticket_analytics": "tickets",
        }
        
        table_content = ""
        if agent and agent in domain_display_names:
            yield f"__REPLACE__Gathering details on {domain_display_names[agent]}..."
        else:
            yield "__REPLACE__Checking what you need..."
        
        formatter_started = False
        config = {"configurable": {"thread_id": conversation_id or "default"}}
        
        active_workers = [agent] if agent else None
        input_state = {
            "messages": [("user", query)],
            "user_id": user_id,
            "user_email": user_email,
            "company_id": company_id,
            "scope": scope,
            "context": context,
            "active_workers": active_workers,
            "error_message": None,
        }

        try:
            # stream_mode multiple yields (stream_mode, data) in newer langgraph
            async for item in self.agent_executor.astream(input_state, config=config, stream_mode=["updates", "messages"]):
                if isinstance(item, tuple) and len(item) == 2:
                    mode, payload = item
                    if mode == "updates" and isinstance(payload, dict):
                        for node_name, node_output in payload.items():
                            if node_name == "gate_keeper":
                                if node_output.get("is_injection"):
                                    msgs = node_output.get("messages", [])
                                    if msgs and not formatter_started:
                                        text = getattr(msgs[-1], "content", str(msgs[-1]))
                                        final_response_parts.append(text)
                                        yield f"__FINAL__{text}"
                                        formatter_started = True
                            elif node_name == "orchestrator":
                                active_workers = node_output.get("active_workers") or []
                                if active_workers:
                                    domain_labels = [domain_display_names.get(d, d.replace('_analytics', '')) for d in active_workers]
                                    yield f"__REPLACE__Gathering details on {', '.join(domain_labels)}..."
                                else:
                                    msgs = node_output.get("messages", [])
                                    if msgs and not formatter_started:
                                        text = getattr(msgs[-1], "content", str(msgs[-1]))
                                        final_response_parts.append(text)
                                        yield f"__FINAL__{text}"
                                        formatter_started = True
                            elif node_name in ["project_analytics", "ticket_analytics", "resource_analytics", "workers"]:
                                msgs = node_output.get("messages", [])
                                has_tool_calls = any(getattr(m, "tool_calls", None) for m in msgs)
                                if has_tool_calls:
                                    yield "__REPLACE__Connecting the data points..."
                                else:
                                    if msgs and not formatter_started:
                                        text = getattr(msgs[-1], "content", str(msgs[-1]))
                                        final_response_parts.append(text)
                                        yield f"__FINAL__{text}"
                                        formatter_started = True
                            elif node_name == "tool_executor":
                                if node_output.get("needs_retry"):
                                    yield "__REPLACE__Double-checking the details..."
                                else:
                                    yield "__REPLACE__Almost ready, Writing up your insights..."
                            elif node_name == "formatter":
                                msgs = node_output.get("messages", [])
                                if msgs and not formatter_started:
                                    text = getattr(msgs[-1], "content", str(msgs[-1]))
                                    final_response_parts.append(text)
                                    yield f"__FINAL__{text}"
                                    formatter_started = True
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
                                final_response_parts.append(content)
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
                    final_response_parts.append(table_content)
                    yield f"__FINAL__{table_content}"
                else:
                    empty_msg = "Execution completed with no data."
                    final_response_parts.append(empty_msg)
                    yield f"__FINAL__{empty_msg}"
            else:
                pass
        finally:
            elapsed = time.time() - start_time
            usage = global_token_usage.get()
            total_tokens = usage[0] if usage else 0
            in_tokens = usage[1] if len(usage) > 1 else 0
            out_tokens = usage[2] if len(usage) > 2 else 0

            raw_tracker = get_telemetry_tracker()
            node_usage = {k: v for k, v in raw_tracker.items() if not k.startswith("_")}
            cache_tokens = sum(n.get("cacheToken", 0) for n in node_usage.values())

            final_text = "".join(final_response_parts).strip()
            status = "SUCCESS"
            if node_usage.get("gate_keeper", {}).get("status") == "INJECTION_BLOCKED":
                status = "INJECTION_BLOCKED"
            elif node_usage.get("orchestrator", {}).get("status") == "NO_DOMAIN_FALLBACK":
                status = "NO_DOMAIN_FALLBACK"

            payload = {
                "requestId": request_id,
                "conversationId": conversation_id or "default",
                "companyId": company_id or 0,
                "userId": user_id or 0,
                "userEmail": user_email,
                "userName": user_name,
                "chatMode": chat_mode or "panel",
                "userQuery": query,
                "finalResponse": final_text or None,
                "status": status,
                "totalLatencyMs": int(elapsed * 1000),
                "inToken": in_tokens,
                "outToken": out_tokens,
                "cacheToken": cache_tokens,
                "totalTokens": total_tokens or (in_tokens + out_tokens),
                "nodeWiseUsage": node_usage,
                "errorMessage": None,
            }
            print("\n[REQUEST COMPLETE]")
            print(f"  Request ID: {request_id}")
            print(f"  Time:       {elapsed:.2f}s")
            print(f"  Tokens:     {total_tokens:,} (In: {in_tokens:,}, Out: {out_tokens:,}, Cache: {cache_tokens:,})")
            print(f"  Status:     {status}")
            dispatch_audit_telemetry(payload)
# Initialize the global agent object for FastAPI to import
insight_agent = None
if api_key:
    try:
        insight_agent = InsightAgent(api_key)
    except Exception as e:
        print(f"Error initializing InsightAgent: {e}")
