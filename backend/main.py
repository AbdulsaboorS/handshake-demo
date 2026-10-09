"""Serves the demo UI, runs one call at a time, and streams every event to the browser.

Every event is timestamped and kept for the life of the call. That history is the audit log, and it
is replayed to anyone who connects late (like the owner's phone).
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.call import Call

UI = Path(__file__).parent.parent / "ui"

app = FastAPI()
history: list[dict] = []
subscribers: set[asyncio.Queue] = set()
current: Call | None = None
task: asyncio.Task | None = None


def emit(kind: str, **data) -> None:
    event = {"kind": kind, "t": time.time(), **data}
    history.append(event)
    for q in subscribers:
        q.put_nowait(event)
    if kind == "owner_notified" and os.environ.get("NTFY_TOPIC"):
        asyncio.create_task(_push(data["intent"]))


async def _push(intent: str) -> None:
    # Free push to the owner's phone via ntfy.sh. Tapping it opens the approval page.
    async with httpx.AsyncClient() as client:
        await client.post(f"https://ntfy.sh/{os.environ['NTFY_TOPIC']}", content=intent, headers={
            "Title": "Pocket needs your approval",
            "Click": f"{os.environ.get('PUBLIC_URL', '')}/approve",
            "Priority": "urgent",
            "Tags": "airplane",
        })


class StartCall(BaseModel):
    scenario: Literal["happy", "unreachable", "overreach"]
    voice: bool = False


class Decision(BaseModel):
    approve: bool


class Answer(BaseModel):
    session: str
    sdp: str


@app.get("/")
async def index():
    return FileResponse(UI / "index.html")


@app.get("/approve")
async def approve_page():
    return FileResponse(UI / "approve.html")


@app.post("/calls")
async def start_call(body: StartCall):
    global current, task
    if task and not task.done():
        task.cancel()
    history.clear()
    current = Call(emit, overreach=body.scenario == "overreach", voice=body.voice)
    emit("call_started", scenario=body.scenario, voice=body.voice)

    async def run(call: Call):
        try:
            await call.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            emit("error", message=f"{type(e).__name__}: {e}")
        emit("call_ended")

    task = asyncio.create_task(run(current))
    return {"ok": True}


@app.post("/approvals/{approval_id}")
async def decide(approval_id: str, body: Decision):
    if current is None or approval_id not in current.provider.approvals:
        raise HTTPException(404, "no such approval")
    return {"status": current.provider.resolve(approval_id, body.approve).status}


@app.post("/rtc/listen")
async def rtc_listen():
    """The browser joins the call as a listener. The server talks to Cloudflare so the app token stays here."""
    from backend.voice import listen_in

    if current is None or not current.voice:
        raise HTTPException(409, "no voice call running")
    session, offer = await listen_in(current.line.api, current.line.tracks())
    return {"session": session, "offer": offer}


@app.post("/rtc/answer")
async def rtc_answer(body: Answer):
    from aiortc import RTCSessionDescription

    if current is None or not current.voice:
        raise HTTPException(409, "no voice call running")
    await current.line.api.renegotiate(body.session, RTCSessionDescription(sdp=body.sdp, type="answer"))
    current.listener_ready.set()
    return {"ok": True}


@app.websocket("/events")
async def events(ws: WebSocket):
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue()
    for event in history:
        q.put_nowait(event)
    subscribers.add(q)
    try:
        while True:
            await ws.send_json(await q.get())
    except WebSocketDisconnect:
        pass
    finally:
        subscribers.discard(q)
