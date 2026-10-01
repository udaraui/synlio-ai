from typing import Annotated, TypedDict, Sequence, Optional
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

import operator

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    extracted_values: Optional[dict]
    execution_plan: Optional[list[str]]
    current_step: Optional[int]
    domain_results: Optional[dict]
import contextvars
global_token_usage = contextvars.ContextVar("global_token_usage", default=[0])
