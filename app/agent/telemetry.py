import os
import contextvars
import asyncio
from typing import Optional, Dict, Any, List, Tuple
import httpx

# =============================================================================
# 1. REQUEST-SCOPED CONTEXT VARIABLES
# Defaulting to None prevents mutable collection sharing across async tasks.
# =============================================================================

node_telemetry_tracker: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "node_telemetry_tracker", default=None
)
last_executed_queries: contextvars.ContextVar[Optional[List[Dict[str, Any]]]] = contextvars.ContextVar(
    "last_executed_queries", default=None
)
global_token_usage: contextvars.ContextVar[Optional[List[int]]] = contextvars.ContextVar(
    "global_token_usage", default=None
)


def init_telemetry():
    """Initializes/resets all request-scoped telemetry and token counters."""
    node_telemetry_tracker.set({})
    last_executed_queries.set([])
    global_token_usage.set([0, 0, 0])


def get_telemetry_tracker() -> Dict[str, Any]:
    """Returns the current request's node telemetry dictionary, auto-initializing if needed."""
    tracker = node_telemetry_tracker.get()
    if tracker is None:
        tracker = {}
        node_telemetry_tracker.set(tracker)
    return tracker


def get_token_usage() -> List[int]:
    """Returns [total_tokens, input_tokens, output_tokens] for the current request."""
    usage = global_token_usage.get()
    if usage is None:
        usage = [0, 0, 0]
        global_token_usage.set(usage)
    return usage


def record_executed_query(table: str, sql: str, row_count: int, total_count: int):
    """Records executed database queries into the request's telemetry tracker."""
    entry = {
        "table": table,
        "sql": sql,
        "rowCount": row_count,
        "totalCount": total_count,
    }

    try:
        tracker = get_telemetry_tracker()
        if "_executed_queries" not in tracker:
            tracker["_executed_queries"] = []
        tracker["_executed_queries"].append(entry)
    except Exception as e:
        print(f"[TELEMETRY] Error recording query in tracker: {e}")

    try:
        queries = last_executed_queries.get()
        if queries is None:
            queries = []
        else:
            queries = list(queries)
        queries.append(entry)
        last_executed_queries.set(queries)
    except Exception as e:
        print(f"[TELEMETRY] Error recording executed query: {e}")


def get_executed_queries() -> List[Dict[str, Any]]:
    """Retrieves all database queries executed during the current request."""
    try:
        tracker = node_telemetry_tracker.get()
        if tracker and "_executed_queries" in tracker:
            return tracker["_executed_queries"]
    except Exception:
        pass
    return last_executed_queries.get() or []


# =============================================================================
# 2. TOKEN USAGE PARSING
# =============================================================================

def parse_tokens(response: Any, default_model: str = "unknown") -> Tuple[int, int, int, int, str]:
    """
    Extracts (in_tokens, out_tokens, cache_tokens, total_tokens, model_name)
    from a LangChain AIMessage or an OpenRouter response dictionary.
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

        # De-duplicate model string if repeated by proxy (e.g. "anthropic/claudeanthropic/claude")
        if isinstance(model_name, str):
            half = len(model_name) // 2
            if half > 0 and model_name[:half] == model_name[half:]:
                model_name = model_name[:half]

        if usage:
            in_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
            out_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0

            # OpenRouter cache read details
            details = usage.get("prompt_tokens_details", {})
            if isinstance(details, dict):
                cache_tokens = details.get("cached_tokens", 0) or 0
            else:
                cache_tokens = usage.get("cached_tokens", 0) or 0

            total_tokens = usage.get("total_tokens", 0) or (in_tokens + out_tokens)
    except Exception as e:
        print(f"[TELEMETRY] Error parsing tokens: {e}")

    return in_tokens, out_tokens, cache_tokens, total_tokens, model_name


# =============================================================================
# 3. CONSOLE LOGGING
# =============================================================================

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


# =============================================================================
# 4. NODE TELEMETRY RECORDING
# =============================================================================

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
    updates request token usage, and prints a standardized console log banner.
    """
    # 1. Parse tokens and model from response if provided
    if response is not None:
        parsed_in, parsed_out, parsed_cache, parsed_tot, parsed_model = parse_tokens(
            response, default_model=model or "unknown"
        )
        in_token = parsed_in if in_token is None else in_token
        out_token = parsed_out if out_token is None else out_token
        cache_token = parsed_cache if cache_token is None else cache_token
        total_tokens = parsed_tot if total_tokens is None else total_tokens
        model = parsed_model if model is None else model

    in_val = int(in_token or 0)
    out_val = int(out_token or 0)
    cache_val = int(cache_token or 0)
    total_val = int(total_tokens or (in_val + out_val))

    # 2. Accumulate request token totals directly (no circular import)
    try:
        usage_list = get_token_usage()
        usage_list[0] += total_val
        if len(usage_list) >= 3:
            usage_list[1] += in_val
            usage_list[2] += out_val
    except Exception:
        pass

    # 3. Save node record into request-scoped tracker
    tracker = get_telemetry_tracker()
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

    # 4. Print standardized console log
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


# =============================================================================
# 5. ASYNC AUDIT DISPATCH
# =============================================================================

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
    Ensures zero delay on SSE streaming to the client.
    """
    try:
        asyncio.create_task(_send_telemetry_http(payload))
    except Exception as e:
        print(f"[TELEMETRY] Warning: Could not schedule background telemetry dispatch: {e}")
