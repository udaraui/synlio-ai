import os
import contextvars
import asyncio
from typing import Optional, Dict, Any
import httpx

# Request-scoped dictionary tracking node-by-node execution telemetry
node_telemetry_tracker = contextvars.ContextVar("node_telemetry_tracker", default={})
last_executed_queries = contextvars.ContextVar("last_executed_queries", default=[])

def init_telemetry():
    node_telemetry_tracker.set({})
    last_executed_queries.set([])

def get_telemetry_tracker() -> Dict[str, Any]:
    return node_telemetry_tracker.get()

def record_executed_query(table: str, sql: str, row_count: int, total_count: int):
    entry = {
        "table": table,
        "sql": sql,
        "rowCount": row_count,
        "totalCount": total_count,
    }
    try:
        tracker = node_telemetry_tracker.get()
        if "_executed_queries" not in tracker:
            tracker["_executed_queries"] = []
        tracker["_executed_queries"].append(entry)
    except Exception:
        pass
    try:
        queries = list(last_executed_queries.get())
        queries.append(entry)
        last_executed_queries.set(queries)
    except Exception as e:
        print(f"[TELEMETRY] Error recording executed query: {e}")

def get_executed_queries():
    try:
        tracker_queries = node_telemetry_tracker.get().get("_executed_queries", [])
        if tracker_queries:
            return tracker_queries
    except Exception:
        pass
    return last_executed_queries.get() or []

def parse_tokens(response, default_model: str = "unknown"):
    """
    Extracts (in_tokens, out_tokens, cache_tokens, total_tokens, model_name)
    from a LangChain AIMessage or an OpenRouter Decisions response dictionary.
    """
    in_tokens = 0
    out_tokens = 0
    cache_tokens = 0
    total_tokens = 0
    model_name = default_model

    try:
        usage = None
        if isinstance(response, dict):
            usage = response.get("usage", {})
            model_name = response.get("model", default_model)
        elif response is not None:
            usage = getattr(response, "usage_metadata", None)
            if not usage:
                usage = getattr(response, "response_metadata", {}).get("token_usage", {})
            model_name = getattr(response, "response_metadata", {}).get("model_name", default_model)

        if isinstance(model_name, str):
            half = len(model_name) // 2
            if half > 0 and model_name[:half] == model_name[half:]:
                model_name = model_name[:half]

        if usage:
            in_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
            out_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
            
            # Check prompt_tokens_details for cached tokens
            details = usage.get("prompt_tokens_details", {})
            if isinstance(details, dict):
                cache_tokens = details.get("cached_tokens", 0) or 0
            else:
                cache_tokens = usage.get("cached_tokens", 0) or 0

            total_tokens = usage.get("total_tokens", 0) or (in_tokens + out_tokens)
    except Exception as e:
        print(f"[TELEMETRY] Error parsing tokens: {e}")

    return in_tokens, out_tokens, cache_tokens, total_tokens, model_name

def _print_node_log(
    node: str,
    status: str,
    model: Optional[str],
    latency_ms: int,
    in_token: int,
    out_token: int,
    cache_token: int,
    total_tokens: int,
    extra_summary: Optional[str] = None,
):
    print(f"\n[{node.upper()}]")
    print(f"  Status:   {status}")
    if model:
        print(f"  Model:    {model}")
    print(f"  Time:     {latency_ms / 1000:.2f}s")

    if total_tokens > 0:
        token_line = f"  Tokens:   {total_tokens:,} (In: {in_token:,}, Out: {out_token:,}"
        if cache_token > 0:
            token_line += f", Cache: {cache_token:,}"
        token_line += ")"
        print(token_line)

    if node == "tool_executor":
        queries = get_executed_queries()
        if queries:
            for q in queries:
                print(f"  Table:    {q.get('table', '-')}")
                print(f"  Records:  {q.get('rowCount', 0)} returned (Total matching: {q.get('totalCount', 0)})")
                print(f"  SQL:")
                print(f"    {q.get('sql', '').strip()}")
        elif extra_summary:
            print(f"  Details:  {extra_summary}")
    elif extra_summary:
        print(f"  Details:  {extra_summary}")

def record_node_telemetry(
    node: str,
    latency_ms: int,
    response: Any = None,
    in_token: Optional[int] = None,
    out_token: Optional[int] = None,
    cache_token: Optional[int] = None,
    total_tokens: Optional[int] = None,
    model: Optional[str] = None,
    status: str = "SUCCESS",
    input_data: Optional[Dict[str, Any]] = None,
    output_data: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    extra_summary: Optional[str] = None,
):
    """
    Records telemetry for an individual node into the request's context-scoped tracker,
    updates global token usage, and prints a clean standardized console log.
    """
    # 1. If response is passed, parse tokens and model automatically
    if response is not None:
        parsed_in, parsed_out, parsed_cache, parsed_tot, parsed_model = parse_tokens(response, default_model=model or "unknown")
        in_token = parsed_in if in_token is None else in_token
        out_token = parsed_out if out_token is None else out_token
        cache_token = parsed_cache if cache_token is None else cache_token
        total_tokens = parsed_tot if total_tokens is None else total_tokens
        model = parsed_model if model is None else model

    in_val = int(in_token or 0)
    out_val = int(out_token or 0)
    cache_val = int(cache_token or 0)
    total_val = int(total_tokens or (in_val + out_val))

    # 2. Update global token usage for the request
    try:
        from app.agent.state import global_token_usage
        usage_list = global_token_usage.get()
        usage_list[0] += total_val
        if len(usage_list) >= 3:
            usage_list[1] += in_val
            usage_list[2] += out_val
    except Exception:
        pass

    # 3. Save into request-scoped tracker
    tracker = node_telemetry_tracker.get()
    tracker[node] = {
        "model": model,
        "latencyMs": int(latency_ms),
        "inToken": in_val,
        "outToken": out_val,
        "cacheToken": cache_val,
        "totalTokens": total_val,
        "status": status,
        "input": input_data or {},
        "output": output_data or {},
        "errorMessage": error_message,
    }

    # 4. Standardized clean console logging
    _print_node_log(
        node=node,
        status=status,
        model=model,
        latency_ms=latency_ms,
        in_token=in_val,
        out_token=out_val,
        cache_token=cache_val,
        total_tokens=total_val,
        extra_summary=extra_summary,
    )

async def _send_telemetry_http(payload: Dict[str, Any]):
    backend_url = os.getenv("BACKEND_URL")
    if not backend_url:
        print("  Audit Log:  BACKEND_URL not set in environment (.env). Audit log dispatch skipped.")
        print(f"{'-'*80}\n")
        return

    endpoint = f"{backend_url.rstrip('/')}/chat/internal/audit-telemetry"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(endpoint, json=payload)
            if resp.status_code in (200, 202):
                print(f"  Audit Log:  Enqueued to Service Bus!")
                print(f"{'-'*80}\n")
            else:
                print(f"  Audit Log:  Backend returned status {resp.status_code}: {resp.text}")
                print(f"{'='*80}\n")
    except Exception as e:
        print(f"  Audit Log:  Failed to post audit telemetry to backend ({endpoint}): {e}")
        print(f"{'='*80}\n")

def dispatch_audit_telemetry(payload: Dict[str, Any]):
    """
    Dispatches the audit telemetry payload in a background non-blocking task.
    Ensures ZERO delay on the SSE streaming response to the client.
    """
    try:
        asyncio.create_task(_send_telemetry_http(payload))
    except Exception as e:
        print(f"[TELEMETRY] Warning: Could not schedule background telemetry dispatch: {e}")
