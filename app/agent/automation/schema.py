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

class TaskDraft(BaseModel):
    """Structured draft of a task aligned 1:1 with CreateTaskDto."""
    name: Optional[str] = Field(None, description="The name / title of the task")
    description: Optional[str] = Field(None, description="Detailed description of the task")
    taskSpaceName: Optional[str] = Field(None, description="Name of the target task space")
    taskSpaceId: Optional[int] = Field(None, description="Resolved ID of the task space")
    parentTaskId: Optional[int] = Field(None, description="Parent task ID if nested inside a project/task")
    parentTaskName: Optional[str] = Field(None, description="Name or code of the parent task")
    hierarchyLevelConfigId: Optional[int] = Field(None, description="Resolved hierarchy level config ID")
    hierarchyLevelName: Optional[str] = Field(None, description="Hierarchy level name, e.g. Project, Initiative, Task, Milestone")
    hierarchyLevelSequence: Optional[int] = Field(None, description="Sequence: 1 for root project/initiative, 2 for task, etc.")
    statusName: Optional[str] = Field(None, description="Status name, e.g. Open, To Do, In Progress")
    statusId: Optional[int] = Field(None, description="Resolved ID of the status")
    severityName: Optional[str] = Field(None, description="Severity / Priority: Critical, High, Medium, Low")
    severityId: Optional[int] = Field(None, description="Resolved ID of the severity")
    assigneeName: Optional[str] = Field(None, description="Name of the assigned resource")
    assigneeId: Optional[int] = Field(None, description="Resolved ID of the resource")
    startDate: Optional[str] = Field(None, description="Start date (YYYY-MM-DD)")
    dueDate: Optional[str] = Field(None, description="Due date (YYYY-MM-DD)")
    estimateEffort: Optional[float] = Field(None, description="Estimated effort in hours")
    sourceTaskCode: Optional[str] = Field(None, description="Source task or project code to clone (e.g. PTS-P7)")
    cloneChildren: Optional[bool] = Field(None, description="Whether to recursively clone all child tasks and subtasks")
    isCreateNewLevel1: Optional[bool] = Field(None, description="Flag indicating new root project creation")

class NeedsFieldOption(BaseModel):
    id: Optional[int] = None
    name: str
    prefix: Optional[str] = None

class NeedsField(BaseModel):
    field: str
    label: str
    options: List[NeedsFieldOption] = []

class AutomationDraftPayload(BaseModel):
    action: str = "CREATE_TICKET"  # "CREATE_TICKET" or "CREATE_TASK"
    entityType: str = "ticket"     # "ticket" or "task"
    status: str = "DRAFT"
    summary: str
    draft: Optional[Dict[str, Any]] = None
    drafts: List[Dict[str, Any]] = []
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

class TaskMutationChanges(BaseModel):
    name: Optional[str] = Field(None, description="New task title")
    description: Optional[str] = Field(None, description="New task description")
    statusName: Optional[str] = Field(None, description="Target status name (e.g. Done, In Progress)")
    severityName: Optional[str] = Field(None, description="Target severity (e.g. Critical, High, Medium, Low)")
    assigneeName: Optional[str] = Field(None, description="Target resource name (e.g. Udara, Me, Unassigned)")
    startDate: Optional[str] = Field(None, description="Target start date (ISO string YYYY-MM-DD)")
    dueDate: Optional[str] = Field(None, description="Target due date (ISO string YYYY-MM-DD)")
    estimateEffort: Optional[float] = Field(None, description="Target estimate effort in hours")

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
    action: str = "UPDATE_TICKET"  # "UPDATE_TICKET" or "UPDATE_TASK"
    entityType: str = "ticket"     # "ticket" | "task"
    status: str = "READY"          # "READY" | "NEEDS_INPUT"
    targets: List[str] = Field(default_factory=list, description="Target ticket or task codes, e.g. ['PTS-12'] or ['PRJ-10']")
    filter: Optional[Dict[str, Any]] = None
    changes: Dict[str, Any] = Field(default_factory=dict)
    options: List[MutationOption] = Field(default_factory=list)
    summary: str = Field("", description="Conversational explanation of changes")
    warnings: List[str] = Field(default_factory=list)
    attemptedHierarchyChange: bool = Field(False, description="Flagged if user tried to alter task hierarchy level")
