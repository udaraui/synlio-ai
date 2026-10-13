from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from app.api.chat import router as chat_router
from app.agent.service import insight_agent

app = FastAPI(title="Synlio AI API")

app.include_router(chat_router)

@app.get("/", response_class=PlainTextResponse)
def read_root():
    return "Synlio AI service is running."

@app.get("/health")
def health_check():
    return {"status": "ok", "agent_loaded": insight_agent is not None}