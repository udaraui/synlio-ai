import os
import time
import json
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
    elif force and total_count is not None:
        header_line = f"{name}[{len(rows)} of {total_count} total records (FULL LIST)]{{{','.join(headers)}}}:"
    else:
        header_line = f"{name}[{len(rows)}]{{{','.join(headers)}}}:"
        
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
            f"You MUST inform the user in your Overview section in a polite, user-friendly tone (do NOT use the word 'force'): "
            f"'Found {total_count} records matching your request. Here is a preview of {sample_count} sample records. "
            f"You can explore the data at an aggregate level (such as summary charts), or let me know if you would like to view the complete list.'\n"
            f"At the very end of your response, append: `<!--preview_count:{total_count}-->`]"
        )
    return "\n".join(lines)

def _build_and_execute_sql(table_name: str, columns: list[str], filters: dict = None, force: bool = False) -> str:
    start_time = time.time()
    
    # Build the SELECT clause
    cols_str = ", ".join(columns) if columns else "*"
    
    # Build the WHERE clause
    where_clause = ""
    if filters:
        filter_clauses = []
        for key, value in filters.items():
            if isinstance(value, str) and value.upper() in ["CURRENT_DATE"]:
                filter_clauses.append(f"{key} = {value}")
            elif isinstance(value, str) and "%" in value:
                filter_clauses.append(f"{key} ILIKE '{value}'")
            elif isinstance(value, str):
                filter_clauses.append(f"{key} = '{value}'")
            elif isinstance(value, bool):
                filter_clauses.append(f"{key} = {1 if value else 0}")
            else:
                filter_clauses.append(f"{key} = {value}")
        if filter_clauses:
            where_clause = " WHERE " + " AND ".join(filter_clauses)
            
    if not pg_db_url:
        return "Error: PG_DB_URL not configured."
    
    try:
        engine = create_engine(pg_db_url)
        with engine.connect() as conn:
            # 1. First get total count
            count_sql = f"SELECT COUNT(*) FROM {table_name}{where_clause};"
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
                
            data_sql = f"SELECT {cols_str} FROM {table_name}{where_clause}{limit_clause};"
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
def query_project_data(table_name: str, columns: list[str], filters: dict = None, force: bool = False) -> str:
    """Fetch project data by specifying the exact table_name from the schema, a list of columns to retrieve, an optional dictionary of filters (e.g. {'assignee_full_name': '%John%'} for ILIKE matches), and force (set to True ONLY if the user explicitly asks to 'force', 'show all', or override sampling limits)."""
    return _build_and_execute_sql(table_name, columns, filters, force=force)

@tool
def query_ticket_data(table_name: str, columns: list[str], filters: dict = None, force: bool = False) -> str:
    """Fetch ticket data by specifying the exact table_name from the schema, a list of columns to retrieve, an optional dictionary of filters, and force (set to True ONLY if the user explicitly asks to 'force', 'show all', or override sampling limits)."""
    return _build_and_execute_sql(table_name, columns, filters, force=force)

@tool
def query_resource_data(table_name: str, columns: list[str], filters: dict = None, force: bool = False) -> str:
    """Fetch resource data by specifying the exact table_name from the schema, a list of columns to retrieve, an optional dictionary of filters, and force (set to True ONLY if the user explicitly asks to 'force', 'show all', or override sampling limits)."""
    return _build_and_execute_sql(table_name, columns, filters, force=force)
