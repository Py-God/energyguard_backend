"""
models/schemas.py
==================
Pydantic v2 models that mirror the ESP32 JSON protocol exactly.

These serve three purposes:
  1. Automatic request validation — FastAPI rejects malformed payloads
     before they reach any business logic.
  2. Auto-generated OpenAPI docs — the /docs page shows the full
     expected shape of every request and response.
  3. Type safety — all downstream code works with typed objects,
     not raw dicts.

Matches the ESP32 payload from wifi_comms.cpp:
{
  "v": 220.1,
  "ch": [
    {"name":"Medical/Fridge","i":0.68,"p":150,"e":0.042,"on":1,"shed":0,"prio":1},
    ...
  ],
  "te": 0.123,  "tc": 27.6,  "qr": 4.877,
  "sr": 208.3,  "eta": 23.4, "avgP": 160.0,
  "shed": 0,    "auto": 1
}
"""

from __future__ import annotations
from typing import List, Optional, Literal
from pydantic import BaseModel, Field
from datetime import datetime


# ── Per-channel data (matches ESP32 "ch" array element) ───────
class ChannelReading(BaseModel):
    name:  str   = Field(...,  description="Channel label, e.g. 'Medical/Fridge'")
    i:     float = Field(...,  description="RMS current (A)")
    p:     float = Field(...,  description="Instantaneous power (W)")
    e:     float = Field(...,  description="Cumulative energy (kWh)")
    on:    int   = Field(...,  description="1 = energised, 0 = off")
    shed:  int   = Field(...,  description="1 = auto-shed active on this channel")
    prio:  int   = Field(...,  description="Priority: 1=HIGH, 2=MEDIUM, 3=LOW")


# ── Full sensor payload (POST /api/data from ESP32) ────────────
class SensorPayload(BaseModel):
    v:     float               = Field(...,  description="Mains RMS voltage (V)")
    ch:    List[ChannelReading] = Field(...,  description="Per-channel readings")
    te:    float               = Field(...,  description="Total energy used (kWh)")
    tc:    float               = Field(...,  description="Total cost (₦)")
    qr:    float               = Field(...,  description="Quota remaining (kWh)")
    sr:    float               = Field(...,  description="Sustainable rate (W)")
    eta:   float               = Field(...,  description="Hours until depletion")
    avgP:  float               = Field(...,  description="Rolling-average power (W)")
    shed:  int                 = Field(...,  description="1 = system is shedding")
    auto:  int                 = Field(...,  description="1 = auto-shed enabled")

    # Optional so the backend keeps validating payloads from firmware that
    # predates these fields. Server and device are flashed separately and one
    # of them is always older for a while; a required field here would reject
    # every reading in that window. Note q/th were already being SENT by
    # wifi_comms.cpp and silently dropped for want of a model entry.
    q:     Optional[float]     = Field(None, description="Current period credit (kWh)")
    th:    Optional[float]     = Field(None, description="Target period (hours)")
    rq:    Optional[float]     = Field(None, description="Auto-renew refill amount (kWh)")
    el:    Optional[float]     = Field(None, description="Hours elapsed in period")
    hl:    Optional[float]     = Field(None, description="Hours left in period")
    ar:    Optional[int]       = Field(None, description="1 = auto-renew at deadline")
    ck:    Optional[int]       = Field(None, description="1 = wall clock synced (NTP)")


# ── Enriched reading stored in memory (adds server timestamp) ─
class StoredReading(BaseModel):
    received_at: datetime
    data:        SensorPayload


# ── Commands (dashboard → server → ESP32) ─────────────────────

class ToggleCommand(BaseModel):
    # cmd: str = "toggle"
    cmd: Literal['toggle']
    ch:  int = Field(..., ge=0, le=3, description="Channel index 0-3")
    val: int = Field(..., ge=0, le=1, description="1=ON, 0=OFF")


class QuotaCommand(BaseModel):
    # cmd: str   = "quota"
    cmd: Literal['quota']
    kwh: float = Field(..., gt=0, description="Energy quota (kWh)")
    h:   float = Field(..., gt=0, description="Target period (hours)")


class TopUpCommand(BaseModel):
    """Buy more credit. ADDS to the running period rather than replacing it,
    and does not restart the pacing clock — distinct from QuotaCommand, which
    edits the budget parameters themselves."""
    cmd: Literal['topup']
    kwh: float = Field(..., gt=0, description="Credit to ADD to the current period (kWh)")


class AutoRenewCommand(BaseModel):
    """What happens at the deadline. Neither mode cuts the user off; the
    difference is only whether the stock is refilled."""
    cmd: Literal['renew']
    val: int = Field(..., ge=0, le=1,
                     description="1=refill to nominal quota, 0=carry leftover and wait for top-up")


class AutoShedCommand(BaseModel):
    # cmd: str = "autoshed"
    cmd: Literal['autoshed']
    val: int = Field(..., ge=0, le=1, description="1=enable, 0=disable")


class ResetEnergyCommand(BaseModel):
    # cmd: str = "reset_energy"
    cmd: Literal['reset_energy']


# Union type for the command queue
CommandPayload = (ToggleCommand | QuotaCommand | TopUpCommand
                  | AutoRenewCommand | AutoShedCommand | ResetEnergyCommand)


# ── API response wrappers ──────────────────────────────────────
class StatusResponse(BaseModel):
    status:  str
    message: Optional[str] = None


class CommandListResponse(BaseModel):
    commands: List[dict]
    count:    int
