import os
import time
import json
import re
from sqlalchemy import create_engine, text
from langchain_core.tools import tool
from dotenv import load_dotenv

load_dotenv()
pg_db_url = os.getenv("PG_DB_URL")

# Global limit for sample/preview queries (defaults to 200, configurable via SAMPLE_LIMIT env var)
SAMPLE_LIMIT = int(os.getenv("SAMPLE_LIMIT", 200))

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

_TABLE_COLUMNS_CACHE: dict[str, set[str]] = {}

def _get_table_columns(conn, table_name: str) -> set[str]:
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

def get_db_engine(url: str):
    if not url:
        return None
    normalized = url.strip()
    if normalized.startswith("postgres://"):
        normalized = "postgresql://" + normalized[len("postgres://"):]
    try:
        return create_engine(normalized)
    except Exception as e:
        err_msg = str(e)
        if "psycopg" in err_msg and "postgresql+psycopg://" in normalized:
            return create_engine(normalized.replace("postgresql+psycopg://", "postgresql+psycopg2://"))
        if "psycopg2" in err_msg and "postgresql+psycopg2://" in normalized:
            return create_engine(normalized.replace("postgresql+psycopg2://", "postgresql+psycopg://"))
        if "psycopg" in err_msg and normalized.startswith("postgresql://"):
            return create_engine(normalized.replace("postgresql://", "postgresql+psycopg2://", 1))
        raise

def _build_and_execute_sql(
    table_name: str, 
    columns: list[str], 
    filters: dict = None, 
    force: bool = False,
    company_id: int = None,
    scope: dict = None
) -> str:
    start_time = time.time()
    
    if not pg_db_url:
        return "Error: PG_DB_URL not configured."
    
    try:
        engine = get_db_engine(pg_db_url)
        with engine.connect() as conn:
            dim_cols = [
                "reporting_resource_name", "reporting_resource_id", "pool_list",
                "resource_pool_name", "resource_type", "email", "daily_working_hours",
                "hourly_cost", "daily_cost", "skill_list"
            ]
            is_resource_table = table_name in [
                "gold.bridge_resource_skill",
                "gold.bridge_resource_pool",
                "gold.fct_resource_day",
                "gold.mart_resource_workload_current"
            ]
            from_clause = f"{table_name} b LEFT JOIN gold.dim_resource r ON r.resource_id = b.resource_id" if is_resource_table else table_name
            table_cols = _get_table_columns(conn, table_name)
            
            # Filter out raw IDs (person_id, resource_id, company_id) so the user only sees human names and meaningful metrics
            cleaned_cols = [c for c in (columns or []) if not (c.endswith('_id') or c in ['id', 'person_id', 'company_id'])]
            
            is_fct_day = (table_name == "gold.fct_resource_day")
            # Aggregation mode for gold.fct_resource_day: if daily grain columns (date_day, resource_day_id) are not requested,
            # we aggregate over the timeframe (e.g. weekly or multi-week capacity per resource) to prevent duplicate daily rows.
            is_fct_aggregated = is_fct_day and not any(c in cleaned_cols for c in ["date_day", "resource_day_id"])

            fct_aggregations = {
                "capacity_hrs": "SUM(b.capacity_hrs) AS capacity_hrs",
                "planned_hrs": "SUM(b.planned_hrs) AS planned_hrs",
                "logged_hrs": "SUM(b.logged_hrs) AS logged_hrs",
                "overdue_hrs": "SUM(b.overdue_hrs) AS overdue_hrs",
                "undated_hrs": "SUM(b.undated_hrs) AS undated_hrs",
                "free_hrs": "SUM(b.capacity_hrs - b.planned_hrs) AS free_hrs",
                "available_hrs": "SUM(b.capacity_hrs - b.planned_hrs) AS free_hrs",
                "capacity_cost": "SUM(b.capacity_cost) AS capacity_cost",
                "logged_cost": "SUM(b.logged_cost) AS logged_cost",
            }

            group_by_cols = ["r.resource_id", "r.full_name"]

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
                    elif is_fct_aggregated and col in fct_aggregations:
                        formatted_cols.append(fct_aggregations[col])
                    elif is_fct_day and not is_fct_aggregated and col in ["free_hrs", "available_hrs"]:
                        formatted_cols.append("(b.capacity_hrs - b.planned_hrs) AS free_hrs")
                    elif col in dim_cols:
                        actual_col = "pool_list" if col == "resource_pool_name" else col
                        formatted_cols.append(f"r.{actual_col}")
                        if is_fct_aggregated:
                            group_by_cols.append(f"r.{actual_col}")
                    elif col == "company_week_start_date":
                        formatted_cols.append("b.company_week_start_date")
                        if is_fct_aggregated:
                            group_by_cols.append("b.company_week_start_date")
                    elif table_cols and col in table_cols:
                        formatted_cols.append(f"b.{col}")
                        if is_fct_aggregated and col not in fct_aggregations:
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
            
            # Deduplicate group_by_cols while preserving order
            group_by_cols = list(dict.fromkeys(group_by_cols))

            # Build the WHERE and HAVING clauses safely
            filter_clauses = []
            having_clauses = []
            has_date_filter = False

            if filters:
                for key, value in filters.items():
                    # If company_id is provided in security context, avoid duplicate/conflicting filter
                    if key == "company_id" and company_id is not None:
                        continue

                    # Handle aggregated metric filters (e.g. free_hrs >= 16)
                    if is_fct_aggregated and key in ["free_hrs", "available_hrs", "capacity_hrs", "planned_hrs", "logged_hrs", "overdue_hrs", "undated_hrs"]:
                        metric_expr = "SUM(b.capacity_hrs - b.planned_hrs)" if key in ["free_hrs", "available_hrs"] else f"SUM(b.{key})"
                        if isinstance(value, str):
                            comp_match = re.match(r"^(>=|<=|>|<|!=|<>|=)?\s*(-?\d+(?:\.\d+)?)$", value.strip())
                            if comp_match:
                                op, num = comp_match.groups()
                                op = op or "="
                                having_clauses.append(f"{metric_expr} {op} {num}")
                            else:
                                having_clauses.append(f"{metric_expr} = {value.strip()}")
                        elif isinstance(value, (int, float)):
                            having_clauses.append(f"{metric_expr} = {value}")
                        continue

                    # Check relative date filters
                    if key in ["company_week_start_date", "date_day", "date", "week", "week_start_date"] and isinstance(value, str):
                        v_lower = value.strip().lower()
                        has_date_filter = True
                        target_col = "b.company_week_start_date" if (is_resource_table and "company_week_start_date" in table_cols) else ("b.date_day" if "date_day" in table_cols else (f"b.{key}" if is_resource_table else key))
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

                    if is_resource_table and (key in dim_cols or key == "full_name" or (table_cols and key in table_cols) or (is_fct_day and key in ["free_hrs", "available_hrs"])):
                        pass
                    elif table_cols and key not in table_cols:
                        print(f"[TOOL] Notice: Dropping filter on non-existent column '{key}' for table '{table_name}'")
                        continue

                    # Column expression (prefix with b. or r.)
                    if is_resource_table:
                        if key == "full_name":
                            col_expr = "b.full_name" if (table_cols and "full_name" in table_cols and not is_fct_day) else "r.full_name"
                        elif key in dim_cols:
                            actual_col = "pool_list" if key == "resource_pool_name" else key
                            col_expr = f"r.{actual_col}"
                        elif is_fct_day and not is_fct_aggregated and key in ["free_hrs", "available_hrs"]:
                            col_expr = "(b.capacity_hrs - b.planned_hrs)"
                        else:
                            col_expr = f"b.{key}"
                    else:
                        col_expr = key

                    if isinstance(value, str) and value.upper() in ["CURRENT_DATE"]:
                        filter_clauses.append(f"{col_expr} = {value}")
                    elif isinstance(value, str) and "%" in value:
                        filter_clauses.append(f"{col_expr} ILIKE '{value}'")
                    elif isinstance(value, str):
                        val_trimmed = value.strip()
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
                            filter_clauses.append(f"{col_expr} = '{value}'")
                    elif isinstance(value, bool):
                        filter_clauses.append(f"{col_expr} = {'TRUE' if value else 'FALSE'}")
                    else:
                        filter_clauses.append(f"{col_expr} = {value}")

            # Fallback date filter for fct_resource_day to prevent scanning historical records
            if is_fct_day and not has_date_filter:
                filter_clauses.append("b.company_week_start_date >= DATE_TRUNC('week', CURRENT_DATE)::date")

            # =========================================================================
            # MANDATORY COMPANY FILTER ENFORCEMENT
            # =========================================================================
            effective_company_id = company_id
            if effective_company_id is None and os.getenv("DEFAULT_COMPANY_ID"):
                try:
                    effective_company_id = int(os.getenv("DEFAULT_COMPANY_ID"))
                except ValueError:
                    pass

            if effective_company_id is not None:
                if not table_cols or "company_id" in table_cols:
                    comp_target = "b.company_id" if is_resource_table else "company_id"
                    print(f"[SECURITY] Enforcing Company ID filter: {comp_target} = {int(effective_company_id)} on table '{table_name}'")
                    filter_clauses.append(f"{comp_target} = {int(effective_company_id)}")

            where_clause = ""
            if filter_clauses:
                where_clause = " WHERE " + " AND ".join(filter_clauses)
            
            group_by_clause = f" GROUP BY {', '.join(group_by_cols)}" if is_fct_aggregated else ""
            having_clause = f" HAVING {' AND '.join(having_clauses)}" if (is_fct_aggregated and having_clauses) else ""

            order_by_clause = ""
            if is_fct_aggregated:
                if "free_hrs" in cols_str:
                    order_by_clause = " ORDER BY free_hrs DESC"
                elif "capacity_hrs" in cols_str:
                    order_by_clause = " ORDER BY capacity_hrs DESC"
                else:
                    order_by_clause = " ORDER BY r.full_name ASC"

            # 1. First get total count
            if is_fct_aggregated:
                count_sql = f"SELECT COUNT(*) FROM (SELECT r.resource_id FROM {from_clause}{where_clause}{group_by_clause}{having_clause}) sub;"
            else:
                count_sql = f"SELECT COUNT(*) FROM {from_clause}{where_clause};"

            print("[TOOL]===============================================================")
            print("Checking total count query:")
            print(count_sql)
            count_res = conn.execute(text(count_sql))
            total_count = count_res.scalar() or 0
            print(f"Total matching records: {total_count}")
            
            if total_count == 0:
                elapsed = time.time() - start_time
                print(f"Query executed. Forwarding 0 rows to Formatter. | Time: {elapsed:.2f}s\n")
                return "Query returned zero rows."
                
            # 2. Determine sampling limit based on count and force flag
            is_sampled = False
            limit_clause = ""
            if not force and total_count > SAMPLE_LIMIT:
                is_sampled = True
                limit_clause = f" LIMIT {SAMPLE_LIMIT}"
                
            data_sql = f"SELECT {cols_str} FROM {from_clause}{where_clause}{group_by_clause}{having_clause}{order_by_clause}{limit_clause};"
            print("Executing data query:")
            print(data_sql)
            print(f"Force: {force} | Sampled (Limit {SAMPLE_LIMIT}): {is_sampled}")
            
            result = conn.execute(text(data_sql))
            rows = result.fetchall()
            json_rows = [dict(row._mapping) for row in rows]
            
            elapsed = time.time() - start_time
            print(f"Query executed. Forwarding {len(rows)} rows (in TOON format, Total: {total_count}) to Formatter. | Time: {elapsed:.2f}s\n")
            return rows_to_toon(json_rows, name=table_name, total_count=total_count, is_sampled=is_sampled, force=force)
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"SQL Execution Error: {e} | Time: {elapsed:.2f}s\n")
        return "I encountered an issue while trying to fetch your data. Please try rephrasing your request."

@tool
def query_project_data(
    table_name: str, 
    columns: list[str], 
    filters: dict = None, 
    force: bool = False,
    company_id: int = None,
    scope: dict = None
) -> str:
    """Fetch project data by specifying the exact table_name from the schema, a list of columns to retrieve, an optional dictionary of filters (e.g. {'assignee_full_name': '%John%'} for ILIKE matches), and force (set to True ONLY if the user explicitly asks to 'force', 'show all', or override sampling limits)."""
    return _build_and_execute_sql(table_name, columns, filters, force=force, company_id=company_id, scope=scope)

@tool
def query_ticket_data(
    table_name: str, 
    columns: list[str], 
    filters: dict = None, 
    force: bool = False,
    company_id: int = None,
    scope: dict = None
) -> str:
    """Fetch ticket data by specifying the exact table_name from the schema, a list of columns to retrieve, an optional dictionary of filters, and force (set to True ONLY if the user explicitly asks to 'force', 'show all', or override sampling limits)."""
    return _build_and_execute_sql(table_name, columns, filters, force=force, company_id=company_id, scope=scope)

@tool
def query_resource_data(
    table_name: str, 
    columns: list[str], 
    filters: dict = None, 
    force: bool = False,
    company_id: int = None,
    scope: dict = None
) -> str:
    """Fetch resource data by specifying the exact table_name from the schema, a list of columns to retrieve, an optional dictionary of filters, and force (set to True ONLY if the user explicitly asks to 'force', 'show all', or override sampling limits)."""
    return _build_and_execute_sql(table_name, columns, filters, force=force, company_id=company_id, scope=scope)
