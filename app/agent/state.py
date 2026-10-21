from typing import Annotated, TypedDict, Sequence, Optional
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

import operator

def merge_dicts(a: Optional[dict], b: Optional[dict]) -> dict:
    res = (a or {}).copy()
    res.update(b or {})
    return res

class UserScope(TypedDict, total=False):
    allowed_ticket_space_ids: Optional[list[int]]
    allowed_task_space_ids: Optional[list[int]]
    root_task_restrictions: Optional[dict[str, list[int]]]

class AgentState(TypedDict, total=False):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    active_workers: Optional[list[str]]
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

import contextvars
global_token_usage = contextvars.ContextVar("global_token_usage", default=[0, 0, 0])

