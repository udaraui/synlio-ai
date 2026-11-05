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
    sourceTicketCode: Optional[str] = Field(None, description="Source ticket code to clone or copy from (e.g. PTS-11)")

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
    status: str = "DRAFT"
    summary: str
    draft: TicketDraft
    warnings: List[str] = []
    needs: List[NeedsField] = []

class TicketMutationChanges(BaseModel):
    name: Optional[str] = Field(None, description="New ticket title")
    description: Optional[str] = Field(None, description="New ticket description")
    statusName: Optional[str] = Field(None, description="Target status name (e.g. Done, In Progress)")
    severityName: Optional[str] = Field(None, description="Target severity (e.g. Critical, High, Medium, Low)")
    ticketTypeName: Optional[str] = Field(None, description="Target ticket type (e.g. Bug, Feature, Task)")
    assigneeName: Optional[str] = Field(None, description="Target assignee name (e.g. Udara, Me, Unassigned)")
    queueName: Optional[str] = Field(None, description="Target queue/department")
    completionDate: Optional[str] = Field(None, description="Target due date (ISO string YYYY-MM-DD)")
    plannedEffort: Optional[float] = Field(None, description="Target effort in hours")

class TicketMutationFilter(BaseModel):
    assignee: Optional[str] = Field(None, description="Assignee filter name, e.g. Reshan, me")
    status: Optional[str] = Field(None, description="Status filter name")
    severity: Optional[str] = Field(None, description="Severity filter name")
    ticketType: Optional[str] = Field(None, description="Ticket type filter")
    all: Optional[bool] = Field(None, description="Whether all matching tickets should be considered")

class MutationOption(BaseModel):
    id: Optional[int] = None
    label: str
    value: str
    actionText: str

class MutationPayload(BaseModel):
    action: str = "UPDATE_TICKET"  # or "UPDATE_TASK"
    entityType: str = "ticket"     # "ticket" | "task"
    status: str = "READY"          # "READY" | "NEEDS_INPUT"
    targets: List[str] = Field(default_factory=list, description="Target ticket codes or IDs, e.g. ['PTS-12']")
    filter: Optional[TicketMutationFilter] = None
    changes: TicketMutationChanges = Field(default_factory=TicketMutationChanges)
    options: List[MutationOption] = Field(default_factory=list)
    summary: str = Field("", description="Conversational explanation of changes")
    warnings: List[str] = Field(default_factory=list)
