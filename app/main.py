import logging
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from app.api.chat import router as chat_router
from app.agent.service import insight_agent

# Disable Uvicorn's raw HTTP access log ("POST /chat HTTP/1.1 200 OK") to keep console clean
logging.getLogger("uvicorn.access").disabled = True
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

app = FastAPI(title="Synlio Co-Pilot API")

@app.on_event("startup")
async def startup_event():
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

app.include_router(chat_router)

@app.get("/", response_class=PlainTextResponse)
def read_root():
    return "Synlio Co-Pilot service is running."

@app.get("/health")
def health_check():
    return {"status": "ok", "agent_loaded": insight_agent is not None}