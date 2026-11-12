"""FastAPI application: static frontend, health, rooms API, WebSocket route.

Run locally:
    uvicorn backend.main:app --reload --port 8000

On Render the start command is:
    uvicorn backend.main:app --host 0.0.0.0 --port $PORT --ws websockets
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.adapters.factory import build_stt_provider, build_translator
from backend.config import get_settings
from backend.rooms import RoomRegistry
from backend.ws import handle_websocket

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
INDEX_FILE = FRONTEND_DIR / "index.html"

log = logging.getLogger("thai_viet_webtalk")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    app.state.settings = settings
    app.state.registry = RoomRegistry(settings)
    app.state.provider = build_stt_provider(settings)
    app.state.translator = build_translator(settings)
    app.state.started_at = time.time()

    log.info(
        "startup: stt=%s translate=%s targets=%s max_clients=%d",
        app.state.provider.name,
        app.state.translator.name,
        ",".join(settings.targets),
        settings.max_room_clients,
    )

    gc_task = asyncio.create_task(app.state.registry.gc_loop())
    try:
        yield
    finally:
        gc_task.cancel()
        await asyncio.gather(gc_task, return_exceptions=True)
        await _quietly(app.state.provider.aclose())
        await _quietly(app.state.translator.aclose())
        log.info("shutdown complete")


async def _quietly(coro) -> None:
    try:
        await coro
    except Exception:
        log.exception("cleanup failed")


app = FastAPI(
    title="THAI_VIET_WEBTALK",
    version="0.1.0",
    description="Real-time Thai <-> Vietnamese speech translator for video calls.",
    lifespan=lifespan,
)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    registry: RoomRegistry = app.state.registry
    stats = registry.stats()
    payload = {
        "status": "ok",
        "stt": app.state.provider.name,
        "translator": app.state.translator.name,
        "rooms": stats["rooms"],
        "clients": stats["clients"],
        "uptime_s": int(time.time() - app.state.started_at),
    }
    translator_stats = getattr(app.state.translator, "stats", None)
    if callable(translator_stats):
        payload["translate_stats"] = translator_stats()
    return payload


@app.get("/api/rooms", include_in_schema=False)
async def list_rooms() -> dict:
    registry: RoomRegistry = app.state.registry
    return {"stats": registry.stats(), "requests": registry.summary()}


@app.get("/", include_in_schema=False)
@app.get("/room/{room_id}", include_in_schema=False)
async def index(room_id: str | None = None):
    if not INDEX_FILE.exists():
        return JSONResponse(
            {"error": "frontend missing", "expected": str(INDEX_FILE)},
            status_code=500,
        )
    return FileResponse(
        INDEX_FILE,
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.websocket("/ws/{room_id}/{client_id}")
async def websocket_route(websocket: WebSocket, room_id: str, client_id: str) -> None:
    await handle_websocket(websocket, room_id, client_id)


if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
else:  # pragma: no cover - only on a broken checkout
    log.warning("frontend directory not found at %s", FRONTEND_DIR)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        ws_max_size=1024 * 1024,
    )
