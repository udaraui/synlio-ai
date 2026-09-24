from pydantic import BaseModel

class ChatRequest(BaseModel):
    message: str

class ChatResponse(BaseModel):
    response: str

class QueryRequest(BaseModel):
    query: str

class QueryResponse(BaseModel):
    response: str

