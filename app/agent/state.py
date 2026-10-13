from typing import Annotated, TypedDict, Sequence, Optional
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

import operator

def merge_dicts(a: Optional[dict], b: Optional[dict]) -> dict:
    res = (a or {}).copy()
    res.update(b or {})
    return res

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    active_workers: Optional[list[str]]
    error_message: Optional[str]
    current_round: Optional[int]
    retry_feedback: Optional[str]
    needs_retry: Optional[bool]

import contextvars
global_token_usage = contextvars.ContextVar("global_token_usage", default=[0])

