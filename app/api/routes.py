from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from app.api.models import ChatRequest, ChatResponse
from app.agent.agent import insight_agent
import traceback
import asyncio

router = APIRouter()

@router.post("/chat")
async def chat(request: ChatRequest):
    if not insight_agent:
        raise HTTPException(status_code=500, detail="LLM API Key is not set or agent failed to initialize.")
        
    async def event_stream():
        try:
            async for chunk in insight_agent.astream_run(request.message):
                yield chunk
        except Exception as e:
            print(f"--- INTERNAL SERVER ERROR ---")
            traceback.print_exc()
            yield f"__REPLACE__Error occurred"
            yield f"\n\nI encountered an unexpected issue while trying to process your request. Please try rephrasing or trying again later."

    return StreamingResponse(event_stream(), media_type="text/event-stream")
