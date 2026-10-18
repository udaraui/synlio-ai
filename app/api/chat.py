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
    user_id: Optional[int] = Body(None),
    user_email: Optional[str] = Body(None),
    company_id: Optional[int] = Body(None),
    agent: Optional[str] = Body(None),
    context: Optional[dict] = Body(None),
    scope: Optional[dict] = Body(None),
):
    print("\n[INCOMING REQUEST CONTEXT]============================================")
    print(f"Message:           {message}")
    print(f"Conversation ID:   {conversation_id}")
    print(f"User ID:           {user_id}")
    print(f"User Email:        {user_email}")
    print(f"Company ID:        {company_id}")
    print(f"Agent (Preset):    {agent}")
    print(f"Page Context:      {context}")
    if scope:
        print(f"Ticket Space IDs:  {scope.get('allowed_ticket_space_ids', [])}")
        print(f"Task Space IDs:    {scope.get('allowed_task_space_ids', [])}")
        print(f"Root Task Limits:  {scope.get('root_task_restrictions', {})}")
    else:
        print("Scope:             None")
    print("======================================================================\n")

    if not insight_agent:
        async def unavailable_stream():
            yield "__FINAL__Synlio Co-Pilot is temporarily unavailable. Please try again in a few moments."
        return StreamingResponse(unavailable_stream(), media_type="text/event-stream")
        
    async def event_stream():
        try:
            async for chunk in insight_agent.astream_run(
                message,
                conversation_id=conversation_id,
                user_id=user_id,
                user_email=user_email,
                company_id=company_id,
                agent=agent,
                context=context,
                scope=scope,
            ):
                yield chunk
        except Exception as e:
            print(f"--- INTERNAL SERVER ERROR ---")
            traceback.print_exc()
            yield f"__REPLACE__Error occurred"
            yield f"\n\nI encountered an unexpected issue while trying to process your request. Please try rephrasing or trying again later."

    return StreamingResponse(event_stream(), media_type="text/event-stream")
