"""
app/recorder.py
================
Turns the 2-second live reading stream into durable history.

Three things get written, at three very different rates:

  1. MINUTE BUCKETS   (samples, system_samples)
     ~30 readings folded into one row per channel per minute. See schema.sql
     for why the raw stream is not stored.

  2. EVENTS           (events)
     Written only when something CHANGES: a channel sheds or recovers, the
     user tops up, a period rolls over. Rare, small, and where most of the
     interesting analysis lives.

  3. PERIODS          (periods)
     One row per quota period, opened and closed by watching the firmware's
     own rollover, not by a clock on this side.

WHAT THIS MODULE WILL NOT DO
────────────────────────────
It records what the device reported and nothing else. It does not interpolate
across gaps, does not carry the last reading forward through an outage, and
does not synthesise a bucket for a minute in which no reading arrived. A
missing minute is missing in the database and shows as a gap in the charts,
because a plotted line that bridges an outage is a claim about energy nobody
measured.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from app import db
from app.models.schemas import SensorPayload, StoredReading

log = logging.getLogger("energyguard.recorder")

# One row per channel per minute. Shorter buckets give finer charts at linear
# storage cost; 60 s is the point where a day of data is still a few thousand
# rows and an hour-of-day profile is still smooth.
BUCKET_SECONDS = max(10, int(os.getenv("BUCKET_SECONDS", "60")))

# A single reading is credited with at most this many seconds of "on" or
# "shed" time. Without a cap, one reading either side of a two-hour outage
# would book the whole outage as energised time on whatever state it happened
# to be in.
MAX_READING_WEIGHT_S = 10.0

# The firmware's "no meaningful projection" sentinel for hours-to-depletion.
ETA_SENTINEL = 999.0


# ── Per-channel accumulator ───────────────────────────────────
class _ChanAcc:
    __slots__ = ("name", "prio", "n", "w_sum", "w_peak", "a_sum",
                 "kwh", "on_s", "shed_s")

    def __init__(self) -> None:
        self.name = ""
        self.prio = 0
        self.n = 0
        self.w_sum = 0.0
        self.w_peak = 0.0
        self.a_sum = 0.0
        self.kwh = 0.0
        self.on_s = 0.0
        self.shed_s = 0.0


class _SysAcc:
    __slots__ = ("n", "v_sum", "w_sum", "w_peak", "kwh", "cost", "quota",
                 "qr", "sr_sum", "eta", "shed_s", "auto", "ck")

    def __init__(self) -> None:
        self.n = 0
        self.v_sum = 0.0
        self.w_sum = 0.0
        self.w_peak = 0.0
        self.kwh = 0.0
        self.cost = 0.0
        self.quota: Optional[float] = None
        self.qr = 0.0
        self.sr_sum = 0.0
        self.eta: Optional[float] = None
        self.shed_s = 0.0
        self.auto = 0
        self.ck: Optional[int] = None


def _bucket_start(ts: datetime) -> datetime:
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(
        epoch - (epoch % BUCKET_SECONDS), tz=timezone.utc)


class Recorder:
    def __init__(self) -> None:
        self._bucket: Optional[datetime] = None
        self._chans: List[_ChanAcc] = []
        self._sys = _SysAcc()
        self._prev: Optional[SensorPayload] = None
        self._prev_ts: Optional[datetime] = None

        self._period_id: Optional[int] = None
        self._period_lock = asyncio.Lock()
        # Bucket writes are spawned as detached tasks, so without this two
        # slow writes could complete in the opposite order to the minutes they
        # describe. The rows themselves are keyed by ts and would survive that,
        # but the running totals on the open period row are last-write-wins and
        # would be left showing an earlier minute's figures. asyncio.Lock wakes
        # waiters FIFO, and the tasks are created in bucket order, so taking it
        # here restores chronological order for the whole write path.
        self._write_lock = asyncio.Lock()
        self._pending_topup = 0.0      # top-ups seen before the row was opened
        self._last_prune: Optional[datetime] = None

    # ── Startup ───────────────────────────────────────────────
    async def resume(self) -> None:
        """
        Re-attach to whatever period was open when the server last stopped.
        Render restarts the process on every deploy and after every cold
        start; without this, each restart would orphan the open row and begin
        a new one, splitting one real period into several short fictional
        ones.
        """
        if not db.enabled():
            return
        try:
            row = await db.fetchrow(
                "SELECT id, topup_kwh FROM periods "
                "WHERE device_id = $1 AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                db.DEVICE_ID)
            if row is None:
                log.info("no open period to resume")
                return
            self._period_id = int(row["id"])
            # Defensive: more than one open row means a previous run crashed
            # between opening and closing. Close the strays so the "current
            # period" query stays unambiguous.
            await db.execute(
                "UPDATE periods SET ended_at = now() "
                "WHERE device_id = $1 AND ended_at IS NULL AND id <> $2",
                db.DEVICE_ID, self._period_id)
            log.info("resumed open period id=%d", self._period_id)
        except Exception as exc:
            log.error("period resume failed: %s", exc)

    # ── Ingestion (called from POST /api/data) ────────────────
    def offer(self, reading: StoredReading) -> None:
        """
        Fold one live reading into the current bucket.

        Synchronous and cheap by design — arithmetic on a handful of floats.
        Anything that touches the network is handed to db.spawn() so the
        ESP32's POST returns without waiting for Supabase.
        """
        if not db.enabled():
            return
        try:
            self._ingest(reading)
        except Exception as exc:
            # A bug in history accounting must not turn into a 500 on the
            # endpoint the device depends on.
            log.error("recorder ingest failed: %s", exc)

    def _ingest(self, reading: StoredReading) -> None:
        ts = reading.received_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        payload = reading.data

        bucket = _bucket_start(ts)

        # Seconds this reading speaks for: the gap back to the previous one,
        # capped so an outage cannot be booked as observed time.
        if self._prev_ts is None:
            dt = 0.0
        else:
            dt = max(0.0, min((ts - self._prev_ts).total_seconds(),
                              MAX_READING_WEIGHT_S))

        if self._bucket is None:
            self._start_bucket(bucket, len(payload.ch))
            dt = min(dt, (ts - bucket).total_seconds())
        elif bucket != self._bucket:
            # This interval straddles a bucket boundary. Credit the part lying
            # before the boundary to the OUTGOING bucket, then flush, then
            # carry only the remainder into the new one.
            #
            # Clipping the whole interval to the new bucket instead — the
            # obvious shortcut — throws away on average half a reading
            # interval at every boundary. At 2 s posts and 60 s buckets that
            # is a systematic ~1.7% undercount on every "hours shed" and
            # "hours energised" figure the analysis reports, always in the
            # same direction, which is exactly the kind of quiet bias that is
            # impossible to spot in a finished chart.
            spill = 0.0
            if self._prev_ts is not None:
                spill = min(dt, max(0.0, (bucket - self._prev_ts).total_seconds()))
            if spill > 0.0:
                self._credit_time(payload, spill)
            self._flush()
            self._start_bucket(bucket, len(payload.ch))
            dt = max(0.0, dt - spill)

        # ── Channels ──────────────────────────────────────────
        while len(self._chans) < len(payload.ch):
            self._chans.append(_ChanAcc())
        for i, c in enumerate(payload.ch):
            a = self._chans[i]
            a.name = c.name
            a.prio = c.prio
            a.n += 1
            a.w_sum += c.p
            a.w_peak = max(a.w_peak, c.p)
            a.a_sum += c.i
            a.kwh = c.e               # last value wins: it is a running total
        self._credit_time(payload, dt)

        # ── System ────────────────────────────────────────────
        s = self._sys
        total_w = sum(c.p for c in payload.ch)
        s.n += 1
        s.v_sum += payload.v
        s.w_sum += total_w
        s.w_peak = max(s.w_peak, total_w)
        s.kwh = payload.te
        s.cost = payload.tc
        s.qr = payload.qr
        s.sr_sum += payload.sr
        s.quota = payload.q
        s.auto = payload.auto
        s.ck = payload.ck
        s.eta = None if payload.eta is None or payload.eta > ETA_SENTINEL \
            else payload.eta

        # ── Transitions ───────────────────────────────────────
        events = self._detect(self._prev, payload, ts)
        rollover = any(e[1] == "period_start" for e in events)
        if events:
            db.spawn(self._write_events(events))
        if rollover and self._prev is not None:
            db.spawn(self._roll_period(ts, self._prev, payload))

        self._prev = payload
        self._prev_ts = ts

    def _start_bucket(self, bucket: datetime, n_chan: int) -> None:
        self._bucket = bucket
        self._chans = [_ChanAcc() for _ in range(n_chan)]
        self._sys = _SysAcc()

    def _credit_time(self, payload: SensorPayload, seconds: float) -> None:
        """
        Book `seconds` of elapsed time against the states in `payload`.

        Kept separate from the sample accumulation above because a sample
        belongs wholly to one bucket while the interval it represents may have
        to be divided between two.
        """
        if seconds <= 0.0:
            return
        while len(self._chans) < len(payload.ch):
            self._chans.append(_ChanAcc())
        for i, c in enumerate(payload.ch):
            a = self._chans[i]
            if c.on == 1:
                a.on_s += seconds
            if c.shed == 1:
                a.shed_s += seconds
        if payload.shed == 1:
            self._sys.shed_s += seconds

    # ── Event detection ───────────────────────────────────────
    def _detect(self, prev: Optional[SensorPayload], cur: SensorPayload,
                ts: datetime) -> List[Tuple]:
        """
        Compare consecutive readings and emit a row per state change.
        Returns tuples of (ts, kind, ch, value, detail).
        """
        out: List[Tuple] = []
        if prev is None:
            out.append((ts, "device_seen", None, None,
                        "First reading after backend start"))
            return out

        # Per-channel relay and shed transitions.
        for i, c in enumerate(cur.ch):
            if i >= len(prev.ch):
                continue
            p = prev.ch[i]
            if c.on != p.on:
                out.append((ts, "channel_on" if c.on == 1 else "channel_off",
                            i, None, c.name))
            if c.shed != p.shed:
                out.append((ts, "shed_start" if c.shed == 1 else "shed_end",
                            i, c.p, c.name))

        if cur.shed != prev.shed:
            out.append((ts,
                        "system_shed_start" if cur.shed == 1 else "system_shed_end",
                        None, cur.qr, None))
        if cur.auto != prev.auto:
            out.append((ts,
                        "autoshed_enabled" if cur.auto == 1 else "autoshed_disabled",
                        None, None, None))
        if cur.ar is not None and prev.ar is not None and cur.ar != prev.ar:
            out.append((ts, "renew_mode", None, float(cur.ar),
                        "auto-renew" if cur.ar == 1 else "carry-over"))
        if cur.ck == 1 and prev.ck == 0:
            out.append((ts, "clock_synced", None, None, None))

        # A rollover is the firmware restarting the pacing clock, so elapsed
        # hours go backwards. te dropping is the fallback signal for firmware
        # that predates the el field — both are checked because either alone
        # has a blind spot: el is absent on old builds, and te legitimately
        # stays near zero across a rollover on an idle system.
        rolled = False
        if cur.el is not None and prev.el is not None and cur.el < prev.el - 0.02:
            rolled = True
        elif cur.te < prev.te - 0.001:
            rolled = True
        if rolled:
            out.append((ts, "period_start", None, cur.q, None))

        # Distinguishing a top-up from a quota edit: Predictor_SetQuota writes
        # the same value to BOTH quotaKwh and renewKwh, while Predictor_TopUp
        # raises quotaKwh alone. So a change in rq means the budget itself was
        # edited; a rise in q with rq unchanged is credit being added.
        if not rolled and cur.q is not None and prev.q is not None:
            rq_changed = (cur.rq is not None and prev.rq is not None
                          and abs(cur.rq - prev.rq) > 1e-6)
            if rq_changed:
                out.append((ts, "quota_set", None, cur.q,
                            f"{cur.th:g} h" if cur.th is not None else None))
            elif cur.q > prev.q + 1e-6:
                out.append((ts, "topup", None, cur.q - prev.q, None))

        if (not rolled and cur.th is not None and prev.th is not None
                and abs(cur.th - prev.th) > 1e-6):
            out.append((ts, "target_hours", None, cur.th, None))

        return out

    # ── Writers ───────────────────────────────────────────────
    async def _write_events(self, events: List[Tuple],
                            source: str = "device") -> None:
        await db.executemany(
            "INSERT INTO events (ts, device_id, source, kind, ch, value, detail) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            [(ts, db.DEVICE_ID, source, kind, ch, value, detail)
             for (ts, kind, ch, value, detail) in events])

    def _flush(self) -> None:
        """Close the current bucket and schedule its write."""
        bucket = self._bucket
        sys_acc = self._sys
        if bucket is None or sys_acc.n == 0:
            return

        chan_rows = [
            (bucket, db.DEVICE_ID, i, a.name, a.prio,
             a.w_sum / a.n, a.w_peak, a.a_sum / a.n, a.kwh,
             a.on_s, a.shed_s, a.n)
            for i, a in enumerate(self._chans) if a.n > 0
        ]
        sys_row = (
            bucket, db.DEVICE_ID,
            sys_acc.v_sum / sys_acc.n,
            sys_acc.w_sum / sys_acc.n,
            sys_acc.w_peak,
            sys_acc.kwh, sys_acc.cost, sys_acc.quota, sys_acc.qr,
            sys_acc.sr_sum / sys_acc.n, sys_acc.eta,
            sys_acc.shed_s, sys_acc.auto, sys_acc.ck, sys_acc.n,
        )
        db.spawn(self._write_bucket(chan_rows, sys_row, sys_acc))

    async def _write_bucket(self, chan_rows: List[tuple], sys_row: tuple,
                            sys_acc: _SysAcc) -> None:
        async with self._write_lock:
            # ON CONFLICT DO NOTHING rather than an upsert: a duplicate primary
            # key here means the same minute is being written twice, which only
            # happens if two backend instances are live at once. Keeping the
            # first write is arbitrary but at least deterministic; overwriting
            # would let two partial views of the same minute alternate.
            await db.executemany(
                "INSERT INTO samples (ts, device_id, ch, name, prio, watts_avg, "
                "watts_peak, amps_avg, kwh_cum, on_s, shed_s, n) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) "
                "ON CONFLICT DO NOTHING",
                chan_rows)
            await db.execute(
                "INSERT INTO system_samples (ts, device_id, volts_avg, watts_avg, "
                "watts_peak, kwh_cum, cost_ngn, quota_kwh, quota_rem, sustain_w, "
                "eta_h, shed_s, auto_shed, clock_ok, n) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) "
                "ON CONFLICT DO NOTHING",
                *sys_row)

            await self._touch_period(sys_row[0], sys_acc)
            await self._maybe_prune(sys_row[0])

    # ── Period bookkeeping ────────────────────────────────────
    async def _touch_period(self, ts: datetime, sys_acc: _SysAcc) -> None:
        """Open the period row if there isn't one, then keep its totals current."""
        async with self._period_lock:
            if self._period_id is None:
                row = await db.fetchrow(
                    "INSERT INTO periods (device_id, started_at, witnessed_start, "
                    "topup_kwh) VALUES ($1, $2, FALSE, $3) RETURNING id",
                    db.DEVICE_ID, ts, self._pending_topup)
                self._period_id = int(row["id"])
                self._pending_topup = 0.0
                log.info("opened period id=%d (start not witnessed)",
                         self._period_id)

            prev = self._prev
            await db.execute(
                "UPDATE periods SET quota_kwh = $2, energy_kwh = $3, "
                "cost_ngn = $4, target_hours = $5, auto_renew = $6 "
                "WHERE id = $1",
                self._period_id, sys_acc.quota, sys_acc.kwh, sys_acc.cost,
                prev.th if prev else None,
                (prev.ar == 1) if (prev and prev.ar is not None) else None)

    async def _roll_period(self, ts: datetime, prev: SensorPayload,
                           cur: SensorPayload) -> None:
        """
        Close the outgoing period with the LAST reading of the old one, then
        open the next. `prev` is used deliberately: by the time `cur` arrives
        the firmware has already re-baselined energy to zero, so reading the
        final consumption off `cur` would record every period as using 0 kWh.
        """
        async with self._period_lock:
            within = None
            if prev.q is not None:
                within = prev.te <= prev.q + 1e-6
            if self._period_id is not None:
                await db.execute(
                    "UPDATE periods SET ended_at = $2, energy_kwh = $3, "
                    "cost_ngn = $4, quota_kwh = $5, within_quota = $6 "
                    "WHERE id = $1",
                    self._period_id, ts, prev.te, prev.tc, prev.q, within)
            row = await db.fetchrow(
                "INSERT INTO periods (device_id, started_at, witnessed_start, "
                "target_hours, quota_kwh, auto_renew) "
                "VALUES ($1, $2, TRUE, $3, $4, $5) RETURNING id",
                db.DEVICE_ID, ts, cur.th, cur.q,
                (cur.ar == 1) if cur.ar is not None else None)
            self._period_id = int(row["id"])
            log.info("period rolled over — new id=%d", self._period_id)

    async def _record_topup(self, kwh: float) -> None:
        async with self._period_lock:
            if self._period_id is None:
                self._pending_topup += kwh
                return
            await db.execute(
                "UPDATE periods SET topup_kwh = topup_kwh + $2 WHERE id = $1",
                self._period_id, kwh)

    # ── User commands (called from the commands router) ───────
    def note_command(self, cmd: dict) -> None:
        """
        Log a command the API accepted. This is the user's INTENT; the matching
        device event, if any, is logged separately when the ESP32 reports the
        change. Keeping both is what makes an unacknowledged command visible
        instead of invisible.
        """
        if not db.enabled():
            return
        name = cmd.get("cmd", "?")
        value = None
        for key in ("kwh", "val", "h"):
            if key in cmd and isinstance(cmd[key], (int, float)):
                value = float(cmd[key])
                break
        ch = cmd.get("ch")
        ts = datetime.now(timezone.utc)
        db.spawn(self._write_events(
            [(ts, f"cmd_{name}", ch, value, None)], source="user"))
        if name == "topup" and isinstance(cmd.get("kwh"), (int, float)):
            db.spawn(self._record_topup(float(cmd["kwh"])))

    # ── Retention ─────────────────────────────────────────────
    async def _maybe_prune(self, now: datetime) -> None:
        if self._last_prune is not None and now - self._last_prune < timedelta(days=1):
            return
        self._last_prune = now
        await db.prune()


# Single module-level instance — imported by the routers, same as store.
recorder = Recorder()
