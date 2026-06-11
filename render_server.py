from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path
import os

app = FastAPI()

@app.get("/")
def index():
    content = Path("phone.html").read_text(encoding="utf-8")
    return HTMLResponse(content, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/version")
def version():
    p = Path("phone.html")
    content = p.read_text(encoding="utf-8")
    return JSONResponse({"size_bytes": len(content.encode()), "has_cpPad": "cpPad" in content, "has_줄선택": "줄선택" in content})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
