import os
import time
from datetime import datetime
from typing import Dict, Any
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from sqlalchemy import text
from app.agent.state import AgentState
from app.agent.telemetry import record_node_telemetry
from app.agent.tools import get_db_engine
from app.agent.prompts import (
    get_worker_prompt,
    get_worker_user_context,
    format_page_context,
)


def make_worker_node(llm: ChatOpenAI, domain: str, tool_func):
    """
    Creates a specialized domain worker node (project, ticket, or resource analytics).
    Generates deterministic SQL tool call arguments against the semantic layer.
    """
    async def worker_node(state: AgentState, config: RunnableConfig = None):
        current_date_str = datetime.now().strftime("%Y-%m-%d")
        system_content = f"CURRENT SYSTEM DATE: {current_date_str} (Today's date)\n\n" + get_worker_prompt(domain)

        user_email = state.get("user_email")
        user_id = state.get("user_id")
        company_id = state.get("company_id")

        if user_email:
            user_name = None
            user_manager = None
            user_pool = None
            try:
                pg_db_url = os.getenv("PG_DB_URL")
                if pg_db_url:
                    engine = get_db_engine(pg_db_url)
                    with engine.connect() as conn:
                        if company_id is not None:
                            q = text("SELECT full_name, reporting_resource_name, pool_list FROM gold.dim_resource WHERE email = :e AND company_id = :c LIMIT 1")
                            row = conn.execute(q, {"e": user_email, "c": company_id}).fetchone()
                        else:
                            q = text("SELECT full_name, reporting_resource_name, pool_list FROM gold.dim_resource WHERE email = :e LIMIT 1")
                            row = conn.execute(q, {"e": user_email}).fetchone()
                        if row:
                            user_name = row[0]
                            user_manager = row[1]
                            user_pool = row[2]
            except Exception as e:
                print(f"[WORKER USER LOOKUP] Warning: Could not look up user profile: {e}")

            worker_user_context = get_worker_user_context(
                user_email=user_email,
                user_id=user_id,
                company_id=company_id,
                user_name=user_name,
                user_manager=user_manager,
                user_pool=user_pool,
            )
            if worker_user_context:
                system_content += f"\n\n{worker_user_context}"

        page_context_str = format_page_context(state.get("context"))
        if page_context_str:
            system_content += f"\n\n{page_context_str}"

        llm_with_tools = llm.bind_tools([tool_func], tool_choice="any")

        recent_msgs = state.get("messages", [])[-6:]
        conversation_context = ""
        for m in recent_msgs[:-1]:
            role = "User" if isinstance(m, HumanMessage) else "Assistant"
            conversation_context += f"{role}: {m.content}\n"

        human_msgs = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        user_query = human_msgs[-1].content if human_msgs else ""

        prompt_query = user_query
        if conversation_context.strip():
            prompt_query = f"CONVERSATION HISTORY:\n{conversation_context.strip()}\n\nCURRENT USER REQUEST:\n{user_query}"

        retry_feedback = state.get("retry_feedback")
        if retry_feedback:
            prompt_query += (
                f"\n\n[RETRY NOTICE: Previous query execution returned no results or failed: {retry_feedback}. "
                f"Please review the Semantic Layer Schema carefully and adjust your query: "
                f"use wildcards (e.g. '%value%' with ILIKE), verify table and column names in the schema, "
                f"or relax filter constraints to find matching data.]"
            )

        messages = [SystemMessage(content=system_content), HumanMessage(content=str(prompt_query))]
        start_time = time.time()

        try:
            from langchain_core.callbacks import CallbackManager
            response = await llm_with_tools.ainvoke(messages, config={"callbacks": CallbackManager([])})
            elapsed = time.time() - start_time

            tool_calls_data = [{"name": tc["name"], "args": tc["args"]} for tc in getattr(response, "tool_calls", [])] if getattr(response, "tool_calls", None) else []
            tool_names = [tc["name"] for tc in tool_calls_data]

            if not tool_calls_data:
                content = (response.content or "").strip()
                if not content:
                    friendly_text = f"I reviewed your request for {domain.replace('_', ' ')}, but did not find any specific data action to take. Please provide more details or ask a specific question."
                    response = AIMessage(content=friendly_text)

            record_node_telemetry(
                node=domain,
                latency_ms=int(elapsed * 1000),
                response=response,
                model=getattr(llm, "model_name", "unknown"),
                status="SUCCESS",
                input_data={"userQuery": user_query, "domain": domain, "isRetry": bool(retry_feedback)},
                output_data={
                    "toolsCalled": tool_names,
                    "toolArgs": [tc["args"] for tc in tool_calls_data],
                    "hasToolCalls": bool(tool_calls_data),
                },
                extra_summary=f"Tools: {tool_names or 'Direct Answer'}",
            )

            return {
                "messages": [response],
                "active_workers": [domain],
            }

        except Exception as e:
            elapsed = time.time() - start_time
            print(f"Error executing worker {domain}: {e}")
            err_msg = f"I encountered an error querying data for {domain.replace('_', ' ')}."
            record_node_telemetry(
                node=domain,
                latency_ms=int(elapsed * 1000),
                model=getattr(llm, "model_name", "unknown"),
                status="ERROR",
                input_data={"userQuery": user_query, "domain": domain},
                output_data={"error": str(e)},
                error_message=str(e),
            )
            return {
                "messages": [AIMessage(content=err_msg)],
                "active_workers": [domain],
            }

    return worker_node
