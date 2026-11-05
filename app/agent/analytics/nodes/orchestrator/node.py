import os
import time
import yaml
from pathlib import Path
from typing import Dict, Any, List
import httpx
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.runnables import RunnableConfig
from app.agent.state import AgentState
from app.agent.telemetry import record_node_telemetry

_YAML_PATH = Path(__file__).parent / "prompt.yaml"
try:
    with open(_YAML_PATH, "r", encoding="utf-8") as f:
        _CONFIG = yaml.safe_load(f) or {}
except Exception as e:
    print(f"[WARN] Failed to load orchestrator prompt.yaml: {e}")
    _CONFIG = {}

ORCHESTRATOR_SYSTEM_PROMPT = _CONFIG.get("system_prompt", "Route to correct analytics domain specialist.")
ORCHESTRATOR_QUESTIONS = _CONFIG.get("questions", {})


def make_orchestrator_node(llm: ChatOpenAI):
    """
    Creates the Orchestrator Node for the Analytics Subgraph.
    Evaluates requests and maps them to domain workers:
    project_analytics, ticket_analytics, resource_analytics.
    """
    async def orchestrator_node(state: AgentState, config: RunnableConfig = None):
        messages = state.get("messages", [])
        is_retry = bool(state.get("needs_retry"))
        current_round = state.get("current_round", 1) if is_retry else 1
        retry_feedback = state.get("retry_feedback") if is_retry else None

        try:
            if is_retry:
                print(f"  [ORCHESTRATOR] ↻ Re-orchestrating from the top (Round {current_round})...")
            start_time = time.time()

            user_query = ""
            for m in reversed(messages):
                if isinstance(m, HumanMessage) or getattr(m, "type", "") == "human":
                    user_query = m.content
                    break
                elif isinstance(m, tuple) and len(m) == 2 and m[0] in ("user", "human"):
                    user_query = m[1]
                    break
            if not user_query and messages:
                last_m = messages[-1]
                user_query = getattr(last_m, "content", str(last_m))

            chat_history = f"SYSTEM INSTRUCTIONS: {ORCHESTRATOR_SYSTEM_PROMPT}\n\n"
            for m in messages:
                role = "User" if getattr(m, "type", "") == "human" or isinstance(m, HumanMessage) else "Assistant"
                chat_history += f"{role}: {getattr(m, 'content', '')}\n"

            model_name = getattr(llm, "model_name", "typesafe/jev-1.14") if llm else "typesafe/jev-1.14"
            raw_key = getattr(llm, "openai_api_key", None)
            if hasattr(raw_key, "get_secret_value"):
                api_key = raw_key.get_secret_value()
            else:
                api_key = str(raw_key) if raw_key else None

            if not api_key or api_key.startswith("***"):
                api_key = os.getenv("JEV_API_KEY")

            payload = {
                "model": model_name,
                "state": chat_history,
                "questions": ORCHESTRATOR_QUESTIONS,
            }

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://openrouter.ai/api/alpha/decisions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=30.0,
                )

            if resp.status_code != 200:
                raise Exception(f"OpenRouter API Error: {resp.status_code} - {resp.text}")

            result_data = resp.json()
            result_data.setdefault("model", model_name)
            answers = result_data.get("answers", {})

            domain_answer = answers.get("domain", {})
            domain_choice = domain_answer.get("choice", "none")
            domain_probs = domain_answer.get("probabilities", {})

            domains = []
            for dom, prob in domain_probs.items():
                if dom != "none" and prob > 0.25:
                    domains.append(dom)

            if not domains and domain_choice != "none":
                domains = [domain_choice]

            frust_answer = answers.get("frustration_level", {})
            frustration = frust_answer.get("choice", "none")

            UNAVAILABLE_DOMAINS = {"project_analytics", "ticket_analytics"}
            requested_unavailable = [d for d in domains if d in UNAVAILABLE_DOMAINS]
            valid_active_workers = [d for d in domains if d not in UNAVAILABLE_DOMAINS]

            if requested_unavailable and not valid_active_workers:
                unavailable_msg = "Project Analytics, Ticket Analytics, and Project Automation are temporarily unavailable while system updates are underway. Only Resource Analytics and Ticket Automation are currently available."
                elapsed = time.time() - start_time
                record_node_telemetry(
                    node="orchestrator",
                    latency_ms=int(elapsed * 1000),
                    response=result_data,
                    model=model_name,
                    status="UNAVAILABLE_DOMAIN",
                    input_data={"userQuery": user_query, "isRetry": is_retry, "retryFeedback": retry_feedback},
                    output_data={"activeWorkers": [], "requestedUnavailable": requested_unavailable},
                    error_message=unavailable_msg,
                    extra_summary=f"Unavailable: {requested_unavailable}",
                )
                return {
                    "messages": [AIMessage(content=unavailable_msg)],
                    "active_workers": [],
                    "current_round": current_round,
                    "retry_feedback": retry_feedback,
                    "needs_retry": False,
                    "error_message": unavailable_msg,
                }
            elif requested_unavailable and valid_active_workers:
                domains = valid_active_workers

            elapsed = time.time() - start_time
            record_node_telemetry(
                node="orchestrator",
                latency_ms=int(elapsed * 1000),
                response=result_data,
                model=model_name,
                status="SUCCESS" if domains else "NO_DOMAIN_FALLBACK",
                input_data={"userQuery": user_query, "isRetry": is_retry, "retryFeedback": retry_feedback},
                output_data={
                    "activeWorkers": domains,
                    "domainChoice": domain_choice,
                    "domainConfidence": domain_answer.get("confidence", 0),
                    "domainProbabilities": domain_probs,
                    "frustrationLevel": frustration,
                    "frustrationConfidence": frust_answer.get("confidence", 0),
                },
                extra_summary=f"Domains: {domains} | Frustration: {frustration.upper()}",
            )

            if not domains:
                if frustration in ("high", "medium"):
                    friendly_msg = (
                        "I sincerely apologize for the frustration. I am here to help you get the answers you need. "
                        "I can assist with analyzing project timelines, tasks, resource workloads, and tickets. "
                        "Please let me know what specific data or issue you'd like to look into."
                    )
                elif frustration == "low":
                    friendly_msg = (
                        "I apologize for any confusion. I'm here to assist you with your project timelines, "
                        "tasks, resource workloads, or tickets. How can I best help you?"
                    )
                else:
                    friendly_msg = (
                        "I am your Synlio AI Co-Pilot. I can help you analyze project timelines, "
                        "tasks, resource workloads, and tickets. How can I help you?"
                    )
                return {
                    "messages": [AIMessage(content=friendly_msg)],
                    "active_workers": [],
                    "current_round": current_round,
                    "retry_feedback": retry_feedback,
                    "needs_retry": False,
                    "error_message": None,
                    "frustration_level": frustration,
                }

            return {
                "active_workers": domains,
                "current_round": current_round,
                "retry_feedback": retry_feedback,
                "needs_retry": False,
                "error_message": None,
                "frustration_level": frustration,
            }

        except Exception as e:
            error_str = str(e)
            print(f"Error parsing orchestration plan: {error_str}")

            if "401" in error_str or "expired" in error_str.lower() or "unauthorized" in error_str.lower():
                auth_msg = "The AI service is currently unavailable. Please check the backend console for details."
                return {
                    "messages": [AIMessage(content=auth_msg)],
                    "active_workers": [],
                    "current_round": current_round,
                    "retry_feedback": retry_feedback,
                    "needs_retry": False,
                    "error_message": auth_msg,
                }

            fallback_msg = "I'm sorry, I couldn't understand your request or map it to a specific analytics domain. Could you please rephrase or specify if you'd like to inspect projects, tickets, or resources?"
            return {
                "messages": [AIMessage(content=fallback_msg)],
                "active_workers": [],
                "current_round": current_round,
                "retry_feedback": retry_feedback,
                "needs_retry": False,
                "error_message": fallback_msg,
            }

    return orchestrator_node
