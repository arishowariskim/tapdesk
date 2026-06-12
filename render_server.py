from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path
import os
import asyncio
import json
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("relay")

app = FastAPI()

# room_id(token) → {"pc": WebSocket | None, "phone": WebSocket | None}
rooms: dict[str, dict] = {}
rooms_lock = asyncio.Lock()


@app.get("/")
def index():
    content = Path("phone.html").read_text(encoding="utf-8")
    return HTMLResponse(content, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/health")
def health():
    return {"ok": True, "rooms": len(rooms)}


@app.websocket("/ws")
async def relay(ws: WebSocket):
    await ws.accept()
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        auth = json.loads(raw)
    except Exception:
        await ws.close(code=4001)
        return

    if auth.get("type") != "auth" or not auth.get("token"):
        await ws.close(code=4003)
        return

    token = auth["token"]
    role = auth.get("role", "phone")   # PC는 role="pc" 명시, 폰은 기본 "phone"

    async with rooms_lock:
        if token not in rooms:
            rooms[token] = {"pc": None, "phone": None}
        rooms[token][role] = ws

    peer_role = "phone" if role == "pc" else "pc"
    log.info(f"연결: role={role} token={token[:8]}…")

    # 연결 확인 응답
    try:
        await ws.send_text(json.dumps({"type": "relay_ok", "role": role}))
    except Exception:
        pass

    try:
        while True:
            # 텍스트·바이너리 둘 다 처리
            msg = await ws.receive()
            peer = rooms.get(token, {}).get(peer_role)
            if peer is None:
                continue
            try:
                if "text" in msg:
                    await peer.send_text(msg["text"])
                elif "bytes" in msg:
                    await peer.send_bytes(msg["bytes"])
            except Exception:
                pass
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        async with rooms_lock:
            if token in rooms:
                rooms[token][role] = None
                if rooms[token]["pc"] is None and rooms[token]["phone"] is None:
                    del rooms[token]
        log.info(f"해제: role={role} token={token[:8]}…")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
