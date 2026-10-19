# Synlio AI LangGraph Architecture & Decision Logic

This document details the complete end-to-end flow of the **Synlio AI LangGraph Agent**, including all **decision nodes**, **security gates**, **sampling row limits**, **retry thresholds**, and **page context overrides**.

---

## 1. Complete Architecture Flowchart

```mermaid
flowchart TD
    %% Entry & Agent Preset Bypass
    START([User Prompt / API Request]) --> CheckPreset{Screen Agent Preset?}

    CheckPreset -->|Agent preset provided<br/>e.g. resource_analytics| PresetBypass["<b>Orchestrator Bypass</b><br/>Directly set active_workers from agent<br/>Fast-track dispatch"]
    CheckPreset -->|No preset<br/>General /chat request| OrchestratorNode["<b>Orchestrator Node</b><br/>Classify query into domains:<br/>• project_analytics<br/>• ticket_analytics<br/>• resource_analytics"]

    PresetBypass --> WorkersNode
    OrchestratorNode --> CheckActiveDomains{Active Domains Detected?}

    CheckActiveDomains -->|1 or more domains active| WorkersNode["<b>Parallel Workers Node</b><br/>• User Context: email lookup in gold.dim_resource<br/>• Tier 2 Page Context: space_id, pool_id, parent_task_id<br/>• History Window: last 6 conversation messages<br/>• Retry Notice: schema adjustment advice if round > 1<br/>• Parallel dispatch via asyncio.gather"]
    CheckActiveDomains -->|0 domains: greeting or general chat| FormatterNode

    %% Worker Output Evaluation
    WorkersNode --> CheckToolCalls{Tool Calls Generated?}
    CheckToolCalls -->|No tool calls: direct conversational reply| FormatterNode
    CheckToolCalls -->|Tool calls generated| ToolExecutorNode["<b>Custom Tool Executor Node</b>"]

    %% Tool Execution, Security & Sampling Logic
    subgraph ToolExecutorLogic ["Tool Executor & Query Builder Subgraph"]
        CheckForceKeywords{"Force Keywords Detected?<br/>('force', 'full list', 'all data', 'all tasks', etc.)"}

        CheckForceKeywords -->|Keyword Matched| SetForceTrue["Set force = True<br/>Bypass row limit clause"]
        CheckForceKeywords -->|No Match| SetForceFalse["Set force = False<br/>Apply Standard Row Limits"]

        SetForceTrue --> SecurityTier1["<b>Tier 1: Security Scope (Hard Gate - Non-Overridable)</b><br/>• Enforce company_id = auth_user.company_id<br/>• Enforce space_id IN (allowed_task_space_ids)<br/>• Enforce ticket_space_id IN (allowed_ticket_space_ids)"]
        SetForceFalse --> SecurityTier1

        SecurityTier1 --> CheckScopeOverride{"User Prompt Cross-Space Intent?<br/>('all spaces', 'across teams', 'all projects')"}

        CheckScopeOverride -->|Yes: Cross-space intent| DropPageFilter["<b>Tier 2 Override:</b> Drop Page Context Filter<br/><b>RETAIN Tier 1: space_id IN (allowed)</b>"]
        CheckScopeOverride -->|No: Standard contextual query| ApplyPageFilter["<b>Tier 2 Soft Scope:</b> Apply Page Context Filter<br/>(e.g. space_id = current_page_space_id)"]

        DropPageFilter --> SamplingCheck{"Total Rows > SAMPLE_LIMIT?<br/>(Default SAMPLE_LIMIT = 200)"}
        ApplyPageFilter --> SamplingCheck

        SamplingCheck -->|total_count > 200 AND force is False| AddLimit["Append LIMIT 200 to SQL Query"]
        SamplingCheck -->|force is True OR total_count <= 200| NoLimit["Fetch complete dataset (no LIMIT)"]

        AddLimit --> ExecuteSQL["Execute Parameterized SQL<br/>via SQLAlchemy Connection Pool"]
        NoLimit --> ExecuteSQL

        ExecuteSQL --> ValidateResults{"Validate Query Results<br/>Check for: zero rows, empty [], error"}
    end

    ToolExecutorNode --> CheckForceKeywords

    %% Retry Loop & Threshold
    ValidateResults -->|Valid Data (> 0 rows)| RouteToolOutcome{"Needs Retry?"}
    ValidateResults -->|Empty Data or Error| CheckRetryThreshold{"Current Round < MAX_ROUNDS?<br/>(Default QUERY_ROUND_COUNT = 2)"}

    CheckRetryThreshold -->|Round < 2: Retry Allowed| TriggerRetry["Set needs_retry = True<br/>current_round += 1<br/>Generate retry_feedback with schema tips"]
    CheckRetryThreshold -->|Round >= 2: Retries Exhausted| ExhaustRetry["Set needs_retry = False<br/>Forward empty state to Formatter"]

    TriggerRetry --> RouteToolOutcome
    ExhaustRetry --> RouteToolOutcome

    RouteToolOutcome -->|needs_retry == True| OrchestratorNode
    RouteToolOutcome -->|needs_retry == False| FormatterNode["<b>Formatter Node</b><br/>• Synthesize multi-worker query outputs<br/>• Format rich Markdown: tables, badges, KPIs<br/>• Generate ECharts JSON config (if chartable)"]

    FormatterNode --> END([Stream SSE to Frontend Copilot Drawer / Chat])

    %% Styling
    classDef startEnd fill:#0284c7,stroke:#0369a1,color:#ffffff,font-weight:bold;
    classDef decision fill:#fef3c7,stroke:#d97706,color:#92400e,font-weight:bold;
    classDef nodeClass fill:#f0fdf4,stroke:#16a34a,color:#14532d;
    classDef logicClass fill:#f8fafc,stroke:#64748b,color:#0f172a;
    classDef securityClass fill:#fef2f2,stroke:#dc2626,color:#991b1b,font-weight:bold;

    class START,END startEnd;
    class CheckPreset,CheckActiveDomains,CheckToolCalls,CheckForceKeywords,CheckScopeOverride,SamplingCheck,ValidateResults,CheckRetryThreshold,RouteToolOutcome decision;
    class OrchestratorNode,WorkersNode,ToolExecutorNode,FormatterNode nodeClass;
    class PresetBypass,SetForceTrue,SetForceFalse,DropPageFilter,ApplyPageFilter,AddLimit,NoLimit,ExecuteSQL,TriggerRetry,ExhaustRetry logicClass;
    class SecurityTier1 securityClass;
```

---

## 2. Thresholds, Limits & Logic Reference Table

| Logic / Threshold | Configuration / Source | Default Value | Trigger Condition | Behavior |
| :--- | :--- | :--- | :--- | :--- |
| **Max Retry Rounds** | Env `QUERY_ROUND_COUNT` in `app/agent/graph.py` | `2` rounds | Tool output contains `"zero rows"`, `"[0]: []"`, `"error"`, or missing tool | If `current_round < 2`, loops back to orchestrator/workers with `retry_feedback` instructing the LLM to use wildcards (`%val%`), relax filters, or re-verify columns. If `round >= 2`, halts retry and forwards to Formatter. |
| **Sample Row Limit** | Env `SAMPLE_LIMIT` in `app/agent/tools.py` | `200` rows | `total_count > SAMPLE_LIMIT` and `force == False` | Automatically runs a `COUNT(*)` probe. If records exceed `200`, appends `LIMIT 200` to prevent LLM token overflow while informing user of total count. |
| **Force Keyword Override** | `full_list_keywords` list in `app/agent/graph.py` | Keyword list | User query contains `"force"`, `"full list"`, `"complete list"`, `"all records"`, `"bring all data"`, `"show all"`, `"view all"`, `"see all"`, `"all data"`, `"all tasks"`, `"all tickets"` | Sets `force = True`, completely bypassing the `LIMIT 200` clause so the entire dataset is queried and analyzed. |
| **Conversation History Window** | `workers.py` line 285 | `6` messages | When generating SQL queries for follow-up questions | Slices `state["messages"][-6:]` into conversation history context so follow-up queries (*"How about John?"*, *"Break that down by status"*) retain context without bloating prompt tokens. |
| **Tier 1 Security Scope** | Authenticated User Token / Scope in `app/agent/tools.py` | Hard Gate (non-overridable) | Every database query execution | Injects `company_id = :company_id`, `space_id IN (:allowed_task_space_ids)`, and `ticket_space_id IN (:allowed_ticket_space_ids)`. **User cannot bypass this gate under any circumstances.** |
| **Tier 2 Page Context (Soft Scope)** | `context` dict from frontend Copilot side panel in `app/agent/workers.py` | Current Page IDs | Side drawer opened on specific task/ticket/pool page | Automatically injects `current_space_id` or `current_pool_id` into prompt and query filters so answers default to the user's active screen. |
| **Tier 2 Scope Override Rule** | Natural Language Intent Detection | User explicitly requests cross-space data | Query phrases: *"all spaces"*, *"across teams"*, *"all projects"*, *"compare spaces"* | Drops the page-specific filter (`current_space_id`), but **strictly retains Tier 1** (`space_id IN allowed_spaces`) so the user only sees data from spaces they belong to. |
| **Agent Preset Bypass** | Request param `agent` in `app/api/chat.py` | Optional preset | Preset passed (e.g. `resource_analytics`) | Bypasses Orchestrator classification completely, directly setting `active_workers` to the preset and saving ~1.5s latency. |
