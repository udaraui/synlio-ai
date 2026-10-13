from typing import Optional
from fastapi import APIRouter, Body
from fastapi.responses import StreamingResponse
from app.agent.service import insight_agent
import traceback
import asyncio

router = APIRouter()

@router.post("/chat")
async def chat(message: str = Body(...), conversation_id: Optional[str] = Body(None)):
    if not insight_agent:
        async def unavailable_stream():
            yield "__FINAL__Synlio AI is temporarily unavailable. Please try again in a few moments."
        return StreamingResponse(unavailable_stream(), media_type="text/event-stream")
        
    async def event_stream():
        try:
            async for chunk in insight_agent.astream_run(message, conversation_id=conversation_id):
                yield chunk
        except Exception as e:
            print(f"--- INTERNAL SERVER ERROR ---")
            traceback.print_exc()
            yield f"__REPLACE__Error occurred"
            yield f"\n\nI encountered an unexpected issue while trying to process your request. Please try rephrasing or trying again later."

    return StreamingResponse(event_stream(), media_type="text/event-stream")
