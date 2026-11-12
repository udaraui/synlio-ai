import os
import time
import uuid
import asyncio
from typing import Optional, Dict, Any, Tuple, Union, List
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage
import json
from pydantic import BaseModel
from app.agent.state import AgentState
from app.agent.master_graph import create_master_graph
from app.agent.telemetry import (
    init_telemetry,
    get_telemetry_tracker,
    dispatch_audit_telemetry,
    global_token_usage,
)

load_dotenv()

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

    elif node_name == "dispatcher":
        intent = node_output.get("intent")
        if intent == "automation":
            return "__REPLACE__Preparing your draft...", None
        elif intent == "general_chat":
            msgs = node_output.get("messages", [])
            if msgs:
                return None, getattr(msgs[-1], "content", str(msgs[-1]))
        return "__REPLACE__Analyzing workspace data...", None

    elif node_name == "automation":
        mutation_payload = node_output.get("mutation_payload")
        if not mutation_payload and node_output.get("automation_action") in ("UPDATE_TICKET", "UPDATE_TASK"):
            action = node_output.get("automation_action")
            entity_type = node_output.get("entity_type") or ("task" if action == "UPDATE_TASK" else "ticket")
            mutation_payload = {
                "action": action,
                "entityType": entity_type,
                "status": "READY" if node_output.get("targets") else "NEEDS_INPUT",
                "targets": node_output.get("targets") or [],
                "changes": node_output.get("changes") or {},
                "summary": node_output.get("automation_summary") or node_output.get("summary") or "Updated.",
            }

        if mutation_payload:
            summary = (
                node_output.get("automation_summary")
                or node_output.get("summary")
                or mutation_payload.get("summary")
                or "Updated."
            )
            return None, f"{summary}\n\n__MUTATION_PAYLOAD__{json.dumps(mutation_payload)}"

        draft = node_output.get("draft")
        drafts = node_output.get("drafts")
        if not drafts:
            pending = node_output.get("pending_draft")
            if isinstance(pending, list) and len(pending) > 1:
                drafts = pending
        if not drafts and draft:
            drafts = [draft]
        missing_fields = node_output.get("missing_fields") or []
        needs_fields = node_output.get("needs_fields") or []
        action = node_output.get("automation_action") or ("CREATE_TASK" if node_output.get("entity_type") == "task" else "CREATE_TICKET")
        entity_type = node_output.get("entity_type") or ("task" if action == "CREATE_TASK" else "ticket")
        summary = (
            node_output.get("automation_summary")
            or node_output.get("summary")
            or ("Draft ready")
        )
        
        payload = {
            "action": action,
            "entityType": entity_type,
            "status": "NEEDS_INPUT" if missing_fields else "READY",
            "summary": summary,
            "draft": draft,
            "drafts": drafts if drafts else ([draft] if draft else []),
            "warnings": node_output.get("warnings") or [],
            "needs": needs_fields,
        }
        return None, f"{summary}\n\n__DRAFT_PAYLOAD__{json.dumps(payload)}"

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
    is_cancelled: bool = False,
    error_message: Optional[str] = None,
):
    """Gathers token counts and node latencies, logs summary to console, and sends telemetry."""
    usage = global_token_usage.get()
    total_tokens = usage[0] if usage else 0
    in_tokens = usage[1] if len(usage) > 1 else 0
    out_tokens = usage[2] if len(usage) > 2 else 0

    raw_tracker = get_telemetry_tracker()
    node_usage = {k: v for k, v in raw_tracker.items() if not k.startswith("_")}
    cache_tokens = sum(n.get("cacheToken", 0) for n in node_usage.values())

    # Find first error message if any
    node_error_msg = error_message
    if not node_error_msg:
        for n in node_usage.values():
            if n.get("errorMessage"):
                node_error_msg = n.get("errorMessage")
                break

    if is_cancelled:
        status = "STOPPED_BY_USER"
        if not node_error_msg:
            node_error_msg = "Generation stopped by user"
    elif node_error_msg:
        status = "ERROR"
    elif node_usage.get("gate_keeper", {}).get("status") == "INJECTION_BLOCKED":
        status = "INJECTION_BLOCKED"
    elif node_usage.get("orchestrator", {}).get("status") == "NO_DOMAIN_FALLBACK":
        status = "NO_DOMAIN_FALLBACK"
    elif any(n.get("status") == "ERROR" for n in node_usage.values()):
        status = "ERROR"
    elif any(n.get("status") == "FALLBACK" for n in node_usage.values()):
        status = "FALLBACK"
    else:
        status = "SUCCESS"

    # Classify requestType based on executed nodes
    request_type = "analytics"
    if any(k.startswith("automation_") for k in node_usage.keys()):
        request_type = "automation"
    elif any(k in node_usage for k in ("orchestrator", "workers", "ticket_analytics", "project_analytics", "resource_analytics", "tool_executor")):
        request_type = "analytics"
    elif "general_chat" in node_usage:
        request_type = "general_chat"

    payload = {
        "requestId": request_id,
        "conversationId": conversation_id or "default",
        "companyId": company_id or 0,
        "userId": user_id or 0,
        "userEmail": user_email,
        "userName": user_name,
        "chatMode": chat_mode or "panel",
        "requestType": request_type,
        "userQuery": query,
        "finalResponse": final_text or None,
        "status": status,
        "totalLatencyMs": int(elapsed * 1000),
        "inToken": in_tokens,
        "outToken": out_tokens,
        "cacheToken": cache_tokens,
        "totalTokens": total_tokens or (in_tokens + out_tokens),
        "nodeWiseUsage": node_usage,
        "errorMessage": node_error_msg,
    }

    # If stream was cancelled by user, log explicitly and dispatch telemetry
    if is_cancelled:
        print("\n[REQUEST STOPPED BY USER]")
        print(f"  Request ID: {request_id}")
        print(f"  Time:       {elapsed:.2f}s")
        print(f"  Tokens:     {total_tokens:,} (In: {in_tokens:,}, Out: {out_tokens:,}, Cache: {cache_tokens:,})")
        print(f"  Status:     STOPPED_BY_USER")
        dispatch_audit_telemetry(payload)
        return

    # If stream failed with an error, log explicitly and dispatch telemetry
    if status == "ERROR":
        print("\n[REQUEST FAILED WITH ERROR]")
        print(f"  Request ID: {request_id}")
        print(f"  Time:       {elapsed:.2f}s")
        print(f"  Tokens:     {total_tokens:,} (In: {in_tokens:,}, Out: {out_tokens:,}, Cache: {cache_tokens:,})")
        print(f"  Status:     ERROR")
        print(f"  Error:      {node_error_msg}")
        dispatch_audit_telemetry(payload)
        return

    # Check if the AI is actively asking a question (e.g. space or parent project selection pills)
    extract_delta_output = node_usage.get("automation_extract_delta", {}).get("output", {})
    needs_fields = extract_delta_output.get("needs_fields") or []
    missing_fields = extract_delta_output.get("missing_fields") or []

    # Disambiguation questions: asking what space or what project to create under
    has_disambiguation_question = (
        "automation_clarify" in node_usage
        or any(f.get("field") in ("taskSpace", "ticketSpace", "parentTask") for f in needs_fields)
        or ("parentTask" in missing_fields and not extract_delta_output.get("is_complete") and len(needs_fields) > 0)
        or ("taskSpace" in missing_fields and len(needs_fields) > 0)
        or ("ticketSpace" in missing_fields and len(needs_fields) > 0)
    )

    if has_disambiguation_question:
        print("\n[REQUEST WAITING FOR HUMAN INPUT]")
        print(f"  Request ID: {request_id}")
        print(f"  Time:       {elapsed:.2f}s")
        print(f"  Tokens:     {total_tokens:,} (In: {in_tokens:,}, Out: {out_tokens:,}, Cache: {cache_tokens:,})")
        print(f"  Status:     HUMAN_INPUT_NEEDED")
        print(f"  Audit Log:  Skipped (Awaiting space or project disambiguation)")
        return

    # Once the AI fetches and delivers the draft table, AI work is complete -> Enqueue audit log!
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

    def __init__(
        self,
        worker_model: Optional[str] = None,
        orchestrator_model: Optional[str] = None,
        gamma_key: Optional[str] = None,
        jev_key: Optional[str] = None,
    ):
        self.gamma_key = gamma_key or os.getenv("GAMMA_API_KEY")
        if not self.gamma_key:
            print("[ENV ERROR] GAMMA_API_KEY is not set in environment (.env)!")

        self.jev_key = jev_key or os.getenv("JEV_API_KEY")
        if not self.jev_key:
            print("[ENV ERROR] JEV_API_KEY is not set in environment (.env)!")

        self.worker_model = worker_model or os.getenv("WORKER_MODEL")
        if not self.worker_model:
            print("[ENV ERROR] WORKER_MODEL is not set in environment (.env)!")

        self.orchestrator_model = orchestrator_model or os.getenv("ORCHESTRATOR_MODEL")
        if not self.orchestrator_model:
            print("[ENV ERROR] ORCHESTRATOR_MODEL is not set in environment (.env)!")

        # Worker LLM (Gamma): streaming=False prevents OpenRouter SSE stream hangs on tool calling
        self.worker_llm = ChatOpenAI(
            model=self.worker_model,
            api_key=self.gamma_key,
            temperature=0,
            base_url="https://openrouter.ai/api/v1",
            streaming=False
        )

        # Formatter LLM (Gamma): streaming=True enables token-by-token streaming to the UI
        self.formatter_llm = ChatOpenAI(
            model=self.worker_model,
            api_key=self.gamma_key,
            temperature=0,
            base_url="https://openrouter.ai/api/v1",
            streaming=True
        )

        # Orchestrator LLM (Jev Decisions)
        self.orchestrator_llm = ChatOpenAI(
            model=self.orchestrator_model,
            api_key=self.jev_key,
            temperature=0,
            base_url="https://openrouter.ai/api/v1",
            streaming=False
        )

        self.llm = self.worker_llm

        # Build and compile the LangGraph workflow using Master Graph
        self.agent_executor = create_master_graph(
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
        pending_draft: Optional[Union[dict, list]] = None,
        chat_mode: Optional[str] = "panel",
        attachments: Optional[List[Dict[str, Any]]] = None,
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

        effective_mode = (
            chat_mode
            or (context.get("chatMode") if isinstance(context, dict) else None)
            or (context.get("mode") if isinstance(context, dict) else None)
            or "panel"
        )

        return {
            "messages": messages,
            "user_id": user_id,
            "user_email": user_email,
            "company_id": company_id,
            "agent": agent,
            "scope": scope,
            "context": context,
            "chat_mode": effective_mode,
            "active_workers": [agent] if agent else None,
            "error_message": None,
            "pending_draft": pending_draft,
            "attachments": attachments,
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
        pending_draft: Optional[Union[dict, list]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
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
        if attachments:
            yield "__REPLACE__Analyzing attached document..."
        elif agent and agent in DOMAIN_DISPLAY_NAMES:
            yield f"__REPLACE__Gathering details on {DOMAIN_DISPLAY_NAMES[agent]}..."
        else:
            yield "__REPLACE__Checking what you need..."

        # When client sends explicit history, each request is completely stateless
        thread_id = str(uuid.uuid4()) if history else (conversation_id or "default")
        config = {"configurable": {"thread_id": thread_id}}
        input_state = self._build_input_state(query, user_id, user_email, company_id, agent, context, scope, history, pending_draft, chat_mode, attachments=attachments)

        is_cancelled = False
        caught_error: Optional[Exception] = None
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

        except asyncio.CancelledError:
            is_cancelled = True
            raise
        except Exception as e:
            caught_error = e
            raise
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
                is_cancelled=is_cancelled,
                error_message=str(caught_error) if caught_error else None,
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
jev_api_key = os.getenv("JEV_API_KEY")
gamma_api_key = os.getenv("GAMMA_API_KEY")

if jev_api_key and gamma_api_key:
    try:
        insight_agent = InsightAgent(gamma_key=gamma_api_key, jev_key=jev_api_key)
    except Exception as e:
        print(f"Error initializing InsightAgent: {e}")
else:
    missing = []
    if not jev_api_key:
        missing.append("JEV_API_KEY")
    if not gamma_api_key:
        missing.append("GAMMA_API_KEY")
    print(f"[ENV ERROR] Required API keys missing in (.env): {', '.join(missing)}!")
