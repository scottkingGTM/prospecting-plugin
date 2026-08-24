-- ============================================================
-- Prospecting Plugin — monitoring views
--
-- Purpose: the two operational rollups worth watching:
--   * prospector.v_usage  — per rep per day: what each rep did and
--     what it cost (cap tuning + "who is actually using this")
--   * prospector.v_health — per provider per day: hit rate, latency,
--     errors, and credits-per-found (vendor quality + cost drift)
--
-- Apply order: 01_create_schema.sql -> 02_create_role.sql
--              -> 03_views.sql -> 04_seed.sql
--
-- Idempotency: safe to apply twice — CREATE OR REPLACE VIEW throughout.
--
-- Day boundaries: day = UTC calendar day, matching the app's cap
-- accounting (the app's spend caps are per-UTC-day). Change both together
-- or cap tuning will mislead.
--
-- Access: admin only. prospector_service is deliberately NOT granted on
-- these views (02 grants tables by name, so these are excluded by
-- construction) — the app never reads its own rollups.
-- ============================================================

-- ------------------------------------------------------------
-- v_usage: one row per rep per active day.
-- Counting rules:
--   * action counts use status = 'done' — completed work, not attempts
--     (blocked_cap is the exception: every block is counted, since a
--     block never has a 'done').
--   * enrich job counts come from prospector.jobs, not events, because
--     jobs carry the reservation/billing numbers; a job requesting two
--     fields counts once per field column but once in enrich_jobs.
--   * credits_reserved vs credits_billed: a persistent gap means the
--     worst-case reservation model is over-holding reps' daily caps.
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW prospector.v_usage AS
WITH event_days AS (
    SELECT
        e.rep_id,
        (e.created_at AT TIME ZONE 'UTC')::date AS day,
        COUNT(*) FILTER (WHERE e.action = 'recognize'  AND e.status = 'done') AS recognitions,
        COUNT(*) FILTER (WHERE e.action = 'commit'     AND e.status = 'done') AS commits,
        COUNT(*) FILTER (WHERE e.action = 'promote_t2' AND e.status = 'done') AS promotes_t2,
        COUNT(*) FILTER (WHERE e.action = 'promote_t1' AND e.status = 'done') AS promotes_t1,
        COUNT(*) FILTER (WHERE e.action = 'blocked_cap')                      AS blocked_caps
    FROM prospector.events e
    GROUP BY e.rep_id, (e.created_at AT TIME ZONE 'UTC')::date
),
job_days AS (
    SELECT
        j.rep_id,
        (j.created_at AT TIME ZONE 'UTC')::date AS day,
        COUNT(*)                                                   AS enrich_jobs,
        COUNT(*) FILTER (WHERE 'work_email'     = ANY (j.fields))  AS enrich_jobs_work_email,
        COUNT(*) FILTER (WHERE 'mobile'         = ANY (j.fields))  AS enrich_jobs_mobile,
        COUNT(*) FILTER (WHERE 'personal_email' = ANY (j.fields))  AS enrich_jobs_personal_email,
        SUM(j.credits_reserved)                                    AS credits_reserved,
        SUM(j.credits_billed)                                      AS credits_billed
    FROM prospector.jobs j
    GROUP BY j.rep_id, (j.created_at AT TIME ZONE 'UTC')::date
)
SELECT
    r.id                                    AS rep_id,
    r.display_name,
    COALESCE(e.day, j.day)                  AS day,
    COALESCE(e.recognitions, 0)             AS recognitions,
    COALESCE(j.enrich_jobs, 0)              AS enrich_jobs,
    COALESCE(j.enrich_jobs_work_email, 0)   AS enrich_jobs_work_email,
    COALESCE(j.enrich_jobs_mobile, 0)       AS enrich_jobs_mobile,
    COALESCE(j.enrich_jobs_personal_email, 0) AS enrich_jobs_personal_email,
    COALESCE(j.credits_reserved, 0)         AS credits_reserved,
    COALESCE(j.credits_billed, 0)           AS credits_billed,
    COALESCE(e.commits, 0)                  AS commits,
    COALESCE(e.promotes_t2, 0)              AS promotes_t2,
    COALESCE(e.promotes_t1, 0)              AS promotes_t1,
    COALESCE(e.blocked_caps, 0)             AS blocked_caps
FROM event_days e
FULL OUTER JOIN job_days j
    ON j.rep_id = e.rep_id AND j.day = e.day
JOIN prospector.reps r
    ON r.id = COALESCE(e.rep_id, j.rep_id);

COMMENT ON VIEW prospector.v_usage IS
    'Per rep per day: actions completed, enrich jobs by field, credits reserved vs billed, cap blocks. Sources: prospector.events + prospector.jobs.';

-- ------------------------------------------------------------
-- v_health: one row per provider per day, from the attempts ledger.
--   * hit_rate = found / all attempts (rejected_* and errors count in
--     the denominator on purpose — a vendor whose hits keep failing
--     validation should LOOK unhealthy here)
--   * credits_per_found = total spend that day / hits: the real unit
--     cost including everything paid for misses
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW prospector.v_health AS
SELECT
    a.provider_id,
    (a.created_at AT TIME ZONE 'UTC')::date                                      AS day,
    COUNT(*)                                                AS attempts,
    COUNT(*) FILTER (WHERE a.status = 'found')              AS hits,
    ROUND(
        COUNT(*) FILTER (WHERE a.status = 'found')::numeric
        / NULLIF(COUNT(*), 0),
        3)                                                  AS hit_rate,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY a.latency_ms) AS median_latency_ms,
    COUNT(*) FILTER (WHERE a.status = 'error')              AS error_count,
    ROUND(
        SUM(a.cost_credits)
        / NULLIF(COUNT(*) FILTER (WHERE a.status = 'found'), 0),
        3)                                                  AS credits_per_found
FROM prospector.attempts a
GROUP BY a.provider_id, (a.created_at AT TIME ZONE 'UTC')::date;

COMMENT ON VIEW prospector.v_health IS
    'Per provider per day: attempts, hits, hit_rate, median latency, errors, credits_per_found. Source: prospector.attempts (served by the (provider_id, created_at) index).';
