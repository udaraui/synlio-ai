from typing import Optional, Union, Any, List, Dict
from fastapi import APIRouter, Body
from fastapi.responses import StreamingResponse
from app.agent.service import insight_agent
import traceback
import asyncio

router = APIRouter()

# Registry of active streaming tasks keyed by conversation_id
active_chat_tasks: Dict[str, asyncio.Task] = {}

@router.post("/chat/stop")
async def stop_chat(
    conversation_id: Optional[str] = Body(None, embed=True),
    payload: Optional[Dict[str, Any]] = Body(None),
):
    conv_id = conversation_id or (payload.get("conversation_id") if payload else None) or (payload.get("conversationId") if payload else None)
    if conv_id and conv_id in active_chat_tasks:
        task = active_chat_tasks.get(conv_id)
        if task and not task.done():
            task.cancel()
            print(f"[STOP] Cancelled active AI graph task for conversation: {conv_id}")
            return {"status": "cancelled", "conversation_id": conv_id}
    return {"status": "not_found_or_already_done", "conversation_id": conv_id}

def format_context_summary(ctx: Optional[dict]) -> str:
    """Produces a clean 1-line summary of UI context to prevent console flooding."""
    if not ctx or not isinstance(ctx, dict):
        return "None"
    space_name = ctx.get("taskSpaceName") or ctx.get("ticketSpaceName") or ctx.get("spaceName") or "-"
    space_id = ctx.get("taskSpaceId") or ctx.get("ticketSpaceId") or ctx.get("spaceId") or "-"
    prefix = ctx.get("taskSpacePrefix") or ctx.get("ticketSpacePrefix") or ctx.get("spacePrefix") or ctx.get("prefix") or ""
    page = ctx.get("page") or ctx.get("pageScopeKey") or "-"
    tasks = ctx.get("tasks") or ctx.get("taskList") or []
    tickets = ctx.get("tickets") or ctx.get("ticketList") or []

    parts = [f"Space: '{space_name}' (ID: {space_id}, Prefix: {prefix})"]
    if isinstance(tasks, list) and tasks:
        parts.append(f"{len(tasks)} tasks in view")
    if isinstance(tickets, list) and tickets:
        parts.append(f"{len(tickets)} tickets in view")
    if page != "-":
        parts.append(f"Page: {page}")
    return " | ".join(parts)


@router.post("/chat")
async def chat(
    message: str = Body(...),
    conversation_id: Optional[str] = Body(None),
    request_id: Optional[str] = Body(None),
    chat_mode: Optional[str] = Body("panel"),
    user_id: Optional[int] = Body(None),
    user_email: Optional[str] = Body(None),
    user_name: Optional[str] = Body(None),
    company_id: Optional[int] = Body(None),
    agent: Optional[str] = Body(None),
    context: Optional[dict] = Body(None),
    scope: Optional[dict] = Body(None),
    history: Optional[list] = Body(None),
    pending_draft: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = Body(None),
    pendingDraft: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = Body(None),
    attachments: Optional[List[Dict[str, Any]]] = Body(None),
):
    active_pending_draft = pending_draft or pendingDraft
    print(f"\n{'='*80}")
    print("[INCOMING CHAT]")
    print(f"  Query:           \"{message}\"")
    print(f"  User:            {user_name or 'Anonymous'} ({user_email or 'no-email'}, ID: {user_id or '-'})")
    print(f"  Company:         {company_id} | Mode: {chat_mode}")
    if conversation_id:
        print(f"  Conversation:    {conversation_id}")
    if agent:
        print(f"  Preset Agent:    {agent}")
    if context:
        print(f"  Context Summary: {format_context_summary(context)}")
    if attachments:
        att_summaries = []
        for a in attachments:
            if isinstance(a, dict):
                fname = a.get('filename') or a.get('name') or 'Document'
                size_hint = f"{len(a.get('content') or a.get('contentText') or '')} chars"
                att_summaries.append(f"{fname} ({size_hint})")
        print(f"  Attachments ({len(attachments)}): {', '.join(att_summaries)}")
    if active_pending_draft:
        if isinstance(active_pending_draft, list):
            names = [str(d.get('name') or 'Untitled') for d in active_pending_draft if isinstance(d, dict)]
            print(f"  Pending Drafts ({len(active_pending_draft)}): {', '.join(names)}")
        elif isinstance(active_pending_draft, dict):
            print(f"  Pending Draft:   {active_pending_draft.get('name') or 'Untitled'}")
    if history:
        print(f"  Client History:  {len(history)} messages")
    if scope:
        ticket_spaces = scope.get('allowed_ticket_space_ids', [])
        task_spaces = scope.get('allowed_task_space_ids', [])
        print(f"  Permission Scope:")
        print(f"    Ticket Spaces: {ticket_spaces}")
        print(f"    Task Spaces:   {task_spaces}")

    if not insight_agent:
        async def unavailable_stream():
            yield "__FINAL__Synlio Co-Pilot is temporarily unavailable. Please try again in a few moments."
        return StreamingResponse(unavailable_stream(), media_type="text/event-stream")
        
    async def event_stream():
        current_task = asyncio.current_task()
        if conversation_id and current_task:
            active_chat_tasks[conversation_id] = current_task
        try:
            async for chunk in insight_agent.astream_run(
                message,
                conversation_id=conversation_id,
                request_id=request_id,
                chat_mode=chat_mode,
                user_id=user_id,
                user_email=user_email,
                user_name=user_name,
                company_id=company_id,
                agent=agent,
                context=context,
                scope=scope,
                history=history,
                pending_draft=active_pending_draft,
                attachments=attachments,
            ):
                yield chunk
        except asyncio.CancelledError:
            print(f"[CHAT STOPPED] Stream for conversation {conversation_id} was successfully cancelled.")
            yield "\n\n[Generation stopped]"
            raise
        except Exception as e:
            print(f"--- INTERNAL SERVER ERROR ---")
            traceback.print_exc()
            yield f"__REPLACE__Sorry! I encountered an unexpected issue while processing your request!"
            yield f"\nPlease try rephrasing or trying again."
        finally:
            if conversation_id and conversation_id in active_chat_tasks:
                active_chat_tasks.pop(conversation_id, None)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
