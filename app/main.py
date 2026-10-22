import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from app.api.chat import router as chat_router
from app.agent.service import insight_agent

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Suppress verbose uvicorn access logs for clean SSE streaming output in the terminal
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    yield

app = FastAPI(
    title="Synlio Co-Pilot API",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(chat_router)

@app.get("/", response_class=PlainTextResponse)
def read_root():
    return "Synlio Co-Pilot service is running."

@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "agent_loaded": insight_agent is not None,
    }