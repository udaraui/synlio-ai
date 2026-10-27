from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class TicketDraft(BaseModel):
    """Structured draft of a ticket aligned 1:1 with CreateTicketDto."""
    name: Optional[str] = Field(None, description="The name / title of the ticket")
    description: Optional[str] = Field(None, description="Detailed description of the issue or task")
    ticketSpaceName: Optional[str] = Field(None, description="Name of the target ticket space")
    ticketSpaceId: Optional[int] = Field(None, description="Resolved ID of the ticket space")
    ticketTypeName: Optional[str] = Field(None, description="Type: Bug, Feature, Task, etc.")
    ticketTypeId: Optional[int] = Field(None, description="Resolved ID of the ticket type")
    severityName: Optional[str] = Field(None, description="Severity: Critical, High, Medium, Low")
    severityId: Optional[int] = Field(None, description="Resolved ID of the severity")
    statusName: Optional[str] = Field(None, description="Status name, e.g. To Start")
    statusId: Optional[int] = Field(None, description="Resolved ID of the status")
    assigneeName: Optional[str] = Field(None, description="Full or partial name of assignee")
    assigneeId: Optional[int] = Field(None, description="Resolved ID of the assignee")
    completionDate: Optional[str] = Field(None, description="ISO completion date (YYYY-MM-DD)")
    plannedEffort: Optional[float] = Field(None, description="Estimated effort in hours")
    queueName: Optional[str] = Field(None, description="Target queue name")
    queueId: Optional[int] = Field(None, description="Resolved ID of the queue")
    impactName: Optional[str] = Field(None, description="Impact level name")
    impactId: Optional[int] = Field(None, description="Resolved ID of the impact")


class NeedsFieldOption(BaseModel):
    id: Optional[int] = None
    name: str
    prefix: Optional[str] = None


class NeedsField(BaseModel):
    field: str
    label: str
    options: List[NeedsFieldOption] = []


class AutomationDraftPayload(BaseModel):
    action: str = "CREATE_TICKET"
    status: str = "DRAFT"  # "NEEDS_INPUT" | "READY"
    summary: str
    draft: TicketDraft
    warnings: List[str] = []
    needs: List[NeedsField] = []
