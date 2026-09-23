from fastapi import FastAPI
from app.api.routes import router as chat_router

app = FastAPI()

app.include_router(chat_router)

@app.get("/")
def read_root():
    return {"message": "hello synlio ai"}
