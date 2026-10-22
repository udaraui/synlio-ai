import os
import time
import uuid
from typing import Optional, Dict, Any, Tuple
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage
from pydantic import BaseModel
from app.agent.state import AgentState
from app.agent.graph import create_graph
from app.agent.telemetry import (
    init_telemetry,
    get_telemetry_tracker,
    dispatch_audit_telemetry,
    global_token_usage,
)

load_dotenv()
api_key = os.getenv("LLM_API_KEY")

# =============================================================================
# 1. CONSTANTS & DOMAIN LABELS
# =============================================================================

DOMAIN_DISPLAY_NAMES = {
    "resource_analytics": "resource workload",
    "project_analytics": "projects",
    "ticket_analytics": "tickets",
}

class AgentResult(BaseModel):
    data: str

# =============================================================================
# 2. STREAM EVENT & TELEMETRY HELPERS
# =============================================================================

def _resolve_status_update(node_name: str, node_output: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """
    Translates a LangGraph node state update into UI status indicators:
    Returns (replace_status_message, final_fallback_text).
    """
    if node_name == "gate_keeper":
        if node_output.get("is_injection"):
            msgs = node_output.get("messages", [])
            if msgs:
                return None, getattr(msgs[-1], "content", str(msgs[-1]))

    elif node_name == "orchestrator":
        active_workers = node_output.get("active_workers") or []
        if active_workers:
            domain_labels = [DOMAIN_DISPLAY_NAMES.get(d, d.replace("_analytics", "")) for d in active_workers]
            return f"__REPLACE__Gathering details on {', '.join(domain_labels)}...", None
        else:
            msgs = node_output.get("messages", [])
            if msgs:
                return None, getattr(msgs[-1], "content", str(msgs[-1]))

    elif node_name in ["project_analytics", "ticket_analytics", "resource_analytics", "workers"]:
        msgs = node_output.get("messages", [])
        has_tool_calls = any(getattr(m, "tool_calls", None) for m in msgs)
        if has_tool_calls:
            return "__REPLACE__Connecting the data points...", None
        elif msgs:
            return None, getattr(msgs[-1], "content", str(msgs[-1]))

    elif node_name == "tool_executor":
        if node_output.get("needs_retry"):
            return "__REPLACE__Double-checking the details...", None
        return "__REPLACE__Almost ready, Writing up your insights...", None

    elif node_name == "formatter":
        msgs = node_output.get("messages", [])
        if msgs:
            return None, getattr(msgs[-1], "content", str(msgs[-1]))

    return None, None

def _dispatch_telemetry_payload(
    request_id: str,
    conversation_id: Optional[str],
    company_id: Optional[int],
    user_id: Optional[int],
    user_email: Optional[str],
    user_name: Optional[str],
    chat_mode: Optional[str],
    query: str,
    final_text: str,
    elapsed: float,
):
    """Gathers token counts and node latencies, logs summary to console, and sends telemetry."""
    usage = global_token_usage.get()
    total_tokens = usage[0] if usage else 0
    in_tokens = usage[1] if len(usage) > 1 else 0
    out_tokens = usage[2] if len(usage) > 2 else 0

    raw_tracker = get_telemetry_tracker()
    node_usage = {k: v for k, v in raw_tracker.items() if not k.startswith("_")}
    cache_tokens = sum(n.get("cacheToken", 0) for n in node_usage.values())

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

# =============================================================================
# 3. INSIGHT AGENT SERVICE FACADE
# =============================================================================

class InsightAgent:
    """Service facade managing LLM connections, LangGraph compilation, and streaming."""

    def __init__(self, key: str, worker_model: Optional[str] = None, orchestrator_model: Optional[str] = None):
        self.worker_model = worker_model or os.getenv("WORKER_MODEL")
        if not self.worker_model:
            print("[ENV ERROR] WORKER_MODEL is not set in environment (.env)!")

        self.orchestrator_model = orchestrator_model or os.getenv("ORCHESTRATOR_MODEL")
        if not self.orchestrator_model:
            print("[ENV ERROR] ORCHESTRATOR_MODEL is not set in environment (.env)!")

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

        # Orchestrator LLM
        self.orchestrator_llm = ChatOpenAI(
            model=self.orchestrator_model,
            api_key=key,
            temperature=0,
            base_url="https://openrouter.ai/api/v1",
            streaming=False
        )

        self.llm = self.worker_llm

        # Build and compile the LangGraph workflow
        self.agent_executor = create_graph(
            llm=self.worker_llm,
            orchestrator_llm=self.orchestrator_llm,
            formatter_llm=self.formatter_llm
        )

    def _build_input_state(
        self,
        query: str,
        user_id: Optional[int],
        user_email: Optional[str],
        company_id: Optional[int],
        agent: Optional[str],
        context: Optional[dict],
        scope: Optional[dict],
        history: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Constructs the initial AgentState dictionary from client history and current query."""
        messages = []
        if history and isinstance(history, list):
            for h in history:
                if not isinstance(h, dict):
                    continue
                role = (h.get("role") or "").lower()
                content = h.get("content") or ""
                if not content:
                    continue
                if role in ("user", "human"):
                    messages.append(HumanMessage(content=str(content)))
                elif role in ("assistant", "ai"):
                    messages.append(AIMessage(content=str(content)))

        messages.append(HumanMessage(content=query))

        return {
            "messages": messages,
            "user_id": user_id,
            "user_email": user_email,
            "company_id": company_id,
            "scope": scope,
            "context": context,
            "active_workers": [agent] if agent else None,
            "error_message": None,
        }

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
        history: Optional[list] = None,
    ):
        """Asynchronously streams user status messages and token-by-token responses."""
        init_telemetry()
        if not request_id:
            request_id = str(uuid.uuid4())

        global_token_usage.set([0, 0, 0])
        start_time = time.time()
        final_response_parts = []
        table_content = ""
        formatter_started = False

        # Initial status marker
        if agent and agent in DOMAIN_DISPLAY_NAMES:
            yield f"__REPLACE__Gathering details on {DOMAIN_DISPLAY_NAMES[agent]}..."
        else:
            yield "__REPLACE__Checking what you need..."

        # When client sends explicit history, each request is completely stateless
        thread_id = str(uuid.uuid4()) if history else (conversation_id or "default")
        config = {"configurable": {"thread_id": thread_id}}
        input_state = self._build_input_state(query, user_id, user_email, company_id, agent, context, scope, history)

        try:
            async for item in self.agent_executor.astream(input_state, config=config, stream_mode=["updates", "messages"]):
                if not (isinstance(item, tuple) and len(item) == 2):
                    continue

                mode, payload = item

                # 1. State updates across nodes -> yield UI status phrases
                if mode == "updates" and isinstance(payload, dict):
                    for node_name, node_output in payload.items():
                        status_msg, final_text = _resolve_status_update(node_name, node_output)
                        if status_msg and not formatter_started:
                            yield status_msg
                        elif final_text and not formatter_started:
                            final_response_parts.append(final_text)
                            yield f"__FINAL__{final_text}"
                            formatter_started = True

                # 2. Token-by-token streaming from the formatter node
                elif mode == "messages":
                    chunk, metadata = payload
                    if getattr(chunk, "type", "") == "tool" and chunk.content:
                        if "Query returned zero rows" not in chunk.content:
                            table_content = chunk.content

                    if metadata.get("langgraph_node") == "formatter":
                        content = chunk.content
                        if isinstance(content, list):
                            content = "".join(c.get("text", "") if isinstance(c, dict) else c for c in content)

                        if content:
                            final_response_parts.append(content)
                            if not formatter_started:
                                yield f"__FINAL__{content}"
                                formatter_started = True
                            else:
                                yield content

            # Fallback if the formatter was not reached (e.g. direct data return)
            if not formatter_started:
                fallback = table_content or "Execution completed with no data."
                final_response_parts.append(fallback)
                yield f"__FINAL__{fallback}"

        finally:
            elapsed = time.time() - start_time
            final_text = "".join(final_response_parts).strip()
            _dispatch_telemetry_payload(
                request_id=request_id,
                conversation_id=conversation_id,
                company_id=company_id,
                user_id=user_id,
                user_email=user_email,
                user_name=user_name,
                chat_mode=chat_mode,
                query=query,
                final_text=final_text,
                elapsed=elapsed,
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
        history: Optional[list] = None,
    ) -> AgentResult:
        """Synchronous/batch invocation fallback."""
        thread_id = str(uuid.uuid4()) if history else (conversation_id or "default")
        config = {"configurable": {"thread_id": thread_id}}
        input_state = self._build_input_state(query, user_id, user_email, company_id, agent, context, scope, history)
        response = await self.agent_executor.ainvoke(input_state, config=config)
        output = response["messages"][-1].content
        if isinstance(output, list):
            output = "".join(block.get("text", "") for block in output if block.get("type") == "text")
        return AgentResult(data=str(output))

# =============================================================================
# 4. GLOBAL SERVICE INSTANCE
# =============================================================================

insight_agent: Optional[InsightAgent] = None
if api_key:
    try:
        insight_agent = InsightAgent(api_key)
    except Exception as e:
        print(f"Error initializing InsightAgent: {e}")
else:
    print("[ENV ERROR] LLM_API_KEY is not set in environment (.env)!")
