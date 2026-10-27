from typing import Optional
from fastapi import APIRouter, Body
from fastapi.responses import StreamingResponse
from app.agent.service import insight_agent
import traceback
import asyncio

router = APIRouter()

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
    pending_draft: Optional[dict] = Body(None),
    pendingDraft: Optional[dict] = Body(None),
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
        print(f"  Context:         {context}")
    if active_pending_draft:
        print(f"  Pending Draft:   {active_pending_draft.get('name', 'Untitled')}")
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
            ):
                yield chunk
        except Exception as e:
            print(f"--- INTERNAL SERVER ERROR ---")
            traceback.print_exc()
            yield f"__REPLACE__Sorry! I encountered an unexpected issue while processing your request!"
            yield f"\nPlease try rephrasing or trying again."

    return StreamingResponse(event_stream(), media_type="text/event-stream")
