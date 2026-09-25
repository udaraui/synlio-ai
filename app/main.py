from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from app.api.routes import router as chat_router
from app.agent.agent import insight_agent
from app.api.models import QueryRequest, QueryResponse

app = FastAPI(title="Cubix Analytics API")

app.include_router(chat_router)

@app.get("/", response_class=PlainTextResponse)
def read_root():
    return "hello synlio AI!"

@app.post("/analyze", response_model=QueryResponse)
async def analyze_data(request: QueryRequest):
    if not insight_agent:
        raise HTTPException(status_code=500, detail="Gemini API Key is not set or agent failed to initialize.")
        
    # Run the insight agent with the user's query
    result = await insight_agent.arun(request.query)
    return QueryResponse(response=result.data)

@app.get("/health")
def health_check():
    return {"status": "ok", "agent_loaded": insight_agent is not None}