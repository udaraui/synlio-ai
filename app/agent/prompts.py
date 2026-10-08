import os
import yaml
import json

def load_file(path: str) -> str:
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    print(f"Warning: Prompt file {path} not found.")
    return ""

def get_parsed_semantic_layer():
    try:
        with open("app/semantic_layer.yml", 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"Error parsing yaml: {e}")
        return {}

SEMANTIC_LAYER_DATA = get_parsed_semantic_layer()

# Build the lightweight Orchestrator Context (No tables or KPIs)
def build_orchestrator_context() -> str:
    modules = SEMANTIC_LAYER_DATA.get("modules", {})
    context = []
    for module_name, details in modules.items():
        context.append(f"Module: {module_name}")
        context.append(f" Domain Focus: {details.get('domain_focus', '').strip()}")
        context.append("-" * 30)
    return "\n".join(context)

ORCHESTRATOR_SYSTEM_PROMPT = f"""
{load_file("app/agent/config/orchestrator/system_prompt.md")}

{load_file("app/agent/config/orchestrator/skills.md")}

{load_file("app/agent/config/orchestrator/guard_rails.md")}

SEMANTIC LAYER MODULES (METADATA ONLY):
{build_orchestrator_context()}
"""

FORMATTER_PROMPT = f"""
{load_file("app/agent/config/formatter/system_prompt.md")}

{load_file("app/agent/config/formatter/skills.md")}

{load_file("app/agent/config/formatter/guard_rails.md")}

CRITICAL INSTRUCTION FOR MARKDOWN TABLES:
When rendering Statuses, Severities, Ticket Types, Priorities, Queues, or Hierarchy Levels inside a Markdown table, you MUST use the following exact template format if the query results provide color and icon data:
`{{{{Type::Name::Color::Icon}}}}`

Types mapping:
- Status: `{{{{Status::Name::Color::Icon}}}}` (e.g. `{{{{Status::In Progress::#3B82F6::loader}}}}`)
- Severity/Priority: `{{{{Severity::Name::Color::Icon}}}}`
- Type: `{{{{Type::Name::Color::Icon}}}}`
- Hierarchy: `{{{{Hierarchy::Name::Color::Icon}}}}`
- Queue: `{{{{Queue::Name::Color::Icon}}}}`

If color or icon is missing, leave it blank (e.g. `{{{{Status::Completed::::}}}}`). Do not use this syntax outside of Markdown tables.
"""

def get_worker_prompt(domain_name: str) -> str:
    domain_dir = f"app/agent/config/{domain_name}"
    
    system_prompt = load_file(f"{domain_dir}/system_prompt.md")
    skills = load_file(f"{domain_dir}/skills.md")
    guard_rails = load_file(f"{domain_dir}/guard_rails.md")
    
    # Extract ONLY the schema (tables and kpis) for this specific domain to save tokens
    modules = SEMANTIC_LAYER_DATA.get("modules", {})
    domain_data = modules.get(domain_name, {})
    
    # Strip out verbose metadata to save IN tokens. Only pass table names, column names, and KPI names.
    minified_domain_data = {}
    
    if "tables" in domain_data:
        minified_tables = []
        for table in domain_data["tables"]:
            min_table = {"name": table.get("name")}
            if "columns" in table:
                # Keep only column names as a simple list of strings
                min_table["columns"] = [col.get("name") for col in table.get("columns", []) if isinstance(col, dict) and col.get("name")]
            minified_tables.append(min_table)
        minified_domain_data["tables"] = minified_tables
        
    if "kpis" in domain_data:
        minified_kpis = []
        for kpi_name, kpi_data in domain_data["kpis"].items():
            min_kpi = {"name": kpi_name}
            minified_kpis.append(min_kpi)
        minified_domain_data["kpis"] = minified_kpis
        
    domain_schema_str = yaml.dump({domain_name: minified_domain_data}, sort_keys=False) if minified_domain_data else "No schema found."
    
    return f"""
{skills}

{guard_rails}

CRITICAL RULES FOR METADATA COLUMNS:
When querying for status, severity, ticket type, or hierarchy levels, you MUST SELECT the associated `_name`, `_color`, and `_icon` columns (e.g. `status_name`, `status_color`) ONLY IF they explicitly exist in the schema for that specific table/view. Do not assume all entities have an `_icon` or `_color` column; check the schema first.
NEVER return `_base` columns (like `status_base`) in the final output; ONLY use `_base` columns for WHERE clause filtering.

SEMANTIC LAYER SCHEMA (ONLY FOR {domain_name.upper()}):
{domain_schema_str}

{system_prompt}
"""
