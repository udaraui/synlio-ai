import os
import time
import json
import re
import functools
from typing import Optional, Set, List, Dict, Any, Tuple
from sqlalchemy import create_engine, text
from langchain_core.tools import tool
from dotenv import load_dotenv

from app.agent.telemetry import record_executed_query

load_dotenv()

pg_db_url = os.getenv("PG_DB_URL")
if not pg_db_url:
    print("[ENV ERROR] PG_DB_URL is not set in environment (.env)!")

sample_limit_raw = os.getenv("SAMPLE_LIMIT")
if not sample_limit_raw:
    print("[ENV ERROR] SAMPLE_LIMIT is not set in environment (.env)!")
SAMPLE_LIMIT = int(sample_limit_raw) if sample_limit_raw else None

# =============================================================================
# 1. DATABASE ENGINE & SCHEMA CACHE
# =============================================================================

@functools.lru_cache(maxsize=4)
def get_db_engine(url: str):
    """
    Creates and caches an SQLAlchemy Engine connection pool.
    Reusing the engine prevents recreating TCP/TLS handshakes on every query.
    """
    if not url:
        return None
    normalized = url.strip()
    if normalized.startswith("postgres://"):
        normalized = "postgresql://" + normalized[len("postgres://"):]
    
    engine_kwargs = {
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 20,
    }

    try:
        return create_engine(normalized, **engine_kwargs)
    except Exception as e:
        err_msg = str(e)
        if "psycopg" in err_msg and "postgresql+psycopg://" in normalized:
            return create_engine(normalized.replace("postgresql+psycopg://", "postgresql+psycopg2://"), **engine_kwargs)
        if "psycopg2" in err_msg and "postgresql+psycopg2://" in normalized:
            return create_engine(normalized.replace("postgresql+psycopg2://", "postgresql+psycopg://"), **engine_kwargs)
        if "psycopg" in err_msg and normalized.startswith("postgresql://"):
            return create_engine(normalized.replace("postgresql://", "postgresql+psycopg2://", 1), **engine_kwargs)
        raise

_TABLE_COLUMNS_CACHE: Dict[str, Set[str]] = {}

def _get_table_columns(conn, table_name: str) -> Set[str]:
    """Retrieves and caches column names for a given table from information_schema."""
    if table_name in _TABLE_COLUMNS_CACHE:
        return _TABLE_COLUMNS_CACHE[table_name]
    try:
        if "." in table_name:
            schema, tbl = table_name.split(".", 1)
            res = conn.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_schema = :s AND table_name = :t"),
                {"s": schema, "t": tbl}
            )
        else:
            res = conn.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": table_name}
            )
        cols = {r[0] for r in res}
        if cols:
            _TABLE_COLUMNS_CACHE[table_name] = cols
        return cols
    except Exception as e:
        print(f"[TOOL] Could not retrieve column schema for {table_name}: {e}")
        return set()

# =============================================================================
# 2. TOON OUTPUT SERIALIZATION
# =============================================================================

def rows_to_toon(
    rows: list[dict], 
    name: str = "records", 
    total_count: int = None, 
    is_sampled: bool = False, 
    force: bool = False
) -> str:
    """Converts a list of dicts to Token-Oriented Object Notation (TOON) to drastically reduce LLM prompt tokens."""
    if not rows:
        return f"{name}[0]: []"
    headers = list(rows[0].keys())
    
    if is_sampled and total_count is not None:
        header_line = f"{name}[{len(rows)} of {total_count} total records (PREVIEW)]{{{','.join(headers)}}}:"
    elif total_count is not None:
        header_line = f"{name}[{len(rows)} of {total_count} total records (FULL LIST)]{{{','.join(headers)}}}:"
    else:
        header_line = f"{name}[{len(rows)} (FULL LIST)]{{{','.join(headers)}}}:"
        
    lines = [header_line]
    for row in rows:
        row_vals = []
        for h in headers:
            val = str(row.get(h, ""))
            if val is None or val == "None":
                val = ""
            elif "," in val or "\n" in val or '"' in val:
                val = '"' + val.replace('"', '""').replace('\n', ' ') + '"'
            row_vals.append(val)
        lines.append("  " + ",".join(row_vals))
        
    if is_sampled and total_count is not None:
        sample_count = len(rows)
        lines.append(
            f"\n[SAMPLING_NOTICE: Total matching records: {total_count}. "
            f"Because total count is high, only {sample_count} sample records are returned above for preview. "
            f"In your Overview section, include a brief natural notice: "
            f"'Showing a preview of {sample_count} out of {total_count} records matching your request. "
            f"Let me know if you would like to view the complete list.'\n"
            f"At the very end of your response, append: `<!--preview_count:{total_count}-->`]"
        )
    else:
        cnt = total_count if total_count is not None else len(rows)
        lines.append(
            f"\n[DATA_NOTICE: All {cnt} matching records are provided above (FULL LIST, NOT SAMPLED). "
            f"In your Overview section, write a natural, informative 1-2 sentence executive overview answering the user's query directly based on the data. "
            f"Do NOT use robotic boilerplate phrases like 'Here is the complete list of all {cnt} records matching your request', and NEVER say 'preview of sample records'. "
            f"Do NOT append <!--preview_count:...--> because all matching data is already shown.]"
        )
    return "\n".join(lines)

# =============================================================================
# 3. SQL CLAUSE BUILDERS (Declarative Semantic-Layer Driven)
# =============================================================================

@functools.lru_cache(maxsize=1)
def get_semantic_table_configs() -> Dict[str, Dict[str, Any]]:
    """
    Extracts declarative table configuration (joins, dimensions, measures)
    from semantic_layer.yml to dynamically compile SQL queries.
    """
    from app.agent.prompts import get_parsed_semantic_layer
    schema_data = get_parsed_semantic_layer()
    configs = {}
    for module_data in schema_data.get("modules", {}).values():
        for table in module_data.get("tables", []):
            name = table.get("name")
            if not name:
                continue
            joins = table.get("joins", [])
            measures = table.get("measures", {})
            dim_cols_map = {}
            for j in joins:
                alias = j.get("alias", "r")
                for d in j.get("dimensions", []):
                    dim_cols_map[d] = alias
            configs[name] = {
                "joins": joins,
                "measures": measures,
                "dim_cols_map": dim_cols_map,
                "has_joins": bool(joins),
                "has_measures": bool(measures),
            }
    return configs

def _resolve_from_and_columns(
    table_name: str, 
    requested_cols: List[str], 
    table_cols: Set[str]
) -> Tuple[str, str, List[str], bool, bool]:
    """
    Builds the FROM clause, selected column expressions, GROUP BY list,
    and flags for resource/fct_day table handling using declarative semantic layer metadata.
    """
    tbl_cfg = get_semantic_table_configs().get(table_name, {})
    joins = tbl_cfg.get("joins", [])
    measures = tbl_cfg.get("measures", {})
    dim_cols_map = tbl_cfg.get("dim_cols_map", {})
    is_resource_table = tbl_cfg.get("has_joins", False)
    is_fct_day = (table_name == "gold.fct_resource_day")
    
    if joins:
        from_parts = [f"{table_name} b"]
        for j in joins:
            alias = j.get("alias", "r")
            join_cond = j.get("join_on") or j.get("on") or j.get(True)
            from_parts.append(f"LEFT JOIN {j['table']} {alias} ON {join_cond}")
        from_clause = " ".join(from_parts)
    else:
        from_clause = table_name

    # Filter out surrogate IDs (person_id, resource_id, company_id) from display
    cleaned_cols = [c for c in (requested_cols or []) if not (c.endswith('_id') or c in ['id', 'person_id', 'company_id'])]

    # Aggregated mode for tables with measures when daily grain is not requested
    is_fct_aggregated = bool(measures) and not any(c in cleaned_cols for c in ["date_day", "resource_day_id"])
    group_by_cols = ["r.resource_id", "r.full_name"] if is_fct_aggregated else []

    if is_resource_table:
        if 'full_name' not in cleaned_cols:
            cleaned_cols.insert(0, 'full_name')

        formatted_cols = []
        for col in cleaned_cols:
            if col == 'full_name':
                if table_cols and 'full_name' in table_cols and not is_fct_day:
                    formatted_cols.append("b.full_name")
                else:
                    formatted_cols.append("r.full_name AS full_name")
            elif is_fct_aggregated and col in measures:
                formatted_cols.append(measures[col])
            elif is_fct_day and not is_fct_aggregated and col in ["free_hrs", "available_hrs"]:
                formatted_cols.append("(b.capacity_hrs - b.planned_hrs) AS free_hrs")
            elif col in dim_cols_map:
                alias = dim_cols_map[col]
                actual_col = "pool_list" if col == "resource_pool_name" else col
                formatted_cols.append(f"{alias}.{actual_col}")
                if is_fct_aggregated:
                    group_by_cols.append(f"{alias}.{actual_col}")
            elif col == "company_week_start_date":
                formatted_cols.append("b.company_week_start_date")
                if is_fct_aggregated:
                    group_by_cols.append("b.company_week_start_date")
            elif table_cols and col in table_cols:
                formatted_cols.append(f"b.{col}")
                if is_fct_aggregated and col not in measures:
                    group_by_cols.append(f"b.{col}")
            elif not table_cols:
                formatted_cols.append(f"b.{col}")
            else:
                print(f"[TOOL] Notice: Dropping non-existent column '{col}' for table '{table_name}'")

        cols_str = ", ".join(formatted_cols) if formatted_cols else "b.*"
    else:
        if table_cols and 'full_name' in table_cols and 'full_name' not in cleaned_cols and any(k in table_name for k in ['resource', 'workload']):
            cleaned_cols.insert(0, 'full_name')
        valid_cols = [c for c in cleaned_cols if (not table_cols or c in table_cols)]
        cols_str = ", ".join(valid_cols) if valid_cols else "*"

    group_by_cols = list(dict.fromkeys(group_by_cols))
    return from_clause, cols_str, group_by_cols, is_resource_table, is_fct_aggregated

def _safe_escape_sql_value(val: str) -> str:
    """Safely escapes single quotes in user-provided or LLM-provided string values."""
    return str(val).replace("'", "''")

def _build_filter_clauses(
    table_name: str,
    filters: Optional[dict],
    context: Optional[dict],
    company_id: Optional[int],
    table_cols: Set[str],
    is_resource_table: bool,
    is_fct_day: bool,
    is_fct_aggregated: bool
) -> Tuple[List[str], List[str]]:
    """
    Builds SQL WHERE and HAVING clauses safely, handling:
    - Contextual auto-filtering (Resource Group, Task Space, Ticket Space)
    - Single quote escaping for strings (prevents SQL injection & apostrophe syntax errors)
    - Relative date parsing ('next_week', 'this_week', 'last_week')
    - Comparison operators (>=, <=, !=, <, >)
    - Aggregated metrics in HAVING clauses
    - Multi-tenant company security scoping
    """
    filter_clauses = []
    having_clauses = []
    has_date_filter = False

    effective_filters = dict(filters) if filters else {}
    tbl_cfg = get_semantic_table_configs().get(table_name, {})
    measures = tbl_cfg.get("measures", {})
    dim_cols_map = tbl_cfg.get("dim_cols_map", {})

    # 1. Apply Contextual Scoping (if not already specified by LLM)
    if context and isinstance(context, dict):
        rg_name = context.get("resource_group_name") or context.get("resourcePoolName") or context.get("poolName")
        if rg_name and is_resource_table:
            has_pool_filter = any(k in effective_filters for k in ["pool_list", "resource_pool_name", "pool_name", "resource_pool_id"])
            if not has_pool_filter:
                target_pool_col = "resource_pool_name" if table_name == "gold.bridge_resource_pool" else "pool_list"
                effective_filters[target_pool_col] = f"%{rg_name}%"
                if (os.getenv("DEBUG_SQL", "true")).lower() == "true":
                    print(f"  [CONTEXT AUTO-FILTER] Applied Resource Group: {target_pool_col} = %{rg_name}%")

        ts_name = context.get("task_space_name") or context.get("spaceName")
        if ts_name and not is_resource_table:
            has_ts_filter = any(k in effective_filters for k in ["task_space_name", "space_name", "task_space_id", "space_id"])
            if not has_ts_filter:
                if not table_cols or "task_space_name" in table_cols:
                    effective_filters["task_space_name"] = f"%{ts_name}%"
                    if (os.getenv("DEBUG_SQL", "true")).lower() == "true":
                        print(f"  [CONTEXT AUTO-FILTER] Applied Task Space: task_space_name = %{ts_name}%")
                elif "space_name" in table_cols:
                    effective_filters["space_name"] = f"%{ts_name}%"
                    if (os.getenv("DEBUG_SQL", "true")).lower() == "true":
                        print(f"  [CONTEXT AUTO-FILTER] Applied Task Space: space_name = %{ts_name}%")

        tks_name = context.get("ticket_space_name")
        if tks_name and not is_resource_table:
            has_tks_filter = any(k in effective_filters for k in ["ticket_space_name", "ticket_space_id"])
            if not has_tks_filter:
                if not table_cols or "ticket_space_name" in table_cols:
                    effective_filters["ticket_space_name"] = f"%{tks_name}%"
                    if (os.getenv("DEBUG_SQL", "true")).lower() == "true":
                        print(f"  [CONTEXT AUTO-FILTER] Applied Ticket Space: ticket_space_name = %{tks_name}%")

    # 2. Process Effective Filters
    if effective_filters:
        for key, value in effective_filters.items():
            if key == "company_id" and company_id is not None:
                continue

            # Handle aggregated metric filters (e.g. free_hrs >= 16)
            if is_fct_aggregated and key in measures:
                raw_measure = measures[key]
                metric_expr = raw_measure.split(" AS ")[0] if " AS " in raw_measure else raw_measure
                if isinstance(value, str):
                    comp_match = re.match(r"^(>=|<=|>|<|!=|<>|=)?\s*(-?\d+(?:\.\d+)?)$", value.strip())
                    if comp_match:
                        op, num = comp_match.groups()
                        having_clauses.append(f"{metric_expr} {op or '='} {num}")
                    else:
                        safe_val = _safe_escape_sql_value(value.strip())
                        having_clauses.append(f"{metric_expr} = {safe_val}")
                elif isinstance(value, (int, float)):
                    having_clauses.append(f"{metric_expr} = {value}")
                continue

            # Relative date filter keywords
            if key in ["company_week_start_date", "date_day", "date", "week", "week_start_date"] and isinstance(value, str):
                v_lower = value.strip().lower()
                has_date_filter = True
                target_col = (
                    "b.company_week_start_date" if (is_resource_table and "company_week_start_date" in table_cols)
                    else ("b.date_day" if "date_day" in table_cols else (f"b.{key}" if is_resource_table else key))
                )
                if v_lower in ["next_week", "next week"]:
                    filter_clauses.append(f"{target_col} = (DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week')::date")
                    continue
                elif v_lower in ["this_week", "this week", "current_week", "current week"]:
                    filter_clauses.append(f"{target_col} = DATE_TRUNC('week', CURRENT_DATE)::date")
                    continue
                elif v_lower in ["last_week", "last week", "previous_week"]:
                    filter_clauses.append(f"{target_col} = (DATE_TRUNC('week', CURRENT_DATE) - INTERVAL '1 week')::date")
                    continue

            if key in ["company_week_start_date", "date_day"]:
                has_date_filter = True

            # Validate column existence
            if is_resource_table and (key in dim_cols_map or key in measures or key == "full_name" or (table_cols and key in table_cols) or (is_fct_day and key in ["free_hrs", "available_hrs"])):
                pass
            elif table_cols and key not in table_cols:
                print(f"[TOOL] Notice: Dropping filter on non-existent column '{key}' for table '{table_name}'")
                continue

            # Target column expression with proper table alias
            if is_resource_table:
                if key == "full_name":
                    col_expr = "b.full_name" if (table_cols and "full_name" in table_cols and not is_fct_day) else "r.full_name"
                elif key in dim_cols_map:
                    alias = dim_cols_map[key]
                    actual_col = "pool_list" if key == "resource_pool_name" else key
                    col_expr = f"{alias}.{actual_col}"
                elif is_fct_day and not is_fct_aggregated and key in ["free_hrs", "available_hrs"]:
                    col_expr = "(b.capacity_hrs - b.planned_hrs)"
                else:
                    col_expr = f"b.{key}"
            else:
                col_expr = key

            # Value format evaluation with safe string escaping
            if isinstance(value, str):
                val_trimmed = value.strip()
                safe_val = _safe_escape_sql_value(val_trimmed)
                if val_trimmed.upper() in ["CURRENT_DATE"]:
                    filter_clauses.append(f"{col_expr} = {val_trimmed.upper()}")
                elif "%" in val_trimmed:
                    filter_clauses.append(f"{col_expr} ILIKE '{safe_val}'")
                else:
                    comp_match = re.match(r"^(>=|<=|>|<|!=|<>)\s*(-?\d+(?:\.\d+)?)$", val_trimmed)
                    date_comp_match = re.match(r"^(>=|<=|>|<|!=|<>)\s*(\d{4}-\d{2}-\d{2})$", val_trimmed)
                    if comp_match:
                        op, num = comp_match.groups()
                        filter_clauses.append(f"{col_expr} {op} {num}")
                    elif date_comp_match:
                        op, d_val = date_comp_match.groups()
                        filter_clauses.append(f"{col_expr} {op} '{d_val}'")
                    elif val_trimmed.upper() in ["IS NULL", "IS NOT NULL"]:
                        filter_clauses.append(f"{col_expr} {val_trimmed.upper()}")
                    else:
                        filter_clauses.append(f"{col_expr} = '{safe_val}'")
            elif isinstance(value, bool):
                filter_clauses.append(f"{col_expr} = {'TRUE' if value else 'FALSE'}")
            else:
                filter_clauses.append(f"{col_expr} = {value}")

    # Fallback date filter for fct_resource_day to prevent scanning historical records
    if is_fct_day and not has_date_filter:
        filter_clauses.append("b.company_week_start_date >= DATE_TRUNC('week', CURRENT_DATE)::date")

    # Multi-tenant Company Filter Enforcement
    effective_company_id = company_id
    if effective_company_id is None and os.getenv("DEFAULT_COMPANY_ID"):
        try:
            effective_company_id = int(os.getenv("DEFAULT_COMPANY_ID"))
        except ValueError:
            pass

    if effective_company_id is not None:
        if not table_cols or "company_id" in table_cols:
            comp_target = "b.company_id" if is_resource_table else "company_id"
            if (os.getenv("DEBUG_SQL", "true")).lower() == "true":
                print(f"  [SECURITY] Company ID filter: {comp_target} = {int(effective_company_id)}")
            filter_clauses.append(f"{comp_target} = {int(effective_company_id)}")

    return filter_clauses, having_clauses

# =============================================================================
# 4. QUERY RUNNER
# =============================================================================

def _build_and_execute_sql(
    table_name: str, 
    columns: list[str], 
    filters: dict = None, 
    force: bool = False,
    company_id: int = None,
    scope: dict = None,
    context: dict = None
) -> str:
    """Orchestrates query building, count checks, sampling limits, and database execution."""
    start_time = time.time()
    
    if not pg_db_url:
        return "Error: PG_DB_URL not configured."
    
    try:
        engine = get_db_engine(pg_db_url)
        with engine.connect() as conn:
            table_cols = _get_table_columns(conn, table_name)
            
            # 1. Resolve columns, joins, and groupings
            from_clause, cols_str, group_by_cols, is_resource_table, is_fct_aggregated = _resolve_from_and_columns(
                table_name, columns, table_cols
            )

            # 2. Build WHERE and HAVING filter clauses
            filter_clauses, having_clauses = _build_filter_clauses(
                table_name, filters, context, company_id, table_cols,
                is_resource_table=is_resource_table,
                is_fct_day=(table_name == "gold.fct_resource_day"),
                is_fct_aggregated=is_fct_aggregated
            )

            where_clause = (" WHERE " + " AND ".join(filter_clauses)) if filter_clauses else ""
            group_by_clause = f" GROUP BY {', '.join(group_by_cols)}" if is_fct_aggregated else ""
            having_clause = f" HAVING {' AND '.join(having_clauses)}" if (is_fct_aggregated and having_clauses) else ""

            # 3. Order By clause for aggregated workloads
            order_by_clause = ""
            if is_fct_aggregated:
                if "free_hrs" in cols_str:
                    order_by_clause = " ORDER BY free_hrs DESC"
                elif "capacity_hrs" in cols_str:
                    order_by_clause = " ORDER BY capacity_hrs DESC"
                else:
                    order_by_clause = " ORDER BY r.full_name ASC"

            # 4. Check total count
            if is_fct_aggregated:
                count_sql = f"SELECT COUNT(*) FROM (SELECT r.resource_id FROM {from_clause}{where_clause}{group_by_clause}{having_clause}) sub;"
            else:
                count_sql = f"SELECT COUNT(*) FROM {from_clause}{where_clause};"

            count_res = conn.execute(text(count_sql))
            total_count = count_res.scalar() or 0
            
            if total_count == 0:
                try:
                    record_executed_query(table=table_name, sql=count_sql, row_count=0, total_count=0)
                except Exception:
                    pass
                return "Query returned zero rows."

            # 5. Determine sampling limit
            is_sampled = False
            limit_clause = ""
            if not force and SAMPLE_LIMIT and total_count > SAMPLE_LIMIT:
                is_sampled = True
                limit_clause = f" LIMIT {SAMPLE_LIMIT}"

            data_sql = f"SELECT {cols_str} FROM {from_clause}{where_clause}{group_by_clause}{having_clause}{order_by_clause}{limit_clause};"
            if (os.getenv("DEBUG_SQL", "true")).lower() == "true":
                print(f"  [DEBUG SQL] {data_sql}")
            
            result = conn.execute(text(data_sql))
            rows = result.fetchall()
            json_rows = [dict(row._mapping) for row in rows]
            
            try:
                record_executed_query(table=table_name, sql=data_sql, row_count=len(rows), total_count=total_count)
            except Exception:
                pass

            return rows_to_toon(json_rows, name=table_name, total_count=total_count, is_sampled=is_sampled, force=force)
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"SQL Execution Error: {e} | Time: {elapsed:.2f}s\n")
        return "I encountered an issue while trying to fetch your data. Please try rephrasing your request."

# =============================================================================
# 5. SPECIALIZED LANGCHAIN TOOLS
# =============================================================================

@tool
def query_project_data(
    table_name: str, 
    columns: list[str], 
    filters: dict = None, 
    force: bool = False,
    company_id: int = None,
    scope: dict = None,
    context: dict = None
) -> str:
    """Fetch project data by specifying the exact table_name from the schema, a list of columns to retrieve, an optional dictionary of filters (e.g. {'assignee_full_name': '%John%'} for ILIKE matches), and force (set to True ONLY if the user explicitly asks to 'force', 'show all', or override sampling limits)."""
    return _build_and_execute_sql(table_name, columns, filters, force=force, company_id=company_id, scope=scope, context=context)

@tool
def query_ticket_data(
    table_name: str, 
    columns: list[str], 
    filters: dict = None, 
    force: bool = False,
    company_id: int = None,
    scope: dict = None,
    context: dict = None
) -> str:
    """Fetch ticket data by specifying the exact table_name from the schema, a list of columns to retrieve, an optional dictionary of filters, and force (set to True ONLY if the user explicitly asks to 'force', 'show all', or override sampling limits)."""
    return _build_and_execute_sql(table_name, columns, filters, force=force, company_id=company_id, scope=scope, context=context)

@tool
def query_resource_data(
    table_name: str, 
    columns: list[str], 
    filters: dict = None, 
    force: bool = False,
    company_id: int = None,
    scope: dict = None,
    context: dict = None
) -> str:
    """Fetch resource data by specifying the exact table_name from the schema, a list of columns to retrieve, an optional dictionary of filters, and force (set to True ONLY if the user explicitly asks to 'force', 'show all', or override sampling limits)."""
    return _build_and_execute_sql(table_name, columns, filters, force=force, company_id=company_id, scope=scope, context=context)
