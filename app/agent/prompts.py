import os
import yaml

def load_file(path: str) -> str:
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    # Also attempt relative to app/agent directory
    rel_path = os.path.join(os.path.dirname(__file__), path.replace("app/agent/", ""))
    if os.path.exists(rel_path):
        with open(rel_path, 'r', encoding='utf-8') as f:
            return f.read()
    print(f"Warning: Prompt file {path} not found.")
    return ""

def get_parsed_semantic_layer():
    try:
        schema_path = os.path.join(os.path.dirname(__file__), "schema", "semantic_layer.yml")
        if not os.path.exists(schema_path):
            schema_path = "app/agent/schema/semantic_layer.yml"
        with open(schema_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"Error parsing yaml: {e}")
        return {}

SEMANTIC_LAYER_DATA = get_parsed_semantic_layer()

# Build the lightweight Orchestrator Context (No tables or KPIs)
def build_orchestrator_context() -> str:
    modules = get_parsed_semantic_layer().get("modules", {})
    context = []
    for module_name, details in modules.items():
        context.append(f"Module: {module_name}")
        context.append(f" Domain Focus: {details.get('domain_focus', '').strip()}")
        context.append("-" * 30)
    return "\n".join(context)

def get_orchestrator_config():
    try:
        config_path = os.path.join(os.path.dirname(__file__), "config", "orchestrator", "system_prompt.yaml")
        if not os.path.exists(config_path):
            config_path = "app/agent/config/orchestrator/system_prompt.yaml"
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Error loading orchestrator config: {e}")
        return {}

ORCHESTRATOR_CONFIG = get_orchestrator_config()
ORCHESTRATOR_QUESTIONS = ORCHESTRATOR_CONFIG.get("questions", {})

def get_gate_keeper_config():
    try:
        config_path = os.path.join(os.path.dirname(__file__), "config", "gate_keeper", "system_prompt.yaml")
        if not os.path.exists(config_path):
            config_path = "app/agent/config/gate_keeper/system_prompt.yaml"
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Error loading gate_keeper config: {e}")
        return {}

GATE_KEEPER_CONFIG = get_gate_keeper_config()
GATE_KEEPER_QUESTIONS = GATE_KEEPER_CONFIG.get("questions", {})
GATE_KEEPER_THRESHOLD = GATE_KEEPER_CONFIG.get("threshold", 0.75)
GATE_KEEPER_REFUSAL_MESSAGE = GATE_KEEPER_CONFIG.get(
    "refusal_message",
    "I cannot process this request as it was flagged for potential prompt manipulation or safety policy violations. If you believe this is an error, please rephrase your question."
)

def get_gate_keeper_system_prompt() -> str:
    return load_file("app/agent/config/gate_keeper/system_prompt.md").strip()

ORCHESTRATOR_SYSTEM_PROMPT = f"""
SEMANTIC LAYER MODULES (METADATA ONLY):
{build_orchestrator_context()}
""".strip()

def get_formatter_prompt() -> str:
    return load_file("app/agent/config/formatter/system_prompt.md").strip()

def get_worker_prompt(domain_name: str) -> str:
    domain_dir = f"app/agent/config/{domain_name}"
    
    system_prompt = load_file(f"{domain_dir}/system_prompt.md")
    skills = load_file(f"{domain_dir}/skills.md")
    guard_rails = load_file(f"{domain_dir}/guard_rails.md")
    
    # Extract ONLY the schema (tables and kpis) for this specific domain to save tokens
    modules = get_parsed_semantic_layer().get("modules", {})
    domain_data = modules.get(domain_name, {})
    
    # Strip out verbose metadata to save IN tokens. Only pass table names, column names, and KPI names.
    minified_domain_data = {}
    
    if "tables" in domain_data:
        minified_tables = []
        for table in domain_data["tables"]:
            min_table = {"name": table.get("name")}
            if "columns" in table:
                cols = []
                for col in table.get("columns", []):
                    if isinstance(col, dict) and col.get("name"):
                        desc = col.get("description")
                        if desc:
                            cols.append({col["name"]: desc})
                        else:
                            cols.append(col["name"])
                min_table["columns"] = cols
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

SEMANTIC LAYER SCHEMA (ONLY FOR {domain_name.upper()}):
{domain_schema_str}

{system_prompt}
""".strip()

def get_worker_user_context(
    user_email: str = None,
    user_id: str = None,
    company_id: int = None,
    user_name: str = None,
    user_manager: str = None,
    user_pool: str = None,
) -> str:
    template = load_file("app/agent/config/worker/system_prompt.md")
    if not template:
        return ""
    
    return template.format(
        user_email=user_email or "N/A",
        user_id=user_id or "N/A",
        company_id=company_id if company_id is not None else "N/A",
        user_name=user_name or "N/A",
        user_manager=user_manager or "N/A",
        user_pool=user_pool or "N/A",
    ).strip()
