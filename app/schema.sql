-- ============================================================
-- EnergyGuard — history schema (PostgreSQL / Supabase)
-- ============================================================
-- Applied on every startup. Every statement is idempotent, so this doubles
-- as the migration path: adding a column here and redeploying is enough.
--
-- SAMPLING RATE
-- ─────────────
-- The ESP32 posts every 2 s. Writing every post would be 43,200 rows/day and
-- 1.3 M rows/month — it would fill the 500 MB free tier inside a year and buy
-- nothing, because nobody analyses household load at 2-second resolution.
-- The live 60-point chart is already served from the in-memory ring buffer in
-- store.py, which is the right structure for that job.
--
-- So the database gets one BUCKET per minute instead: ~30 readings folded
-- into a mean, a peak and a count. 4,320 rows/day. The count column (n) is
-- kept so a thin bucket — a minute during which the device was mostly offline
-- — is visibly thin rather than silently equal in weight to a full one.


-- ── Per-channel minute buckets ──────────────────────────────
CREATE TABLE IF NOT EXISTS samples (
    ts          TIMESTAMPTZ NOT NULL,   -- bucket start, UTC, server clock
    device_id   TEXT        NOT NULL,
    ch          SMALLINT    NOT NULL,   -- 0=Lighting 1=Fan/TV 2=A/C 3=Refrigeration
    name        TEXT        NOT NULL,   -- label as the firmware reported it
    -- Shed RANK as the firmware reported it: 1 = shed last, 4 = shed first,
    -- and distinct across the four channels of any one bucket.
    --
    -- ⚠ SEMANTICS CHANGED WITHOUT THE COLUMN CHANGING. Rows written before
    -- shed priority became user-editable hold the old three-valued enum
    -- (1=HIGH 2=MEDIUM 3=LOW); rows after hold a rank. Both are small
    -- integers, so no migration was needed and none is possible — nothing in
    -- the data marks the boundary. Any query spanning it needs the cutover
    -- timestamp supplied by hand.
    prio        SMALLINT    NOT NULL,
    watts_avg   REAL        NOT NULL,
    watts_peak  REAL        NOT NULL,
    amps_avg    REAL        NOT NULL,
    -- Cumulative kWh as the meter reported it, NOT a per-bucket delta.
    -- Storing the running total means a dropped bucket costs one interval of
    -- resolution instead of permanently losing that energy from every future
    -- sum. Deltas are taken at query time with LAG().
    kwh_cum     REAL        NOT NULL,
    on_s        REAL        NOT NULL,   -- seconds energised within the bucket
    shed_s      REAL        NOT NULL,   -- seconds auto-shed within the bucket
    n           SMALLINT    NOT NULL,   -- readings folded into this bucket
    PRIMARY KEY (device_id, ts, ch)
);

CREATE INDEX IF NOT EXISTS samples_ts_idx ON samples (device_id, ts);


-- ── System-wide minute buckets ──────────────────────────────
CREATE TABLE IF NOT EXISTS system_samples (
    ts          TIMESTAMPTZ NOT NULL,
    device_id   TEXT        NOT NULL,
    volts_avg   REAL,
    watts_avg   REAL,
    watts_peak  REAL,
    kwh_cum     REAL,       -- device 'te' — total energy this period
    cost_ngn    REAL,       -- device 'tc' — the device's own tariff maths
    quota_kwh   REAL,       -- device 'q'  — this period's credit
    quota_rem   REAL,       -- device 'qr'
    sustain_w   REAL,       -- device 'sr' — predictor's sustainable rate
    -- NULL, not 9999. The firmware sends 9999 as "no meaningful projection"
    -- when average power is too low to divide by; writing that number into a
    -- REAL column would poison every AVG() and MAX() taken over it.
    eta_h       REAL,
    shed_s      REAL,       -- seconds the system was shedding within the bucket
    auto_shed   SMALLINT,
    clock_ok    SMALLINT,
    n           SMALLINT    NOT NULL,
    PRIMARY KEY (device_id, ts)
);


-- ── Discrete events ─────────────────────────────────────────
-- Written on transitions only, so this table stays small while carrying most
-- of the analytical value: how often shedding fires, which channel absorbs
-- it, how long it lasts, and what the user did in response.
CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL   PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    device_id   TEXT        NOT NULL,
    -- 'device' = observed in a reading; 'user' = a command was accepted by the
    -- API. Both are kept because they answer different questions, and because
    -- a user command that never produced a matching device event is itself a
    -- finding.
    source      TEXT        NOT NULL,
    kind        TEXT        NOT NULL,
    ch          SMALLINT,             -- NULL for system-wide events
    value       REAL,                 -- kWh, hours, etc. where meaningful
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS events_ts_idx ON events (device_id, ts DESC);


-- ── Quota periods ───────────────────────────────────────────
-- One row per budgeting period, closed when the firmware rolls over.
CREATE TABLE IF NOT EXISTS periods (
    id              BIGSERIAL   PRIMARY KEY,
    device_id       TEXT        NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    -- FALSE when the backend came up mid-period and inferred a start rather
    -- than witnessing the rollover. Such a row has a truncated energy figure,
    -- so the adherence statistic excludes it instead of quietly averaging a
    -- number it cannot stand behind.
    witnessed_start BOOLEAN     NOT NULL DEFAULT FALSE,
    target_hours    REAL,
    quota_kwh       REAL,       -- credit at close, including any top-ups
    topup_kwh       REAL        NOT NULL DEFAULT 0,
    energy_kwh      REAL,
    cost_ngn        REAL,
    auto_renew      BOOLEAN,
    within_quota    BOOLEAN
);

CREATE INDEX IF NOT EXISTS periods_open_idx
    ON periods (device_id, started_at DESC);
