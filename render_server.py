from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pathlib import Path
import os

app = FastAPI()

@app.get("/")
def index():
    return HTMLResponse(Path("phone.html").read_text(encoding="utf-8"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
