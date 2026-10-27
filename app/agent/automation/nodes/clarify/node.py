import time
import json
import yaml
from pathlib import Path
from typing import Dict, Any
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from app.agent.automation.state import AutomationState
from app.agent.telemetry import record_node_telemetry

_YAML_PATH = Path(__file__).parent / "prompt.yaml"
try:
    with open(_YAML_PATH, "r", encoding="utf-8") as f:
        _CONFIG = yaml.safe_load(f) or {}
except Exception as e:
    print(f"[WARN] Failed to load clarify prompt.yaml: {e}")
    _CONFIG = {}

CLARIFY_PROMPT_TEMPLATE = _CONFIG.get("system_prompt", "Ask for missing fields.")


def make_clarify_node(llm):
    """
    Creates the clarify node.
    Asks the user for missing mandatory fields (e.g. ticketSpace) in a friendly, conversational manner.
    """
    async def clarify_node(state: AutomationState) -> Dict[str, Any]:
        start_time = time.time()
        draft = state.get("draft") or {}
        missing_fields = state.get("missing_fields") or []
        needs_fields = state.get("needs_fields") or []
        title = draft.get("name") or "your ticket"

        # Fast templating if space is the only missing field
        if missing_fields == ["ticketSpace"]:
            options = []
            for nf in needs_fields:
                if nf.get("field") == "ticketSpace":
                    options = [opt.get("name") for opt in nf.get("options", []) if opt.get("name")]
            opts_hint = f" (e.g., {', '.join(options[:2])})" if options else ""
            clarify_text = f"I've drafted the ticket for '{title}'. Which ticket space should I place it in?{opts_hint}"
        else:
            try:
                system_content = (
                    CLARIFY_PROMPT_TEMPLATE
                    .replace("{missing_fields_list}", ", ".join(missing_fields))
                    .replace("{current_draft_json}", json.dumps(draft))
                    .replace("{options_json}", json.dumps(needs_fields))
                    .replace("{title}", str(title))
                )
                response = await llm.ainvoke([
                    SystemMessage(content=system_content),
                    HumanMessage(content="Generate the clarification question:"),
                ])
                clarify_text = response.content.strip()
            except Exception as e:
                clarify_text = f"I've prepared your ticket '{title}'. Could you please select the target space?"

        elapsed = time.time() - start_time
        record_node_telemetry(
            node="automation_clarify",
            latency_ms=int(elapsed * 1000),
            status="SUCCESS",
            output_data={"clarification": clarify_text, "missing_fields": missing_fields},
        )

        return {
            "messages": [AIMessage(content=clarify_text)],
            "summary": clarify_text,
            "automation_summary": clarify_text,
        }

    return clarify_node
