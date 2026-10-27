import time
from typing import Dict, Any
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from app.agent.state import AgentState
from app.agent.telemetry import record_node_telemetry
from app.agent.prompts import get_formatter_prompt


def make_formatter_node(llm: ChatOpenAI):
    """
    Creates the Formatter Node for the Analytics Subgraph.
    Takes SQL tool execution results and synthesizes executive insights, markdown tables, and charts.
    """
    async def formatter_node(state: AgentState, config: RunnableConfig = None):
        messages = state.get("messages", [])
        human_msgs = [m for m in messages if isinstance(m, HumanMessage)]
        user_query = human_msgs[-1].content if human_msgs else ""
        active_workers = state.get("active_workers") or []
        start_time = time.time()

        if state.get("error_message"):
            response = AIMessage(content=state["error_message"])
            return {"messages": [response], "error_message": None}

        tool_map = {
            "query_project_data": "project_analytics",
            "query_ticket_data": "ticket_analytics",
            "query_resource_data": "resource_analytics",
        }

        domain_results = {}
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
        frustration_level = (state.get("frustration_level") or "none").lower()
        if frustration_level in ("high", "medium"):
            content += f"\n\nUSER TONE & SENTIMENT DIRECTIVE: The user's tone is {frustration_level.upper()}LY frustrated/dissatisfied. You MUST start the Overview section with a genuine, warm, and professional apology acknowledging their frustration before delivering the data summary."
        elif frustration_level == "low":
            content += f"\n\nUSER TONE & SENTIMENT DIRECTIVE: The user appears mildly confused or concerned. Open the Overview with a polite, empathetic acknowledgment."

        if conv_history.strip():
            content += f"\n\nRECENT CONVERSATION:\n{conv_history.strip()}"
        content += f"\n\nUSER QUERY: {user_query}\n\nDOMAIN RESULTS (TOON Format):\n{domain_results_text.strip() if domain_results_text.strip() else 'No records found.'}"

        try:
            response = await llm.ainvoke([
                SystemMessage(content=content),
                HumanMessage(content=str(user_query)),
            ], config=config)
            elapsed = time.time() - start_time

            resp_content = getattr(response, "content", "") if response else ""
            if isinstance(resp_content, list):
                resp_content = "".join([c.get("text", "") if isinstance(c, dict) else str(c) for c in resp_content])

            record_node_telemetry(
                node="formatter",
                latency_ms=int(elapsed * 1000),
                response=response,
                status="SUCCESS",
                input_data={
                    "userQuery": str(user_query),
                    "chatMode": "panel",
                    "frustrationLevel": frustration_level,
                    "domainResultsCount": len(domain_results),
                },
                output_data={
                    "formatMode": "panel",
                    "responseLengthChars": len(str(resp_content)),
                    "responsePreview": str(resp_content)[:200],
                },
                extra_summary=f"Chars: {len(str(resp_content))}",
            )

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
