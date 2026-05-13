"""
store.py
=========
Simple in-memory state store.

Why no database yet?
─────────────────────
During development and hardware testing, an in-memory store is
sufficient and eliminates setup friction. The store is designed
so that swapping in SQLite (or any DB) later only requires
changing this file — all routers import from here, not from
a DB session directly.

What lives here:
  - latest_reading  : the most recent SensorPayload from the ESP32
  - command_queue   : list of dicts waiting to be collected by the ESP32
  - reading_history : rolling list of the last HISTORY_SIZE readings
                      (used by the dashboard to draw the power chart)
"""

from __future__ import annotations
from collections import deque
from datetime import datetime, timezone
from typing import Optional, List
from app.models.schemas import SensorPayload, StoredReading

# ── Configuration ─────────────────────────────────────────────
HISTORY_SIZE = 120   # Keep last 120 readings (2 min at 1/sec)


# ── Singleton store ────────────────────────────────────────────
class _Store:
    def __init__(self):
        self.latest_reading: Optional[StoredReading] = None
        self.command_queue:  List[dict]              = []
        self.history:        deque[StoredReading]    = deque(maxlen=HISTORY_SIZE)

    # ── Data ingestion ─────────────────────────────────────────
    def update(self, payload: SensorPayload) -> StoredReading:
        reading = StoredReading(
            received_at=datetime.now(timezone.utc),
            data=payload
        )
        self.latest_reading = reading
        self.history.append(reading)
        return reading

    # ── Command queue ──────────────────────────────────────────
    def enqueue_command(self, cmd: dict) -> None:
        self.command_queue.append(cmd)

    def flush_commands(self) -> List[dict]:
        """Return all pending commands and clear the queue."""
        pending = list(self.command_queue)
        self.command_queue.clear()
        return pending

    # ── History ────────────────────────────────────────────────
    def get_history(self, limit: int = HISTORY_SIZE) -> List[StoredReading]:
        return list(self.history)[-limit:]

    @property
    def is_empty(self) -> bool:
        return self.latest_reading is None


# Single module-level instance — imported everywhere
store = _Store()
