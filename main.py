from fastapi import FastAPI
import uvicorn

app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "hello synlio ai"}

if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=8000)
