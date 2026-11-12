# Synlio Co-Pilot Architecture

Unified architecture diagram for the Synlio Co-Pilot AI engine.
Shows end-to-end request flow through GateKeeper, Intent Dispatcher, Automation Subgraph, and Analytics Subgraph.

![Architecture Graph](./graph.png)

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	gate_keeper(gate_keeper)
	dispatcher(dispatcher)
	orchestrator(orchestrator)
	project_analytics(project_analytics)
	ticket_analytics(ticket_analytics)
	resource_analytics(resource_analytics)
	tool_executor(tool_executor)
	formatter(formatter)
	__end__([<p>__end__</p>]):::last
	__start__ --> gate_keeper;
	dispatcher -.-> __end__;
	dispatcher -.-> automation\3aautomation_orchestrator;
	dispatcher -.-> orchestrator;
	dispatcher -.-> project_analytics;
	dispatcher -.-> resource_analytics;
	dispatcher -.-> ticket_analytics;
	gate_keeper -.-> __end__;
	gate_keeper -.-> dispatcher;
	orchestrator -.-> __end__;
	orchestrator -.-> project_analytics;
	orchestrator -.-> resource_analytics;
	orchestrator -.-> ticket_analytics;
	project_analytics -.-> __end__;
	project_analytics -.-> tool_executor;
	resource_analytics -.-> __end__;
	resource_analytics -.-> tool_executor;
	ticket_analytics -.-> __end__;
	ticket_analytics -.-> tool_executor;
	tool_executor -.-> formatter;
	tool_executor -.-> orchestrator;
	automation\3a__end__ --> __end__;
	formatter --> __end__;
	subgraph automation
	automation\3aautomation_orchestrator(automation_orchestrator)
	automation\3aextract_delta(extract_delta)
	automation\3afinalize(finalize)
	automation\3aextract_mutation(extract_mutation)
	automation\3a__end__(<p>__end__</p>)
	automation\3aautomation_orchestrator -.-> automation\3aextract_delta;
	automation\3aautomation_orchestrator -.-> automation\3aextract_mutation;
	automation\3aextract_delta -.-> automation\3a__end__;
	automation\3aextract_delta -.-> automation\3afinalize;
	automation\3aextract_mutation --> automation\3a__end__;
	automation\3afinalize --> automation\3a__end__;
	end
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc

```
