from typing import Annotated, TypedDict, Sequence, Optional, List, Dict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

# Re-export global_token_usage for backward compatibility
from app.agent.telemetry import global_token_usage


class UserScope(TypedDict, total=False):
    """User access permissions for data spaces and restricted task trees."""
    allowed_ticket_space_ids: Optional[List[int]]
    allowed_task_space_ids: Optional[List[int]]
    root_task_restrictions: Optional[Dict[str, List[int]]]


class AgentState(TypedDict, total=False):
    """State graph schema passed across all LangGraph nodes."""
    # Conversational messages
    messages: Annotated[Sequence[BaseMessage], add_messages]
    active_workers: Optional[List[str]]
    error_message: Optional[str]
    current_round: Optional[int]
    retry_feedback: Optional[str]
    needs_retry: Optional[bool]

    # Context & Access Scope
    user_id: Optional[int]
    user_email: Optional[str]
    company_id: Optional[int]
    scope: Optional[UserScope]
    context: Optional[dict]

    # Safety & Gate Keeper
    is_injection: Optional[bool]
    safety_status: Optional[str]
    injection_score: Optional[float]

    # Sentiment & Tone
    frustration_level: Optional[str]
