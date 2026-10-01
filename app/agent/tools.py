import os
import time
import json
from sqlalchemy import create_engine, text
from langchain_core.tools import tool
from dotenv import load_dotenv

load_dotenv()
pg_db_url = os.getenv("PG_DB_URL")
def _build_and_execute_sql(table_name: str, columns: list[str], filters: dict = None) -> str:
    start_time = time.time()
    
    # Build the SELECT clause
    cols_str = ", ".join(columns) if columns else "*"
    
    # Build the basic query
    sql_query = f"SELECT {cols_str} FROM {table_name}"
    
    # Build the WHERE clause
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
            sql_query += " WHERE " + " AND ".join(filter_clauses)
            
    sql_query += ";"
            
    print("[TOOL]===============================================================")
    print("Executing built query:")
    print(sql_query)
    
    if not pg_db_url:
        return "Error: PG_DB_URL not configured."
    
    try:
        engine = create_engine(pg_db_url)
        with engine.connect() as conn:
            result = conn.execute(text(sql_query))
            rows = result.fetchall()
            if not rows:
                elapsed = time.time() - start_time
                print(f"Query executed. Forwarding 0 rows to Formatter. | Time: {elapsed:.2f}s")
                print()
                return "Query returned zero rows."
            json_rows = [dict(row._mapping) for row in rows]
            
            elapsed = time.time() - start_time
            print(f"Query executed. Forwarding {len(rows)} rows to Formatter. | Time: {elapsed:.2f}s")
            print()
            return json.dumps(json_rows, default=str)
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"SQL Execution Error: {e} | Time: {elapsed:.2f}s")
        print()
        return "I encountered an issue while trying to fetch your data. Please try rephrasing your request."

@tool
def query_project_data(table_name: str, columns: list[str], filters: dict = None) -> str:
    """Fetch project data by specifying the exact table_name from the schema, a list of columns to retrieve, and an optional dictionary of filters (e.g. {'assignee_full_name': '%John%'} for ILIKE matches)."""
    return _build_and_execute_sql(table_name, columns, filters)

@tool
def query_ticket_data(table_name: str, columns: list[str], filters: dict = None) -> str:
    """Fetch ticket data by specifying the exact table_name from the schema, a list of columns to retrieve, and an optional dictionary of filters."""
    return _build_and_execute_sql(table_name, columns, filters)

@tool
def query_resource_data(table_name: str, columns: list[str], filters: dict = None) -> str:
    """Fetch resource data by specifying the exact table_name from the schema, a list of columns to retrieve, and an optional dictionary of filters."""
    return _build_and_execute_sql(table_name, columns, filters)
