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
    {"name":"Lighting","i":0.27,"p":60,"e":0.042,"on":1,"shed":0,"prio":1},
    {"name":"Fan/TV","i":0.82,"p":180,"e":0.115,"on":1,"shed":0,"prio":2},
    {"name":"Air Conditioner","i":6.8,"p":1500,"e":0.940,"on":0,"shed":1,"prio":3}
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
    name:  str   = Field(...,  description="Channel label, e.g. 'Lighting'")
    i:     float = Field(...,  description="RMS current (A)")
    p:     float = Field(...,  description="Instantaneous power (W)")
    e:     float = Field(...,  description="Cumulative energy (kWh)")
    on:    int   = Field(...,  description="1 = energised, 0 = off")
    shed:  int   = Field(...,  description="1 = auto-shed active on this channel")
    prio:  int   = Field(...,  description="Priority: 1=HIGH, 2=MEDIUM, 3=LOW")


# ── Full sensor payload (POST /api/data from ESP32) ────────────
class SensorPayload(BaseModel):
    v:     float               = Field(...,  description="Mains RMS voltage (V)")
    ch:    List[ChannelReading] = Field(
        ..., min_length=3, max_length=3,
        description="Per-channel readings, index-parallel with the firmware: "
                    "0=Lighting, 1=Fan/TV, 2=Air Conditioner",
    )
    te:    float               = Field(...,  description="Total energy used (kWh)")
    tc:    float               = Field(...,  description="Total cost (₦)")
    qr:    float               = Field(...,  description="Quota remaining (kWh)")
    sr:    float               = Field(...,  description="Sustainable rate (W)")
    eta:   float               = Field(...,  description="Hours until depletion")
    avgP:  float               = Field(...,  description="Rolling-average power (W)")
    shed:  int                 = Field(...,  description="1 = system is shedding")
    auto:  int                 = Field(...,  description="1 = auto-shed enabled")

    # Quota + target period, reported BY the device. These now persist in the
    # ESP32's NVS, which makes the device authoritative: the dashboard must
    # render what it is told instead of assuming its own local defaults still
    # match. Optional, so firmware predating these fields still validates rather
    # than 422-ing every POST during a staggered rollout.
    q:     Optional[float]     = Field(None, gt=0, description="Energy quota (kWh)")
    th:    Optional[float]     = Field(None, gt=0, description="Target period (hours)")


# ── Enriched reading stored in memory (adds server timestamp) ─
class StoredReading(BaseModel):
    received_at: datetime
    data:        SensorPayload


# ── Commands (dashboard → server → ESP32) ─────────────────────

class ToggleCommand(BaseModel):
    # cmd: str = "toggle"
    cmd: Literal['toggle']
    # le=2, not le=3. NUM_CHANNELS is now 3, so ch=3 is out of range. The
    # firmware's Channels_ManualToggle() already rejects it, but validating
    # here means the dashboard gets a 422 telling it what went wrong instead
    # of a command that queues, ships, and is silently dropped on the device.
    ch:  int = Field(..., ge=0, le=2, description="Channel index 0-2")
    val: int = Field(..., ge=0, le=1, description="1=ON, 0=OFF")


class QuotaCommand(BaseModel):
    # cmd: str   = "quota"
    cmd: Literal['quota']
    kwh: float = Field(..., gt=0, description="Energy quota (kWh)")
    h:   float = Field(..., gt=0, description="Target period (hours)")


class AutoShedCommand(BaseModel):
    # cmd: str = "autoshed"
    cmd: Literal['autoshed']
    val: int = Field(..., ge=0, le=1, description="1=enable, 0=disable")


class ResetEnergyCommand(BaseModel):
    # cmd: str = "reset_energy"
    cmd: Literal['reset_energy']


# Union type for the command queue
CommandPayload = ToggleCommand | QuotaCommand | AutoShedCommand | ResetEnergyCommand


# ── API response wrappers ──────────────────────────────────────
class StatusResponse(BaseModel):
    status:  str
    message: Optional[str] = None


class CommandListResponse(BaseModel):
    commands: List[dict]
    count:    int
