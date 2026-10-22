import os
import functools
from typing import Optional, Dict, Any
import yaml

# =============================================================================
# 1. BASE FILE & YAML LOADERS (Cached)
# =============================================================================

def resolve_path(rel_path: str) -> str:
    """Resolves path relative to this file's directory (app/agent) or the project root."""
    cleaned = rel_path.replace("app/agent/", "").lstrip("/\\")
    direct = os.path.join(os.path.dirname(__file__), cleaned)
    if os.path.exists(direct):
        return direct
    if os.path.exists(rel_path):
        return rel_path
    return direct

@functools.lru_cache(maxsize=64)
def load_text(rel_path: str) -> str:
    """Reads a text or markdown template with in-memory caching."""
    path = resolve_path(rel_path)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    print(f"[PROMPTS WARNING] File not found: {rel_path} (resolved: {path})")
    return ""

# Backward-compatible alias
load_file = load_text

@functools.lru_cache(maxsize=16)
def load_yaml(rel_path: str) -> Dict[str, Any]:
    """Parses a YAML configuration with in-memory caching to eliminate redundant disk I/O."""
    path = resolve_path(rel_path)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"[PROMPTS ERROR] Failed parsing YAML {rel_path}: {e}")
    else:
        print(f"[PROMPTS WARNING] YAML file not found: {rel_path} (resolved: {path})")
    return {}

def get_parsed_semantic_layer() -> Dict[str, Any]:
    """Retrieves the parsed semantic layer schema (cached in memory)."""
    return load_yaml("schema/semantic_layer.yml")

SEMANTIC_LAYER_DATA = get_parsed_semantic_layer()

# =============================================================================
# 2. GATEKEEPER CONFIG & PROMPT
# =============================================================================

GATE_KEEPER_CONFIG = load_yaml("config/gate_keeper/system_prompt.yaml")
GATE_KEEPER_QUESTIONS = GATE_KEEPER_CONFIG.get("questions", {})
GATE_KEEPER_THRESHOLD = GATE_KEEPER_CONFIG.get("threshold", 0.75)
GATE_KEEPER_REFUSAL_MESSAGE = GATE_KEEPER_CONFIG.get(
    "refusal_message",
    "I cannot process this request as it was flagged for potential prompt manipulation or safety policy violations. If you believe this is an error, please rephrase your question."
)

def get_gate_keeper_system_prompt() -> str:
    return load_text("config/gate_keeper/system_prompt.md")

# =============================================================================
# 3. ORCHESTRATOR CONFIG & PROMPT
# =============================================================================

ORCHESTRATOR_CONFIG = load_yaml("config/orchestrator/system_prompt.yaml")
ORCHESTRATOR_QUESTIONS = ORCHESTRATOR_CONFIG.get("questions", {})

def build_orchestrator_context() -> str:
    """Builds lightweight domain metadata (focus areas only) without tables or KPIs."""
    modules = SEMANTIC_LAYER_DATA.get("modules", {})
    context = []
    for module_name, details in modules.items():
        context.append(f"Module: {module_name}")
        context.append(f" Domain Focus: {details.get('domain_focus', '').strip()}")
        context.append("-" * 30)
    return "\n".join(context)

ORCHESTRATOR_SYSTEM_PROMPT = f"""
SEMANTIC LAYER MODULES (METADATA ONLY):
{build_orchestrator_context()}
""".strip()

# =============================================================================
# 4. WORKER DOMAIN PROMPTS & MINIFIED SCHEMA
# =============================================================================

@functools.lru_cache(maxsize=8)
def get_worker_prompt(domain_name: str) -> str:
    """
    Assembles the complete system prompt for a specialized worker node:
    skills + guard rails + minified semantic schema (tables & KPIs only) + system prompt.
    Cached per domain name so schema minification runs only once per domain.
    """
    domain_dir = f"config/{domain_name}"
    system_prompt = load_text(f"{domain_dir}/system_prompt.md")
    skills = load_text(f"{domain_dir}/skills.md")
    guard_rails = load_text(f"{domain_dir}/guard_rails.md")

    # Extract ONLY the schema for this specific domain to minimize LLM input tokens
    modules = SEMANTIC_LAYER_DATA.get("modules", {})
    domain_data = modules.get(domain_name, {})
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
                        cols.append({col["name"]: desc} if desc else col["name"])
                min_table["columns"] = cols
            minified_tables.append(min_table)
        minified_domain_data["tables"] = minified_tables

    if "kpis" in domain_data:
        minified_domain_data["kpis"] = [{"name": k} for k in domain_data["kpis"].keys()]

    domain_schema_str = yaml.dump({domain_name: minified_domain_data}, sort_keys=False) if minified_domain_data else "No schema found."

    return f"""
{skills}

{guard_rails}

SEMANTIC LAYER SCHEMA (ONLY FOR {domain_name.upper()}):
{domain_schema_str}

{system_prompt}
""".strip()

# =============================================================================
# 5. DYNAMIC CONTEXT BUILDERS
# =============================================================================

def get_worker_user_context(
    user_email: str = None,
    user_id: str = None,
    company_id: int = None,
    user_name: str = None,
    user_manager: str = None,
    user_pool: str = None,
) -> str:
    """Formats authenticated user metadata for the worker system prompt."""
    template = load_text("config/worker/system_prompt.md")
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

def format_page_context(context: Optional[dict]) -> str:
    """Formats dynamic UI context into a concise declarative prompt block."""
    if not context or not isinstance(context, dict):
        return ""

    lines = []
    rg_name = context.get("resource_group_name") or context.get("resourcePoolName") or context.get("poolName")
    if rg_name:
        lines.append(f"- Resource Group / Pool: \"{rg_name}\"")

    ts_name = context.get("task_space_name") or context.get("spaceName")
    if ts_name:
        lines.append(f"- Task Space: \"{ts_name}\"")

    tks_name = context.get("ticket_space_name")
    if tks_name:
        lines.append(f"- Ticket Space: \"{tks_name}\"")

    pt_name = context.get("parent_task_name")
    if pt_name:
        lines.append(f"- Parent Task: \"{pt_name}\"")

    if not lines:
        return ""

    return (
        "CURRENT PAGE CONTEXT:\n"
        + "\n".join(lines)
        + "\n(Scope queries and answers to this context unless the user explicitly requests company-wide or cross-space data)."
    )

# =============================================================================
# 6. FORMATTER PROMPT
# =============================================================================

def get_formatter_prompt() -> str:
    """Loads the system prompt for the final response formatter agent."""
    return load_text("config/formatter/system_prompt.md")
