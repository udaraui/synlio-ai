from typing import TypedDict, Optional, List, Dict, Any, Sequence, Union
from langchain_core.messages import BaseMessage


class AutomationState(TypedDict, total=False):
    """Internal state schema for the Automation Subgraph workflow."""
    messages: Sequence[BaseMessage]
    pending_draft: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]]
    draft: Optional[Dict[str, Any]]
    drafts: Optional[List[Dict[str, Any]]]
    missing_fields: List[str]
    needs_fields: List[Dict[str, Any]]
    is_complete: bool
    summary: Optional[str]
    warnings: List[str]
    scope: Optional[Dict[str, Any]]
    context: Optional[Dict[str, Any]]
    attachments: Optional[List[Dict[str, Any]]]
