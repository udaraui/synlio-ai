import os
import json
import time
from typing import Literal
from sqlalchemy import create_engine, text
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from app.agent.state import AgentState, global_token_usage
from app.agent.tools import get_db_engine
from app.agent.prompts import (
    ORCHESTRATOR_SYSTEM_PROMPT,
    ORCHESTRATOR_QUESTIONS,
    FORMATTER_PROMPT,
    get_formatter_prompt,
    get_worker_prompt,
    get_worker_user_context,
)

def print_usage(node_name: str, response, elapsed: float = 0.0, question: str = ""):
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
            time_str = f" | Time: {elapsed:.2f}s" if elapsed else ""
            print(f"Token Usage - In: {in_tokens} | Out: {out_tokens} | Total: {total}{time_str}")
            usage_list = global_token_usage.get()
            usage_list[0] += total
            if len(usage_list) >= 3:
                usage_list[1] += in_tokens
                usage_list[2] += out_tokens
            
            # Log to CSV
            import os
            import csv
            from datetime import datetime
            
            output_dir = os.path.join("scripts", "benchmark")
            os.makedirs(output_dir, exist_ok=True)
            csv_file = os.path.join(output_dir, "benchmark_models_node_wise.csv")
            file_exists = os.path.isfile(csv_file)
            
            with open(csv_file, mode='a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["Timestamp", "Question", "Node", "Model", "In Tokens", "Out Tokens", "Total Tokens", "Time (s)"])
                
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    question,
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

def make_orchestrator_node(llm: ChatOpenAI):
    async def orchestrator_node(state: AgentState, config: RunnableConfig):
        messages = state.get("messages", [])
        is_retry = bool(state.get("needs_retry"))
        current_round = state.get("current_round", 1) if is_retry else 1
        retry_feedback = state.get("retry_feedback") if is_retry else None

        try:
            import time
            import os
            import httpx
            
            if is_retry:
                print(f"[ORCHESTRATOR] Re-orchestrating from the top (Round {current_round})...")
            else:
                print("[ORCHESTRATOR]=======================================================")
                print("Analyzing prompt and extracting semantic context with Jeff (via OpenRouter Decisions API)...")
            start_time = time.time()
            
            # Reconstruct the conversation history
            chat_history = f"SYSTEM INSTRUCTIONS: {ORCHESTRATOR_SYSTEM_PROMPT}\n\n"
            for m in messages:
                role = "User" if m.type == "human" else "Assistant"
                chat_history += f"{role}: {m.content}\n"
                
            # Use the orchestrator LLM model and API key
            model_name = getattr(llm, "model_name", "typesafe/jev-1.13") if llm else "typesafe/jev-1.13"
            raw_key = getattr(llm, "openai_api_key", None)
            if hasattr(raw_key, "get_secret_value"):
                api_key = raw_key.get_secret_value()
            else:
                api_key = str(raw_key) if raw_key else None

            if not api_key or api_key.startswith("***"):
                api_key = os.getenv("LLM_API_KEY")

            if not api_key:
                print("WARNING: LLM_API_KEY is not set.")
            
            # OpenRouter requires hitting /api/alpha/decisions for jev-1.13
            payload = {
                "model": model_name,
                "state": chat_history,
                "questions": ORCHESTRATOR_QUESTIONS
            }
            
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://openrouter.ai/api/alpha/decisions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json=payload,
                    timeout=30.0
                )
                
            if resp.status_code != 200:
                raise Exception(f"OpenRouter API Error: {resp.status_code} - {resp.text}")
                
            result_data = resp.json()
            answers = result_data.get("answers", {})
            
            domain_answer = answers.get("domain", {})
            domain_choice = domain_answer.get("choice", "none")
            domain_probs = domain_answer.get("probabilities", {})
            
            print(f"Domain Choice: {domain_choice} (Confidence: {domain_answer.get('confidence', 0):.2f})")
            print(f"Domain Probabilities: {domain_probs}")
            
            domains = []
            for dom, prob in domain_probs.items():
                if dom != "none" and prob > 0.25:
                    domains.append(dom)
                    
            if not domains and domain_choice != "none":
                domains = [domain_choice]
                
            frust_answer = answers.get("frustration_level", {})
            frustration = frust_answer.get("choice", "none")
            
            print(f"Frustration Choice: {frust_answer.get('choice')} (Confidence: {frust_answer.get('confidence', 0):.2f})")
            print(f"Frustration Probabilities: {frust_answer.get('probabilities', {})}")
            print(f"User Frustration Level: {frustration.upper()}")
            
            UNAVAILABLE_DOMAINS = {"project_analytics", "ticket_analytics"}
            requested_unavailable = [d for d in domains if d in UNAVAILABLE_DOMAINS]
            valid_active_workers = [d for d in domains if d not in UNAVAILABLE_DOMAINS]

            if requested_unavailable and not valid_active_workers:
                unavailable_msg = "Project Analytics and Ticket Analytics are temporarily unavailable while system updates are underway. Only Resource Analytics is currently available."
                print(f"[ORCHESTRATOR] Intercepted unavailable domain request: {requested_unavailable}. Returning notice.")
                return {
                    "active_workers": [],
                    "current_round": current_round,
                    "retry_feedback": retry_feedback,
                    "needs_retry": False,
                    "error_message": unavailable_msg
                }
            elif requested_unavailable and valid_active_workers:
                print(f"[ORCHESTRATOR] Mixed request detected. Keeping available domains: {valid_active_workers} (filtering out {requested_unavailable})")
                domains = valid_active_workers

            print(f"Active Workers: {domains}")
            elapsed = time.time() - start_time
            print(f"Time: {elapsed:.2f}s")
            print()
            
            return {
                "active_workers": domains,
                "current_round": current_round,
                "retry_feedback": retry_feedback,
                "needs_retry": False,
                "error_message": None,
            }
        except Exception as e:
            error_str = str(e)
            print(f"Error parsing orchestration plan: {error_str}")
            
            if "401" in error_str or "expired" in error_str.lower() or "unauthorized" in error_str.lower():
                return {
                    "active_workers": [],
                    "current_round": current_round,
                    "retry_feedback": retry_feedback,
                    "needs_retry": False,
                    "error_message": "The AI service is currently unavailable. Please check the backend console for details."
                }
            
            return {
                "active_workers": [],
                "current_round": current_round,
                "retry_feedback": retry_feedback,
                "needs_retry": False
            }
    return orchestrator_node

def make_worker_node(llm: ChatOpenAI, domain: str, tool_func):
    async def worker_node(state: AgentState, config: RunnableConfig):
        from datetime import datetime
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

        # Inject contextual page parameters (if request originates from a page side drawer)
        page_context = state.get("context")
        if page_context and isinstance(page_context, dict):
            context_lines = []
            if page_context.get("resource_group_name"):
                context_lines.append(f"- Current Resource Group / Pool: \"{page_context['resource_group_name']}\"")
            elif page_context.get("resource_group_id"):
                context_lines.append(f"- Current Resource Group ID: {page_context['resource_group_id']}")
            if page_context.get("task_space_name"):
                context_lines.append(f"- Current Task Space: \"{page_context['task_space_name']}\"")
            elif page_context.get("task_space_id"):
                context_lines.append(f"- Current Task Space ID: {page_context['task_space_id']}")
            if page_context.get("parent_task_name"):
                context_lines.append(f"- Current Parent Task: \"{page_context['parent_task_name']}\"")
            elif page_context.get("parent_task_id"):
                context_lines.append(f"- Current Parent Task ID: {page_context['parent_task_id']}")
            if page_context.get("ticket_space_name"):
                context_lines.append(f"- Current Ticket Space: \"{page_context['ticket_space_name']}\"")
            elif page_context.get("ticket_space_id"):
                context_lines.append(f"- Current Ticket Space ID: {page_context['ticket_space_id']}")

            if context_lines:
                system_content += f"\n\nCURRENT PAGE CONTEXT:\n" + "\n".join(context_lines) + "\n(Unless the user explicitly asks for company-wide or other data, prioritize or filter queries by this current page context).\n"
        
        # Bind the specialized tool and force it to be called
        llm_with_tools = llm.bind_tools([tool_func], tool_choice="any")
        
        # Extract conversation messages for context in follow-up queries (up to last 6 messages)
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
        
        print(f"[{domain.upper()}]==================================================")
        print("Generating query...")
        start_time = time.time()
        
        try:
            # Overwrite callbacks with an empty CallbackManager so LangGraph's astream(stream_mode=["messages"])
            # never forces SSE streaming onto worker tool calling
            from langchain_core.callbacks import CallbackManager
            response = await llm_with_tools.ainvoke(messages, config={"callbacks": CallbackManager([])})
            elapsed = time.time() - start_time
            
            # Log the output tool calls or response
            if getattr(response, "tool_calls", None):
                for tc in response.tool_calls:
                    print(f"Tool Called: {tc['name']}")
                    print(f"Arguments: {json.dumps(tc['args'], indent=2)}")
            else:
                print(f"Output: {response.content}")
                
            print_usage(domain, response, elapsed, question=user_query)
            print()
            
            return {"messages": [response]}
        except Exception as e:
            error_str = str(e)
            print(f"Error in {domain} worker: {error_str}")
            if "401" in error_str or "expired" in error_str.lower() or "unauthorized" in error_str.lower():
                msg = AIMessage(content="The AI service is currently unavailable. Please check the backend console for details.")
                return {"messages": [msg], "error_message": msg.content}
            msg = AIMessage(content=f"An error occurred in the {domain} worker. Please try again.")
            return {"messages": [msg]}
    return worker_node

def make_formatter_node(llm: ChatOpenAI):
    async def formatter_node(state: AgentState, config: RunnableConfig):
        messages = state.get("messages", [])
        human_msgs = [m for m in messages if isinstance(m, HumanMessage)]
        user_query = human_msgs[-1].content if human_msgs else ""
        active_workers = state.get("active_workers", [])
        
        print("[FORMATTER]==========================================")
        print("Formatting final response...")
        start_time = time.time()
        
        if state.get("error_message"):
            response = AIMessage(content=state["error_message"])
            print(f"Completed! (Error: {state['error_message']})")
            print()
            return {"messages": [response], "error_message": None}
        
        # Inlined Extraction: extract tool outputs directly from recent ToolMessages
        tool_map = {
            "query_project_data": "project_analytics",
            "query_ticket_data": "ticket_analytics",
            "query_resource_data": "resource_analytics"
        }
        
        domain_results = {}
        # Find index of last human message to only extract tool messages from the current turn
        last_human_idx = 0
        for i, m in enumerate(messages):
            if isinstance(m, HumanMessage):
                last_human_idx = i
                
        turn_messages = messages[last_human_idx:]
        for msg in turn_messages:
            if getattr(msg, "type", "") == "tool" and msg.name in tool_map:
                domain = tool_map[msg.name]
                text = (msg.content or "").strip()
                if (
                    text
                    and "query returned zero rows" not in text.lower()
                    and "[0]: []" not in text
                    and "error" not in text.lower()
                    and "issue while trying" not in text.lower()
                ):
                    if domain not in domain_results:
                        domain_results[domain] = text
                    else:
                        domain_results[domain] += "\n\n" + text
        
        if not active_workers and not domain_results:
            response_text = "I'm sorry, I couldn't understand your request or map it to a specific analytics domain. Could you please rephrase?"
            response = AIMessage(content=response_text)
            print("Completed! (Fallback / No Domain)")
            print()
            return {"messages": [response]}
        
        recent_msgs = messages[-6:]
        conv_history = ""
        for m in recent_msgs[:-1]:
            role = "User" if isinstance(m, HumanMessage) else "Assistant"
            conv_history += f"{role}: {m.content}\n"

        domain_results_text = ""
        for dom, toon_data in domain_results.items():
            domain_results_text += f"\n--- {dom.upper()} ---\n{toon_data}\n"

        content = get_formatter_prompt()
        if conv_history.strip():
            content += f"\n\nRECENT CONVERSATION:\n{conv_history.strip()}"
        content += f"\n\nUSER QUERY: {user_query}\n\nDOMAIN RESULTS (TOON Format):\n{domain_results_text.strip() if domain_results_text.strip() else 'No records found.'}"
        
        try:
            response = await llm.ainvoke([
                SystemMessage(content=content),
                HumanMessage(content=str(user_query))
            ], config=config)
            elapsed = time.time() - start_time
            
            print("Completed!")
            print_usage("FORMATTER", response, elapsed, question=user_query)
            
            return {"messages": [response]}
        except Exception as e:
            error_str = str(e)
            print(f"Error in formatter: {error_str}")
            if "401" in error_str or "expired" in error_str.lower() or "unauthorized" in error_str.lower():
                response = AIMessage(content="The AI service is currently unavailable due to an API key or authentication error. Please check the backend console for details.")
                return {"messages": [response]}
            response = AIMessage(content="An error occurred while formatting the response. Please try again.")
            return {"messages": [response]}
    return formatter_node
