from fastapi import APIRouter
from app.api.models import ChatRequest, ChatResponse
from app.agent.graph import app as graph_app

router = APIRouter()

@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    # Run the langgraph agent with the input message
    result = graph_app.invoke({"messages": [request.message]})
    return ChatResponse(response=result["response"])
