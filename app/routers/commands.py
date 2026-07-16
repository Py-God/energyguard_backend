"""
routers/commands.py
====================
Command flow:  Dashboard → POST /api/commands → queue → ESP32 GET /api/commands

The ESP32 polls GET /api/commands every CMD_POLL_INTERVAL (1s).
The server returns all queued commands as a JSON array and clears
the queue — so each command is delivered exactly once.

The dashboard POSTs individual commands with a typed body.
FastAPI validates the body against the command schema before
it ever reaches the queue.

Endpoints:
  POST /api/commands        Queue a command from the dashboard
  GET  /api/commands        ESP32 collects and clears all pending commands
  GET  /api/commands/queue  Dashboard view: inspect queue without clearing
"""

from __future__ import annotations
from fastapi import APIRouter
from typing import Annotated, Union
from pydantic import Field

from app.models.schemas import (
    ToggleCommand,
    QuotaCommand,
    AutoShedCommand,
    ResetEnergyCommand,
    CommandListResponse,
    StatusResponse,
)
from app.store import store

router = APIRouter(prefix="/api/commands", tags=["Commands"])

# Discriminated union so FastAPI shows all 4 variants in /docs
AnyCommand = Annotated[
    Union[ToggleCommand, QuotaCommand, AutoShedCommand, ResetEnergyCommand],
    Field(discriminator="cmd")
]


# ── POST /api/commands  (Dashboard → Server) ──────────────────
@router.post("", response_model=StatusResponse, summary="Queue a command for the ESP32")
async def enqueue_command(command: AnyCommand):
    """
    Queue a command to be delivered to the ESP32 on its next poll.

    **Toggle a channel:**
    ```json
    {"cmd": "toggle", "ch": 2, "val": 0}
    ```
    ch is 0-indexed (0=CH1 Lighting, 1=CH2 Fan/TV, 2=CH3 Air Conditioner)

    **Update energy quota:**
    ```json
    {"cmd": "quota", "kwh": 5.0, "h": 24}
    ```

    **Enable / disable auto-shedding:**
    ```json
    {"cmd": "autoshed", "val": 1}
    ```

    **Reset all energy counters:**
    ```json
    {"cmd": "reset_energy"}
    ```
    """
    store.enqueue_command(command.model_dump())
    return StatusResponse(
        status="queued",
        message=f"Command '{command.cmd}' queued. Will be delivered on next ESP32 poll."
    )


# ── GET /api/commands  (ESP32 polls this) ─────────────────────
@router.get("", response_model=CommandListResponse, summary="ESP32: collect pending commands")
async def collect_commands():
    """
    Called by the ESP32 every CMD_POLL_INTERVAL (1 second).
    Returns all pending commands as a JSON array and **clears the queue**.
    Each command is delivered exactly once.

    Example response:
    ```json
    {
      "commands": [{"cmd": "toggle", "ch": 2, "val": 0}],
      "count": 1
    }
    ```
    """
    pending = store.flush_commands()
    return CommandListResponse(commands=pending, count=len(pending))


# ── GET /api/commands/queue  (Dashboard inspect — does NOT clear) ──
@router.get("/queue", response_model=CommandListResponse,
            summary="Inspect pending commands without consuming them")
async def inspect_queue():
    """
    Returns the current command queue without clearing it.
    Useful for debugging from the dashboard or /docs.
    """
    return CommandListResponse(
        commands=list(store.command_queue),
        count=len(store.command_queue)
    )
