from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.gzip import GZipMiddleware
from pathlib import Path
import os, asyncio, json, logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("relay")

app = FastAPI()

# 단일 유저 릴레이: token 매칭 없음 — role="pc"면 PC슬롯, 나머지는 phone슬롯
_pc_ws: WebSocket | None = None
_phone_ws: WebSocket | None = None


@app.get("/")
def index():
    content = Path("phone.html").read_text(encoding="utf-8")
    return HTMLResponse(content, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/manifest.json")
def manifest():
    return JSONResponse({
        "name": "TapDesk", "short_name": "TapDesk",
        "start_url": "/", "display": "fullscreen", "orientation": "any",
        "background_color": "#05060a", "theme_color": "#05060a",
        "icons": [{"src": "/icon.png", "sizes": "256x256", "type": "image/png"}],
    })


@app.get("/icon.png")
def icon():
    p = Path("icon.png")
    if p.exists():
        return FileResponse(str(p), media_type="image/png")
    return JSONResponse({"error": "no icon"}, status_code=404)


@app.get("/health")
def health():
    return {"ok": True, "pc": _pc_ws is not None, "phone": _phone_ws is not None}


@app.websocket("/ws")
async def relay(ws: WebSocket):
    global _pc_ws, _phone_ws
    await ws.accept()

    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        auth = json.loads(raw)
    except Exception:
        await ws.close(code=4001)
        return

    role = auth.get("role", "phone")

    if role == "pc":
        _pc_ws = ws
        log.info("PC 연결됨")
    else:
        _phone_ws = ws
        log.info("폰 연결됨")

    try:
        await ws.send_text(json.dumps({"type": "relay_ok", "role": role}))
    except Exception:
        pass

    try:
        while True:
            msg = await ws.receive()
            # 상대방 가져오기
            peer: WebSocket | None = _phone_ws if role == "pc" else _pc_ws
            if peer is None:
                continue
            try:
                text = msg.get("text")
                data = msg.get("bytes")
                if text is not None:
                    await peer.send_text(text)
                elif data is not None:
                    await peer.send_bytes(data)
            except Exception:
                pass
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if role == "pc" and _pc_ws is ws:
            _pc_ws = None
            log.info("PC 해제")
        elif role != "pc" and _phone_ws is ws:
            _phone_ws = None
            log.info("폰 해제")


@app.on_event("startup")
async def _keepalive():
    async def _ping():
        import httpx
        url = os.environ.get("RENDER_EXTERNAL_URL", "")
        if not url:
            return
        await asyncio.sleep(30)
        while True:
            try:
                async with httpx.AsyncClient() as c:
                    await c.get(url + "/health", timeout=5)
            except Exception:
                pass
            await asyncio.sleep(600)   # 10분마다 ping → 절대 잠들지 않음
    asyncio.create_task(_ping())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
