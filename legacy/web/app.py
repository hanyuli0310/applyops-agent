"""ApplyOps Agent FastAPI web server + WebSocket handler."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from applyops.agent import (
    ApplyAgent,
    AskEvent,
    DoneEvent,
    ErrorEvent,
    LearnEvent,
    ScreenshotEvent,
    StatusEvent,
    WaitEvent,
)
from applyops.browser import BrowserController
from applyops.llm.base import BaseLLM
from applyops.llm.claude import ClaudeLLM
from applyops.llm.gemini_llm import GeminiLLM
from applyops.llm.openai_llm import OpenAILLM
from applyops.memory import MemoryStore

# ── Paths ────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent.parent.parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = Path(__file__).parent / "static"
SETTINGS_FILE = DATA_DIR / "settings.json"
MEMORY_FILE = DATA_DIR / "memory.json"

# ── Schemas ──────────────────────────────────────────────────────────


class Settings(BaseModel):
    llm_provider: str = "claude"  # claude, openai, gemini
    llm_model: str = ""
    headless: bool = False
    max_actions_per_job: int = 50


class QAModel(BaseModel):
    question: Optional[str] = None
    answer: str
    context: Optional[str] = None


class ProfileUpdate(BaseModel):
    profile: Dict[str, Any]


# ── App & State ──────────────────────────────────────────────────────

app = FastAPI(title="ApplyOps Agent API", version="0.1.0")
memory_store: Optional[MemoryStore] = None
browser_controller: Optional[BrowserController] = None
active_connections: List[WebSocket] = []
agent: Optional[ApplyAgent] = None
agent_state: str = "idle"


def load_settings() -> Settings:
    settings = Settings()
    if SETTINGS_FILE.exists():
        try:
            settings = Settings.model_validate_json(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    if os.getenv("APPLYOPS_HEADLESS") == "1":
        settings.headless = True

    return settings


def save_settings(settings: Settings):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(settings.model_dump_json(indent=2), encoding="utf-8")


settings = load_settings()


def get_llm_provider() -> BaseLLM:
    provider = settings.llm_provider.lower()
    if provider == "openai" or (provider == "" and os.getenv("OPENAI_API_KEY")):
        return OpenAILLM(model=settings.llm_model)
    elif provider == "gemini" or (provider == "" and (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))):
        return GeminiLLM(model=settings.llm_model)
    # Default to Claude
    return ClaudeLLM(model=settings.llm_model)


@app.on_event("startup")
async def startup_event():
    global memory_store, browser_controller
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    memory_store = MemoryStore(MEMORY_FILE)
    browser_controller = BrowserController(headless=settings.headless)


@app.on_event("shutdown")
async def shutdown_event():
    global browser_controller
    if browser_controller:
        try:
            await browser_controller.close()
        except Exception:
            pass


async def broadcast_ws(message: dict):
    disconnected = []
    for connection in active_connections:
        try:
            await connection.send_json(message)
        except Exception:
            disconnected.append(connection)

    for conn in disconnected:
        if conn in active_connections:
            active_connections.remove(conn)


async def set_agent_state(state: str):
    global agent_state
    agent_state = state
    await broadcast_ws({"type": "agent_state", "state": state})


def agent_event_callback(event: Any):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    if isinstance(event, StatusEvent):
        loop.create_task(broadcast_ws({"type": "status", "message": event.message}))
    elif isinstance(event, ScreenshotEvent):
        loop.create_task(broadcast_ws({"type": "screenshot", "data": event.base64_data}))
    elif isinstance(event, AskEvent):
        loop.create_task(
            broadcast_ws(
                {
                    "type": "ask",
                    "id": str(event.id),
                    "question": event.question,
                    "context": event.context,
                    "suggestion": event.suggestion,
                    "suggestion_confidence": event.suggestion_confidence,
                }
            )
        )
        loop.create_task(set_agent_state("waiting_for_answer"))
    elif isinstance(event, WaitEvent):
        # Previously this path never fired, so the UI had nothing to transition
        # into and the "Continue" button was dead.
        loop.create_task(broadcast_ws({"type": "wait", "reason": event.reason}))
        loop.create_task(set_agent_state("waiting_for_user"))
    elif isinstance(event, LearnEvent):
        loop.create_task(
            broadcast_ws(
                {
                    "type": "learn",
                    "question": event.question,
                    "answer": event.answer,
                    "confidence": event.confidence,
                    "auto": event.auto,
                }
            )
        )
    elif isinstance(event, DoneEvent):
        loop.create_task(
            broadcast_ws(
                {
                    "type": "done",
                    "status": event.status,
                    "summary": event.summary,
                }
            )
        )
        loop.create_task(set_agent_state("idle"))
    elif isinstance(event, ErrorEvent):
        loop.create_task(broadcast_ws({"type": "error", "message": event.message}))
        loop.create_task(set_agent_state("idle"))


# ── REST Endpoints ───────────────────────────────────────────────────


@app.get("/api/memory/profile")
async def get_profile():
    if not memory_store:
        raise HTTPException(status_code=500, detail="Memory not initialized")
    return {"profile": memory_store.get_profile()}


@app.put("/api/memory/profile")
async def update_profile(data: ProfileUpdate):
    if not memory_store:
        raise HTTPException(status_code=500, detail="Memory not initialized")
    memory_store.update_profile(data.profile)
    return {"status": "success"}


@app.get("/api/memory/qa")
async def get_qa():
    if not memory_store:
        raise HTTPException(status_code=500, detail="Memory not initialized")
    return {"qa": memory_store.get_all_qa()}


@app.delete("/api/memory/qa/{qa_id}")
async def delete_qa(qa_id: str):
    if not memory_store:
        raise HTTPException(status_code=500, detail="Memory not initialized")
    memory_store.delete_qa(qa_id)
    return {"status": "success"}


@app.put("/api/memory/qa/{qa_id}")
async def update_qa(qa_id: str, data: QAModel):
    if not memory_store:
        raise HTTPException(status_code=500, detail="Memory not initialized")
    memory_store.update_qa(qa_id, question=data.question, answer=data.answer, context=data.context)
    return {"status": "success"}


class FeedbackRequest(BaseModel):
    """Human telling us whether a memory was actually correct."""

    success: bool


@app.get("/api/memory/stats")
async def get_stats():
    """Flywheel health: how much is automated, how much still needs a human."""
    if not memory_store:
        raise HTTPException(status_code=500, detail="Memory not initialized")
    return memory_store.get_stats()


@app.post("/api/memory/qa/{qa_id}/feedback")
async def qa_feedback(qa_id: str, body: FeedbackRequest):
    """Manually correct a memory — pushes its confidence up or down."""
    if not memory_store:
        raise HTTPException(status_code=500, detail="Memory not initialized")
    memory_store.record_answer_outcome(qa_id, body.success)
    return {"status": "success"}


@app.get("/api/memory/platforms")
async def get_platform_knowledge():
    """Learned + seeded selector knowledge per platform."""
    if not memory_store:
        raise HTTPException(status_code=500, detail="Memory not initialized")
    return {name: memory_store.get_selector_hints(name) for name in memory_store.list_platforms()}


@app.get("/api/history")
async def get_history():
    if not memory_store:
        raise HTTPException(status_code=500, detail="Memory not initialized")
    return {"history": memory_store.get_history()}


@app.get("/api/status")
async def get_status():
    return {"state": agent_state}


@app.get("/api/settings")
async def get_settings():
    return load_settings().model_dump()


@app.post("/api/settings")
async def update_settings(new_settings: Settings):
    global settings, browser_controller
    save_settings(new_settings)
    settings = new_settings
    if browser_controller and browser_controller.headless != settings.headless:
        try:
            await browser_controller.close()
        except Exception:
            pass
        browser_controller = BrowserController(headless=settings.headless)
    return {"status": "success"}


# ── WebSocket Endpoint ───────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    global agent

    await websocket.send_json({"type": "agent_state", "state": agent_state})

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type in ("apply", "start_apply"):
                url = data.get("url")
                if not url:
                    await websocket.send_json({"type": "error", "message": "Missing URL"})
                    continue

                if agent_state == "idle":
                    await set_agent_state("running")
                    llm = get_llm_provider()
                    agent = ApplyAgent(
                        llm=llm,
                        browser=browser_controller,
                        memory=memory_store,
                        callback=agent_event_callback,
                        max_actions=settings.max_actions_per_job,
                    )
                    asyncio.create_task(agent.apply(url))

            elif msg_type == "answer":
                ans_id = data.get("id") or data.get("question_id", "")
                answer = data.get("answer", "")
                if agent and agent_state == "waiting_for_answer":
                    await agent.provide_answer(ans_id, answer)
                    await set_agent_state("running")

            elif msg_type == "resume":
                if agent:
                    await agent.resume()
                    await set_agent_state("running")

            elif msg_type == "stop":
                if agent:
                    await agent.stop()
                    await set_agent_state("idle")

    except WebSocketDisconnect:
        if websocket in active_connections:
            active_connections.remove(websocket)
    except Exception:
        if websocket in active_connections:
            active_connections.remove(websocket)


# ── Static Files ─────────────────────────────────────────────────────

if not STATIC_DIR.exists():
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")
