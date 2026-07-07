"""
app/main.py
============
EnergyGuard FastAPI Backend
----------------------------
Starts the application, registers routers, configures CORS,
and exposes a health-check endpoint.

Run with:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 3000

The --host 0.0.0.0 flag is essential: it makes the server
reachable from the ESP32 on the same local network.
Without it, the server only accepts connections from localhost
and the ESP32 will get connection refused errors.

Auto-generated API docs:
    http://localhost:3000/docs   ← Swagger UI (interactive)
    http://localhost:3000/redoc  ← ReDoc (readable)
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

from app.routers import data, commands

# ── App instance ──────────────────────────────────────────────
app = FastAPI(
    title="EnergyGuard API",
    description=(
        "IoT Energy Management System backend.\n\n"
        "- **ESP32** posts sensor data to `/api/data` every 2s\n"
        "- **Dashboard** connects to `/api/data/ws` for live WebSocket feed\n"
        "- **Dashboard** posts commands to `/api/commands`\n"
        "- **ESP32** polls `/api/commands` every 1s to collect pending commands\n"
    ),
    version="1.0.0",
    contact={
        "name": "EnergyGuard Project",
    }
)

# ── CORS ──────────────────────────────────────────────────────
# Allow the dashboard (served from any origin during dev) to
# call the API. In production, restrict origins to your domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────
app.include_router(data.router)
app.include_router(commands.router)

# ── Serve static dashboard files ──────────────────────────────
# If a 'static' folder exists next to this file, serve it.
# Place energy_dashboard.html in static/ and it will be
# accessible at http://localhost:3000/
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def serve_dashboard():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ── Health check ──────────────────────────────────────────────
@app.get("/health", tags=["System"], summary="Health check")
async def health():
    """
    Quick liveness check. Returns 200 if the server is running.
    The ESP32 can ping this on boot to verify connectivity before
    starting to transmit sensor data.
    """
    from app.store import store
    return {
        "status": "ok",
        "has_data": not store.is_empty,
        "pending_commands": len(store.command_queue),
        "history_size": len(store.history),
    }
