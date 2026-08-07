"""
routers/analytics.py
=====================
Read-side of the history database. Everything the dashboard's History tab
draws comes from here.

DESIGN RULE: AGGREGATE IN SQL, NOT IN PYTHON
─────────────────────────────────────────────
A month of samples is ~130,000 rows. Pulling those into the backend to sum
them in a loop would cost seconds of CPU on a free Render instance, megabytes
of Supabase egress against a 5 GB monthly allowance, and would do badly what
Postgres does well. Every endpoint below therefore returns tens of rows, not
thousands, and the GROUP BY happens on the database side.

TIME ZONES
──────────
Timestamps are stored in UTC, which is the only defensible way to store them.
But "energy used on Tuesday" means Tuesday in Lagos, and UTC+1 puts the last
hour of every Nigerian day into the next UTC day. So every query that buckets
by day or by hour-of-day converts with `AT TIME ZONE $tz` first. Getting this
wrong would not throw an error — it would just quietly misattribute an hour
of consumption per day, which is the sort of thing that survives all the way
into a printed results chapter.

ENERGY
──────
samples.kwh_cum is the meter's running total, which resets to zero at every
period rollover and on reset_energy. Summing a naive difference across a reset
would subtract a whole period's energy. The queries use
GREATEST(kwh_cum - LAG(kwh_cum), 0) — on a reset the difference is negative
and gets clamped, which loses at most one bucket of energy at the reset
boundary rather than corrupting the entire total.

HONEST EMPTINESS
────────────────
When the database is not configured, these endpoints return 503 with a reason
rather than an empty array. An empty chart and a chart of nothing look
identical, and only one of them means "no energy was used".
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from app import db
from app.recorder import BUCKET_SECONDS

router = APIRouter(prefix="/api/analytics", tags=["Analytics"])

# Two buckets are "adjacent" when they are one bucket apart, so the outage
# threshold has to be expressed in multiples of BUCKET_SECONDS, not as a fixed
# wall-clock figure. A hardcoded '5 minutes' works perfectly at the default
# 60 s and then silently routes EVERY delta into unattributed the moment
# someone raises BUCKET_SECONDS past 300 — energy totals would drop to zero
# with no error anywhere, which is precisely the kind of failure that gets
# noticed a week before a defence.
_GAP_LIMIT_S = max(300, int(BUCKET_SECONDS) * 5)


def _require_db() -> None:
    if not db.enabled():
        raise HTTPException(
            status_code=503,
            detail=("History database not configured. Set DATABASE_URL to a "
                    "Supabase pooler connection string and redeploy."),
        )


def _rows(records) -> List[Dict[str, Any]]:
    return [dict(r) for r in records]


# ── Per-bucket energy, reused by several endpoints ────────────
# LAG() is partitioned by channel so one channel's reset cannot leak into
# another's delta, and ordered by ts so the window is the previous bucket in
# time rather than in physical row order.
_DELTA_CTE = f"""
WITH raw AS (
    SELECT ts, ch, name, watts_avg, shed_s, on_s, n, kwh_cum,
           LAG(kwh_cum) OVER (PARTITION BY ch ORDER BY ts) AS prev_kwh,
           LAG(ts)      OVER (PARTITION BY ch ORDER BY ts) AS prev_ts
    FROM samples
    WHERE device_id = $1
      AND ts >= now() - make_interval(days => $2)
), d AS (
    SELECT ts, ch, name, watts_avg, shed_s, on_s, n,
           CASE
               -- First bucket in the window: no predecessor to difference
               -- against, so it contributes no energy rather than its whole
               -- running total.
               WHEN prev_kwh IS NULL THEN 0
               -- Counter went backwards: a period rollover or reset_energy.
               -- Costs one bucket of energy at the boundary, which is the
               -- cheapest correct answer available.
               WHEN kwh_cum < prev_kwh THEN 0
               -- Buckets are not adjacent, so the meter kept integrating
               -- through an outage this backend did not observe. The energy
               -- is real, but WHEN it was used is unknown, and booking an
               -- eight-hour blackout's consumption to the single minute the
               -- device came back would put a spike on the wrong day and the
               -- wrong hour. It is carried in kwh_gap instead and reported
               -- separately, so the total is never silently overstated OR
               -- silently dropped.
               WHEN ts - prev_ts > make_interval(secs => {_GAP_LIMIT_S}) THEN 0
               ELSE kwh_cum - prev_kwh
           END AS kwh,
           CASE
               WHEN prev_kwh IS NOT NULL
                    AND kwh_cum >= prev_kwh
                    AND ts - prev_ts > make_interval(secs => {_GAP_LIMIT_S})
               THEN kwh_cum - prev_kwh
               ELSE 0
           END AS kwh_gap
    FROM raw
)
"""


# ── GET /api/analytics/summary ────────────────────────────────
@router.get("/summary", summary="Headline figures for the History tab")
async def summary(days: int = Query(30, ge=1, le=365)):
    """
    The four numbers at the top of the History tab: how much data exists,
    total energy and cost over the window, and quota adherence.
    """
    _require_db()

    totals = await db.fetchrow(
        _DELTA_CTE + """
        SELECT COALESCE(SUM(kwh), 0)::float8                       AS kwh,
               COALESCE(SUM(kwh_gap), 0)::float8                   AS kwh_gap,
               MIN(ts)                                             AS first_ts,
               MAX(ts)                                             AS last_ts,
               COUNT(DISTINCT (ts AT TIME ZONE $3)::date)::int     AS days_seen
        FROM d
        """,
        db.DEVICE_ID, days, db.LOCAL_TZ)

    # Cost comes from the device's own running total rather than being
    # recomputed here: the firmware owns the tariff, and a second tariff
    # constant in the backend is a second thing to keep in step.
    #
    # It is summed as positive deltas for the same reason energy is. tc resets
    # to zero at every rollover alongside te, so MAX(cost) - MIN(cost) over a
    # multi-period window would report roughly one period's spend as if it
    # were the month's.
    cost = await db.fetchrow(
        """
        WITH c AS (
            SELECT GREATEST(
                       cost_ngn - LAG(cost_ngn) OVER (ORDER BY ts), 0
                   ) AS delta
            FROM system_samples
            WHERE device_id = $1
              AND ts >= now() - make_interval(days => $2)
        )
        SELECT COALESCE(SUM(delta), 0)::float8 AS spread FROM c
        """,
        db.DEVICE_ID, days)

    # Only periods whose start was actually witnessed count towards adherence.
    # A period the backend joined halfway through has a truncated energy
    # figure, and averaging it in would flatter the result.
    adherence = await db.fetchrow(
        """
        SELECT COUNT(*)::int                                     AS closed,
               COUNT(*) FILTER (WHERE within_quota)::int         AS within
        FROM periods
        WHERE device_id = $1
          AND ended_at IS NOT NULL
          AND witnessed_start
          AND ended_at >= now() - make_interval(days => $2)
        """,
        db.DEVICE_ID, days)

    closed = adherence["closed"] or 0
    within = adherence["within"] or 0

    return {
        "days_requested":  days,
        "days_with_data":  totals["days_seen"] or 0,
        "first_reading":   totals["first_ts"].isoformat() if totals["first_ts"] else None,
        "last_reading":    totals["last_ts"].isoformat() if totals["last_ts"] else None,
        "energy_kwh":      round(totals["kwh"] or 0.0, 3),
        # Energy the meter definitely recorded but that cannot be placed on a
        # day or an hour, because the backend was not running when it was
        # used. Non-zero here means the charts below understate consumption by
        # this much, and that is worth stating rather than burying.
        "unattributed_kwh": round(totals["kwh_gap"] or 0.0, 3),
        "cost_ngn":        round(cost["spread"] or 0.0, 2),
        "periods_closed":  closed,
        "periods_within":  within,
        # Null rather than 100% when nothing has closed yet. A percentage of
        # zero periods is not a perfect score, it is an absent one.
        "adherence_pct":   round(100.0 * within / closed, 1) if closed else None,
    }


# ── GET /api/analytics/daily ──────────────────────────────────
@router.get("/daily", summary="Energy per day, split by channel")
async def daily(days: int = Query(30, ge=1, le=365)):
    """
    Feeds the stacked daily bar chart. Days on which the device recorded
    nothing are simply absent from the result — the chart leaves a gap there
    rather than drawing a zero, because "no data" and "no consumption" are
    different claims.
    """
    _require_db()
    rows = await db.fetch(
        _DELTA_CTE + """
        SELECT (ts AT TIME ZONE $3)::date::text AS day,
               ch,
               MAX(name)                        AS name,
               SUM(kwh)::float8                 AS kwh,
               AVG(watts_avg)::float8           AS watts_avg,
               MAX(watts_avg)::float8           AS watts_peak,
               (SUM(shed_s) / 3600.0)::float8   AS shed_hours,
               (SUM(on_s)   / 3600.0)::float8   AS on_hours
        FROM d
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
        db.DEVICE_ID, days, db.LOCAL_TZ)
    return {"timezone": db.LOCAL_TZ, "count": len(rows), "rows": _rows(rows)}


# ── GET /api/analytics/profile ────────────────────────────────
@router.get("/profile", summary="Average load by hour of day")
async def profile(days: int = Query(7, ge=1, le=365)):
    """
    Average power per channel for each hour of the local day, over the window.

    This is the chart that only a database can produce: it needs weeks of
    history to be meaningful, and it answers a question the live dashboard
    cannot — when in the day does this household actually draw power, and
    which load is responsible.
    """
    _require_db()
    rows = await db.fetch(
        """
        SELECT EXTRACT(HOUR FROM ts AT TIME ZONE $3)::int AS hour,
               ch,
               MAX(name)                     AS name,
               AVG(watts_avg)::float8        AS watts_avg,
               MAX(watts_peak)::float8       AS watts_peak,
               COUNT(*)::int                 AS buckets
        FROM samples
        WHERE device_id = $1
          AND ts >= now() - make_interval(days => $2)
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
        db.DEVICE_ID, days, db.LOCAL_TZ)
    return {"timezone": db.LOCAL_TZ, "count": len(rows), "rows": _rows(rows)}


# ── GET /api/analytics/shedding ───────────────────────────────
@router.get("/shedding", summary="Load-shedding activity per channel")
async def shedding(days: int = Query(30, ge=1, le=365)):
    """
    How much work the load-shedding logic actually did, per channel.

    Duration comes from the summed shed_s in the minute buckets rather than
    from pairing shed_start/shed_end events. Pairing breaks whenever the
    backend restarts mid-episode and leaves an unmatched start; the buckets
    just keep counting seconds and cannot become unbalanced.
    """
    _require_db()
    dur = await db.fetch(
        """
        SELECT ch,
               MAX(name)                      AS name,
               (SUM(shed_s) / 3600.0)::float8 AS shed_hours,
               (SUM(on_s)   / 3600.0)::float8 AS on_hours
        FROM samples
        WHERE device_id = $1 AND ts >= now() - make_interval(days => $2)
        GROUP BY ch
        ORDER BY ch
        """,
        db.DEVICE_ID, days)

    # Episode count is a separate question from duration: twenty short sheds
    # and one long one can total the same hours while meaning very different
    # things about the hysteresis settings.
    eps = await db.fetch(
        """
        SELECT ch, COUNT(*)::int AS episodes
        FROM events
        WHERE device_id = $1
          AND kind = 'shed_start'
          AND ch IS NOT NULL
          AND ts >= now() - make_interval(days => $2)
        GROUP BY ch
        """,
        db.DEVICE_ID, days)

    counts = {r["ch"]: r["episodes"] for r in eps}
    out = []
    for r in dur:
        row = dict(r)
        row["episodes"] = counts.get(r["ch"], 0)
        out.append(row)
    return {"count": len(out), "rows": out}


# ── GET /api/analytics/periods ────────────────────────────────
@router.get("/periods", summary="Completed quota periods")
async def periods(days: int = Query(30, ge=1, le=365),
                  limit: int = Query(20, ge=1, le=200)):
    """
    One row per completed budgeting period. `witnessed_start` is exposed so
    the dashboard can mark, rather than hide, the rows whose start the backend
    did not see.

    Windowed by `days` for the same reason every other endpoint is: the
    adherence figure in /summary counts periods that closed inside the window,
    and a table that ignored the window would sit directly beneath it showing
    periods the percentage does not include. Two panels disagreeing on screen
    reads as a bug in the data even when both are right.
    """
    _require_db()
    rows = await db.fetch(
        """
        SELECT id, started_at, ended_at, witnessed_start, target_hours,
               quota_kwh, topup_kwh, energy_kwh, cost_ngn, auto_renew,
               within_quota
        FROM periods
        WHERE device_id = $1
          AND ended_at IS NOT NULL
          AND ended_at >= now() - make_interval(days => $2)
        ORDER BY ended_at DESC
        LIMIT $3
        """,
        db.DEVICE_ID, days, limit)
    return {"count": len(rows), "rows": _rows(rows)}


# ── GET /api/analytics/events ─────────────────────────────────
@router.get("/events", summary="Recent system and user events")
async def events(days: int = Query(7, ge=1, le=365),
                 limit: int = Query(100, ge=1, le=500),
                 kind: Optional[str] = Query(None, description="Filter by event kind")):
    _require_db()
    if kind:
        rows = await db.fetch(
            """
            SELECT ts, source, kind, ch, value, detail
            FROM events
            WHERE device_id = $1
              AND ts >= now() - make_interval(days => $2)
              AND kind = $4
            ORDER BY ts DESC
            LIMIT $3
            """,
            db.DEVICE_ID, days, limit, kind)
    else:
        rows = await db.fetch(
            """
            SELECT ts, source, kind, ch, value, detail
            FROM events
            WHERE device_id = $1
              AND ts >= now() - make_interval(days => $2)
            ORDER BY ts DESC
            LIMIT $3
            """,
            db.DEVICE_ID, days, limit)
    return {"count": len(rows), "rows": _rows(rows)}


# ── GET /api/analytics/uptime ─────────────────────────────────
@router.get("/uptime", summary="Recording coverage per day")
async def uptime(days: int = Query(30, ge=1, le=365)):
    """
    Buckets actually recorded per day against the number a full day would
    produce. This is the honest companion to every other chart here: it says
    how much of each day was observed, so a low bar can be read as an outage
    rather than as a quiet day.
    """
    _require_db()
    rows = await db.fetch(
        """
        SELECT (ts AT TIME ZONE $3)::date::text AS day,
               COUNT(*)::int                    AS buckets,
               SUM(n)::int                      AS readings
        FROM system_samples
        WHERE device_id = $1 AND ts >= now() - make_interval(days => $2)
        GROUP BY 1
        ORDER BY 1
        """,
        db.DEVICE_ID, days, db.LOCAL_TZ)
    full = max(1, int(86400 / BUCKET_SECONDS))
    out = []
    for r in rows:
        row = dict(r)
        row["coverage_pct"] = round(100.0 * row["buckets"] / full, 1)
        out.append(row)
    return {"timezone": db.LOCAL_TZ, "buckets_per_full_day": full,
            "count": len(out), "rows": out}
