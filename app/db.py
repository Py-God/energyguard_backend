"""
app/db.py
==========
Postgres (Supabase) connection pool, schema bootstrap, and query helpers.

WHY POSTGRES AND NOT SQLITE
────────────────────────────
SQLite was scoped for this project and deliberately dropped: Render's free
tier gives every deploy a fresh, ephemeral filesystem, so a .db file on disk
survives exactly until the next push or the next cold start. Persistence that
does not persist is worse than none — it invites you to trust a history that
silently truncates. A hosted Postgres lives outside the dyno, so the data
survives redeploys, and it can do the aggregation itself.

FAILURE POSTURE
───────────────
The database is an OBSERVER of the system, never a participant in it. Load
shedding, the command queue and the live WebSocket feed must all keep working
with the database down, unreachable, full, or simply not configured. So:

  • If DATABASE_URL is unset, everything here no-ops and the rest of the
    backend behaves exactly as it did before this module existed.
  • Every write is fire-and-forget on a background task, so the ESP32's
    2-second POST never waits on a round trip to Supabase.
  • Every background task swallows its own exceptions after logging them.
    A dropped history row is an acceptable loss; a 500 on /api/data is not.

CONNECTING FROM RENDER
──────────────────────
Use Supabase's *pooler* connection string (Supavisor), not the direct one.
Since January 2024 the direct host db.<ref>.supabase.co resolves to IPv6 only,
the IPv4 add-on is Pro-and-above, and Render's outbound network is IPv4 — so
the direct string fails with "network is unreachable" and nothing more useful.
The pooler host is dual-stack and looks like:

    postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres

Prefer port 5432 (SESSION mode). This backend is one long-lived process, which
is exactly the case session mode is for: one dedicated connection, full
Postgres feature support, no surprises.

Port 6543 (TRANSACTION mode) also works and is what the dashboard offers by
default, but it multiplexes client sessions onto shared server connections and
so cannot carry server-side prepared statements — asyncpg prepares every query
by default and fails there with 'prepared statement "__asyncpg_stmt_x__"
already exists'. statement_cache_size=0 below is the documented fix and is set
unconditionally, so either string works: at three writes a minute the lost
microseconds are irrelevant, and the alternative is a deploy that dies on a
detail of which port got pasted into an environment variable.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Iterable, List, Optional

try:
    import asyncpg
except ImportError:      # pragma: no cover — lets the app boot without the dep
    asyncpg = None       # type: ignore

log = logging.getLogger("energyguard.db")

# ── Configuration (all via environment) ───────────────────────
DATABASE_URL   = os.getenv("DATABASE_URL", "").strip()
# One device today. The column costs nothing now and is the difference between
# "this system monitors a house" and "this system monitors houses" later.
DEVICE_ID      = os.getenv("DEVICE_ID", "eg-01").strip() or "eg-01"
# Nigeria is UTC+1 year round. Server timestamps are stored in UTC (correct);
# this is only used to decide where a "day" starts when aggregating.
LOCAL_TZ       = os.getenv("LOCAL_TZ", "Africa/Lagos").strip() or "Africa/Lagos"
# Supabase free tier caps the database at 500 MB. At one bucket per minute
# that is comfortable for years, but an unbounded table is still a wall you
# hit without warning, so we prune on a slow cadence. Set to 0 to keep
# everything.
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "365"))

_pool: Optional["asyncpg.Pool"] = None

# asyncio keeps only a WEAK reference to tasks created with create_task(), so a
# fire-and-forget task can be garbage-collected mid-flight and silently vanish.
# Holding a strong reference until it completes is the documented workaround.
_tasks: set = set()


def enabled() -> bool:
    """True when a pool is up. Callers use this to skip work entirely."""
    return _pool is not None


def _schema_path() -> str:
    return os.path.join(os.path.dirname(__file__), "schema.sql")


async def init_db() -> None:
    """
    Open the pool and apply the schema. Never raises: a failure here disables
    history and leaves the rest of the backend untouched.
    """
    global _pool

    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — history recording disabled")
        return
    if asyncpg is None:
        log.warning("asyncpg not installed — history recording disabled")
        return

    try:
        _pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=1,
            max_size=3,          # free-tier Postgres has few connection slots
            command_timeout=15,
            statement_cache_size=0,   # see module docstring (Supavisor)
        )
        with open(_schema_path(), "r", encoding="utf-8") as fh:
            ddl = fh.read()
        async with _pool.acquire() as con:
            # No arguments -> asyncpg uses the simple query protocol, which is
            # what lets a multi-statement DDL script run in one call.
            await con.execute(ddl)
        log.info("database ready (device_id=%s, tz=%s)", DEVICE_ID, LOCAL_TZ)
    except Exception as exc:
        _pool = None
        log.error("database init failed — history recording disabled: %s", exc)


async def close_db() -> None:
    global _pool
    if _pool is not None:
        try:
            await _pool.close()
        except Exception as exc:
            log.warning("pool close failed: %s", exc)
        _pool = None


# ── Query helpers ─────────────────────────────────────────────
# These DO raise. Read paths (the analytics router) want to know about a
# failure so they can return a truthful error instead of an empty chart.
# Write paths go through spawn(), which swallows.

async def fetch(sql: str, *args: Any) -> List["asyncpg.Record"]:
    if _pool is None:
        raise RuntimeError("database not configured")
    async with _pool.acquire() as con:
        return await con.fetch(sql, *args)


async def fetchrow(sql: str, *args: Any) -> Optional["asyncpg.Record"]:
    if _pool is None:
        raise RuntimeError("database not configured")
    async with _pool.acquire() as con:
        return await con.fetchrow(sql, *args)


async def execute(sql: str, *args: Any) -> None:
    if _pool is None:
        raise RuntimeError("database not configured")
    async with _pool.acquire() as con:
        await con.execute(sql, *args)


async def executemany(sql: str, rows: Iterable[tuple]) -> None:
    rows = list(rows)
    if not rows:
        return
    if _pool is None:
        raise RuntimeError("database not configured")
    async with _pool.acquire() as con:
        await con.executemany(sql, rows)


# ── Fire-and-forget ───────────────────────────────────────────
async def _guarded(coro) -> None:
    try:
        await coro
    except Exception as exc:
        # Logged, not raised. An unretrieved task exception would otherwise
        # print a bare traceback at GC time with no context about what failed.
        log.error("background database write failed: %s", exc)


def spawn(coro) -> None:
    """
    Run a coroutine detached from the request that created it.

    Used so the ESP32's POST /api/data returns as soon as the reading is in
    memory and broadcast, rather than after Supabase has acknowledged a write.
    """
    if _pool is None:
        coro.close()          # never awaited — close it or asyncio warns
        return
    try:
        task = asyncio.create_task(_guarded(coro))
    except RuntimeError:
        # No running loop (e.g. called from a sync context during shutdown).
        coro.close()
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ── Retention ─────────────────────────────────────────────────
async def prune() -> None:
    """Drop rows older than RETENTION_DAYS. No-op when retention is 0."""
    if RETENTION_DAYS <= 0 or _pool is None:
        return
    cutoff_sql = "now() - make_interval(days => $1)"
    async with _pool.acquire() as con:
        await con.execute(
            f"DELETE FROM samples WHERE device_id = $2 AND ts < {cutoff_sql}",
            RETENTION_DAYS, DEVICE_ID)
        await con.execute(
            f"DELETE FROM system_samples WHERE device_id = $2 AND ts < {cutoff_sql}",
            RETENTION_DAYS, DEVICE_ID)
        # Events and periods are tiny and are the most useful rows for the
        # write-up, so they are kept for twice as long.
        await con.execute(
            "DELETE FROM events WHERE device_id = $2 "
            "AND ts < now() - make_interval(days => $1)",
            RETENTION_DAYS * 2, DEVICE_ID)
    log.info("pruned history older than %d days", RETENTION_DAYS)
