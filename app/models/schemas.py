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
    {"name":"Refrigeration","i":0.68,"p":150,"e":0.042,"on":1,"shed":0,
     "prio":1,"prot":1,"crit":1},
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
    # `prio` carries RANK: a permutation of 1..NUM_CHANNELS across the channel
    # array, where 1 is shed LAST and NUM_CHANNELS is shed FIRST. It replaced a
    # three-valued HIGH/MEDIUM/LOW enum without a key rename, because the
    # Supabase `prio` column and every row already in it key off this name.
    #
    # ⚠ The VALUE semantics changed even though the name did not. Rows written
    # before the firmware that made priority user-editable mean HIGH/MEDIUM/LOW;
    # rows after mean rank. Both are small positive integers and nothing in the
    # data distinguishes them, so evaluation queries spanning the changeover
    # need the cutover timestamp applied by hand.
    prio:  int   = Field(...,  description="Shed rank: 1 = shed last, N = shed first")

    # Optional so the backend keeps validating payloads from firmware that
    # predates runtime-editable priorities — server and device are deployed
    # separately and one is always older for a while.
    prot:  Optional[int] = Field(None, description="1 = protected; auto-shed will never open it")
    crit:  Optional[int] = Field(None, description="1 = critical load; un-protecting warrants confirmation")


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
    # Count of channels auto-shed is permitted to open. 0 is legal, not an
    # error: the user may protect everything, in which case shedding cannot
    # comply with the quota and the dashboard says so.
    sh:    Optional[int]       = Field(None, description="Number of sheddable (unprotected) channels")


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


class PriorityCommand(BaseModel):
    """Move one channel to a given position in the shed order.

    ONE MOVE PER COMMAND, deliberately. The firmware derives every other
    channel's new rank from this single move and so keeps the ordering a valid
    permutation of 1..N by construction. Shipping the whole ordering as an
    array would make a partially delivered ordering representable — and the
    command path is a fire-and-forget queue over a link that drops things.
    """
    cmd: Literal['prio']
    ch:  int = Field(..., ge=0, le=3, description="Channel index 0-3")
    val: int = Field(..., ge=1, le=4,
                     description="Target rank: 1 = shed last (most important), 4 = shed first")


class ProtectCommand(BaseModel):
    """Exempt a channel from auto-shedding entirely, or stop exempting it.

    Orthogonal to rank: a protected channel keeps its rank, which is what it
    will use if protection is later removed. Nothing here refuses to
    un-protect a critical load — the `crit` flag drives a confirmation and a
    standing warning in the dashboard, not a veto in the firmware.
    """
    cmd: Literal['protect']
    ch:  int = Field(..., ge=0, le=3, description="Channel index 0-3")
    val: int = Field(..., ge=0, le=1, description="1=protected, 0=sheddable")


class ResetEnergyCommand(BaseModel):
    # cmd: str = "reset_energy"
    cmd: Literal['reset_energy']


# Union type for the command queue
CommandPayload = (ToggleCommand | QuotaCommand | TopUpCommand
                  | AutoRenewCommand | AutoShedCommand | PriorityCommand
                  | ProtectCommand | ResetEnergyCommand)


# ── API response wrappers ──────────────────────────────────────
class StatusResponse(BaseModel):
    status:  str
    message: Optional[str] = None


class CommandListResponse(BaseModel):
    commands: List[dict]
    count:    int
