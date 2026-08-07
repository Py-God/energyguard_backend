"""
routers/data.py
================
Endpoints for sensor data flow.

POST /api/data
  Called by the ESP32 every TX_INTERVAL (2s).
  Stores the reading and broadcasts it to any connected
  WebSocket clients (dashboard).

GET /api/data/latest
  Dashboard polls this on first load to get an immediate
  reading before the WebSocket connection is established.

GET /api/data/history?limit=N
  Returns the last N readings for the power chart on the
  dashboard (used when the page is first opened to
  backfill the chart with historical data).

WebSocket /ws
  The dashboard connects here once and receives every new
  reading pushed as JSON — no polling needed.
  The ESP32 does NOT use this; it uses POST /api/data.
"""

from __future__ import annotations
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Query
from typing import List
import json

from app.models.schemas import SensorPayload, StatusResponse
from app.recorder import recorder
from app.store import store

router = APIRouter(prefix="/api/data", tags=["Data"])


# ── WebSocket connection manager ──────────────────────────────
class _ConnectionManager:
    """Tracks all active dashboard WebSocket connections."""

    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, payload: dict):
        """Push data to all connected dashboards."""
        message = json.dumps(payload)
        # Iterate over a copy — disconnect modifies the list
        for ws in list(self.active):
            try:
                await ws.send_text(message)
            except Exception:
                self.disconnect(ws)


manager = _ConnectionManager()


# ── POST /api/data  (ESP32 → Server) ─────────────────────────
@router.post("", response_model=StatusResponse, summary="Receive sensor data from ESP32")
async def receive_data(payload: SensorPayload):
    """
    Called by the ESP32 every 2 seconds with the full system snapshot.
    Stores the reading and pushes it to all connected dashboard clients
    via WebSocket.
    """
    reading = store.update(payload)

    # Build the WebSocket broadcast payload (include server timestamp)
    broadcast_payload = {
        "received_at": reading.received_at.isoformat(),
        **payload.model_dump()
    }
    await manager.broadcast(broadcast_payload)

    # History recording happens LAST and never blocks: offer() is float
    # arithmetic in memory, and anything that touches Supabase is handed to a
    # detached background task. The live path above — in-memory store, then
    # dashboard broadcast — completes whether or not the database exists, so
    # a Supabase outage costs history, not shedding.
    recorder.offer(reading)

    return StatusResponse(status="ok", message="Reading stored")


# ── GET /api/data/latest  (Dashboard first load) ─────────────
@router.get("/latest", summary="Get the most recent sensor reading")
async def get_latest():
    """
    Returns the most recent reading from the store.
    Used by the dashboard on first load before the WebSocket connects.
    Returns 503 if no data has been received yet from the ESP32.
    """
    if store.is_empty:
        raise HTTPException(
            status_code=503,
            detail="No data received from device yet. Is the ESP32 connected?"
        )
    reading = store.latest_reading
    return {
        "received_at": reading.received_at.isoformat(),
        **reading.data.model_dump()
    }


# ── GET /api/data/history  (Dashboard chart backfill) ────────
@router.get("/history", summary="Get recent reading history")
async def get_history(
    limit: int = Query(default=60, ge=1, le=120, description="Number of readings to return")
):
    """
    Returns the last `limit` readings (max 120).
    Used to backfill the dashboard power chart when the page loads.
    """
    readings = store.get_history(limit)
    return {
        "count": len(readings),
        "readings": [
            {"received_at": r.received_at.isoformat(), **r.data.model_dump()}
            for r in readings
        ]
    }


# ── WebSocket /ws  (Dashboard live feed) ─────────────────────
@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    Persistent WebSocket connection for the dashboard.
    The server pushes every new sensor reading as JSON
    the moment it arrives from the ESP32.

    On connect, immediately sends the latest reading so the
    dashboard populates instantly without waiting up to 2s.
    """
    await manager.connect(ws)
    try:
        # Send the latest reading immediately on connection
        if not store.is_empty:
            reading = store.latest_reading
            await ws.send_text(json.dumps({
                "received_at": reading.received_at.isoformat(),
                **reading.data.model_dump()
            }))

        # Keep the connection alive — the server pushes data,
        # we just wait here for a disconnect signal
        while True:
            # We don't expect messages from the dashboard via WS,
            # but we must await something to detect disconnects
            await ws.receive_text()

    except WebSocketDisconnect:
        manager.disconnect(ws)
