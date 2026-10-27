from typing import Annotated, TypedDict, Sequence, Optional, List, Dict, Any
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from app.agent.state import UserScope


class AnalyticsState(TypedDict, total=False):
    """Internal state schema for the Analytics Subgraph workflow."""
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

    # Sentiment & Tone
    frustration_level: Optional[str]
