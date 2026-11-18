from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field, ConfigDict

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

class QueryFilterCondition(BaseModel):
    field: str = Field(..., description="Target property, e.g. statusBase, status, severity, assignee, queue, ticketType, code, name, query, startDate, dueDate")
    operator: str = Field("eq", description="Condition operator: eq, neq, in, not_in, like, ilike, gte, lte, is_null, is_not_null, fuzzy")
    value: Any = Field(None, description="Operand value or array of values")

class SemanticQueryPlan(BaseModel):
    entityType: str = Field("ticket", description="'ticket' or 'task'")
    targetScope: str = Field("all", description="'all', 'single', 'children', or 'direct'")
    logic: str = Field("AND", description="'AND' or 'OR'")
    conditions: List[QueryFilterCondition] = Field(default_factory=list)
    mutation: Dict[str, Any] = Field(default_factory=dict)
    summary: str = Field("", description="Conversational summary of query and proposed mutation")

class MutationPayload(BaseModel):
    action: str = "UPDATE_TICKET"  # "UPDATE_TICKET" or "UPDATE_TASK"
    entityType: str = "ticket"     # "ticket" | "task"
    status: str = "READY"          # "READY" | "NEEDS_INPUT"
    targets: List[str] = Field(default_factory=list, description="Target ticket or task codes, e.g. ['PTS-12'] or ['PRJ-10']")
    filter: Optional[Dict[str, Any]] = None
    queryPlan: Optional[SemanticQueryPlan] = None
    changes: Dict[str, Any] = Field(default_factory=dict)
    options: List[MutationOption] = Field(default_factory=list)
    summary: str = Field("", description="Conversational explanation of changes")
    warnings: List[str] = Field(default_factory=list)
    attemptedHierarchyChange: bool = Field(False, description="Flagged if user tried to alter task hierarchy level")


class MutationFilterExtraction(BaseModel):
    """Structured filter criteria extracted from a mutation command."""
    assignee: Optional[str] = Field(None, description="Source assignee filter (e.g. 'Reshan', 'me', 'Unassigned')")
    status: Optional[str] = Field(None, description="Source status filter (e.g. 'Open', 'In Progress', 'unresolved')")
    severity: Optional[str] = Field(None, description="Source severity/priority filter (e.g. 'High', 'Low', 'Medium', 'Critical')")
    ticketType: Optional[str] = Field(None, description="Source ticket type filter")
    queue: Optional[str] = Field(None, description="Source queue/department filter")
    query: Optional[str] = Field(None, description="Title or fuzzy name search string (without trailing words like 'task' or 'ticket')")
    hierarchyLevelName: Optional[str] = Field(None, description="Configured hierarchy level name when user targets a specific level (e.g. 'Project', 'Epic', 'Milestone', 'Subtask'), null for generic 'tasks'")
    hierarchyLevelPosition: Optional[int] = Field(None, description="1-based hierarchy level position when specified structurally (1 for root/1st level, 2 for 'child of root' / 'second level' / 'level 2', 3 for 'third level')")
    codeRangeStart: Optional[int] = Field(None, description="Starting numeric code when user specifies a code range (e.g. 5 for 'ticket code 5 to 10' or 'TSU-5 to TSU-10')")
    codeRangeEnd: Optional[int] = Field(None, description="Ending numeric code when user specifies a code range (e.g. 10 for 'ticket code 5 to 10' or 'TSU-5 to TSU-10')")
    all: Optional[bool] = Field(None, description="True if user requested all/every matching items")


class MutationChangesExtraction(BaseModel):
    """Structured target field changes extracted from a mutation command."""
    name: Optional[str] = Field(None, description="New title/name if renaming")
    description: Optional[str] = Field(None, description="New description if requested")
    statusName: Optional[str] = Field(None, description="New status (e.g. 'To Do', 'To Start', 'In Progress', 'In Review', 'Done', 'Closed', 'Open', 'Resolved')")
    severityName: Optional[str] = Field(None, description="New severity/priority ONLY when changing priority (not when filtering by priority)")
    ticketTypeName: Optional[str] = Field(None, description="New ticket type (e.g. 'Bug', 'CRN', 'Service Request', 'Maintenance', 'Task')")
    assigneeName: Optional[str] = Field(None, description="New assignee name (e.g. 'Udara', 'me', 'Unassigned')")
    queueName: Optional[str] = Field(None, description="New queue/department name")
    startDate: Optional[str] = Field(None, description="New absolute start date (YYYY-MM-DD)")
    dueDate: Optional[str] = Field(None, description="New absolute due/end date (YYYY-MM-DD)")
    startDateOffsetDays: Optional[int] = Field(None, description="Signed day offset for relative start date (e.g. -12 for '12 days before end date', +2 for 'forward by 2 days')")
    startDateRelativeTo: Optional[Literal["startDate", "dueDate"]] = Field(None, description="Reference field for startDateOffsetDays")
    dueDateOffsetDays: Optional[int] = Field(None, description="Signed day offset for relative due/end date (e.g. +2 for 'forward by 2 days', +5 for '5 days after start date')")
    dueDateRelativeTo: Optional[Literal["startDate", "dueDate"]] = Field(None, description="Reference field for dueDateOffsetDays")
    estimateEffort: Optional[float] = Field(None, description="New effort estimate in hours")


class MutationExtractionResult(BaseModel):
    """Structured LLM output for extract_mutation."""
    action: Literal["UPDATE_TASK", "UPDATE_TICKET"] = Field("UPDATE_TICKET", description="UPDATE_TASK or UPDATE_TICKET")
    entityType: Literal["task", "ticket"] = Field("ticket", description="'task' or 'ticket'")
    targets: List[str] = Field(default_factory=list, description="Explicit item codes only; empty list when using filter criteria")
    attemptedHierarchyChange: bool = Field(False, description="True only if user explicitly asked to alter task hierarchy level")
    filter: Optional[MutationFilterExtraction] = Field(None, description="Filter criteria when targeting by attributes or title")
    changes: MutationChangesExtraction = Field(default_factory=MutationChangesExtraction, description="Requested field updates")
    summary: str = Field("", description="Concise summary of the prepared update draft")


class DeltaDraftItem(BaseModel):
    """Structured draft item extracted for ticket or task creation/cloning."""
    model_config = ConfigDict(extra="allow")

    name: Optional[str] = Field(None, description="Title/name of the ticket or task")
    description: Optional[str] = Field(None, description="Detailed description")
    entityType: Optional[Literal["ticket", "task"]] = Field(None, description="'ticket' or 'task'")
    ticketSpaceName: Optional[str] = Field(None, description="Target ticket space name")
    taskSpaceName: Optional[str] = Field(None, description="Target task space name")
    ticketTypeName: Optional[str] = Field(None, description="Ticket type (e.g. Bug, Maintenance, Task)")
    severityName: Optional[str] = Field(None, description="Severity/priority (Critical, High, Medium, Low)")
    statusName: Optional[str] = Field(None, description="Initial status name")
    assigneeName: Optional[str] = Field(None, description="Assignee name or 'me'")
    queueName: Optional[str] = Field(None, description="Target queue name")
    completionDate: Optional[str] = Field(None, description="Ticket due date (YYYY-MM-DD)")
    plannedEffort: Optional[float] = Field(None, description="Ticket planned effort in hours")
    startDate: Optional[str] = Field(None, description="Task start date (YYYY-MM-DD)")
    dueDate: Optional[str] = Field(None, description="Task due/end date (YYYY-MM-DD)")
    estimateEffort: Optional[float] = Field(None, description="Task effort estimate in hours")
    parentTaskName: Optional[str] = Field(None, description="Parent project or task name/code")
    parentTaskId: Optional[int] = Field(None, description="Parent task ID if known")
    hierarchyLevelName: Optional[str] = Field(None, description="Hierarchy level name (Project, Task, Subtask)")
    hierarchyLevelSequence: Optional[int] = Field(None, description="Hierarchy level sequence")
    sourceTicketCode: Optional[str] = Field(None, description="Source ticket code when cloning")
    sourceTaskCode: Optional[str] = Field(None, description="Source task/project code when cloning")
    cloneChildren: Optional[bool] = Field(None, description="Whether to clone child tasks")
    isCreateNewLevel1: Optional[bool] = Field(None, description="True if creating a new root Level 1 project")


class DeltaExtractionResult(BaseModel):
    """Structured LLM output for extract_delta (supports 1 or more drafts)."""
    drafts: List[DeltaDraftItem] = Field(default_factory=list, description="List of extracted ticket or task drafts")
