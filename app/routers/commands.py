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
    TopUpCommand,
    AutoRenewCommand,
    AutoShedCommand,
    ResetEnergyCommand,
    CommandListResponse,
    StatusResponse,
)
from app.recorder import recorder
from app.store import store

router = APIRouter(prefix="/api/commands", tags=["Commands"])

# Discriminated union so FastAPI shows all 6 variants in /docs
AnyCommand = Annotated[
    Union[ToggleCommand, QuotaCommand, TopUpCommand,
          AutoRenewCommand, AutoShedCommand, ResetEnergyCommand],
    Field(discriminator="cmd")
]


# ── POST /api/commands  (Dashboard → Server) ──────────────────
@router.post("", response_model=StatusResponse, summary="Queue a command for the ESP32")
async def enqueue_command(command: AnyCommand):
    """
    Queue a command to be delivered to the ESP32 on its next poll.

    **Toggle a channel:**
    ```json
    {"cmd": "toggle", "ch": 3, "val": 0}
    ```
    ch is 0-indexed (0=CH1 Medical, 1=CH2 Lights, 2=CH3 Fan/TV, 3=CH4 AC)

    **Update the budget (edits the running period):**
    ```json
    {"cmd": "quota", "kwh": 5.0, "h": 24}
    ```
    Changing `h` moves the deadline; it does not restart the clock. Set this
    to 12 at hour 3 of a 24 h period and 9 hours remain, not 12.

    **Buy more credit (does not restart the clock):**
    ```json
    {"cmd": "topup", "kwh": 2.0}
    ```

    **Period rollover behaviour:**
    ```json
    {"cmd": "renew", "val": 0}
    ```
    1 = refill to the nominal quota at each deadline. 0 = carry the leftover
    forward and stay shed at zero until a top-up arrives.

    **Enable / disable auto-shedding:**
    ```json
    {"cmd": "autoshed", "val": 1}
    ```

    **Reset all energy counters:**
    ```json
    {"cmd": "reset_energy"}
    ```
    """
    payload = command.model_dump()
    store.enqueue_command(payload)
    # Logged as source='user': this records that the API ACCEPTED the
    # instruction, not that the ESP32 carried it out. The device-sourced event
    # written when the change shows up in telemetry is the evidence. Keeping
    # both is what makes a command that never took effect visible in the
    # history instead of invisible.
    recorder.note_command(payload)
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
      "commands": [{"cmd": "toggle", "ch": 3, "val": 0}],
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
