-- ============================================================
-- Prospecting Plugin — Supabase/Postgres access role
--
-- Purpose: give the app a least-privilege way into the database:
--   * Working access (SELECT, INSERT, UPDATE) to the `prospector`
--     schema's WORKING tables (jobs, attempts, events); the CONFIG
--     tables (reps, providers, waterfalls) are SELECT-only — the service
--     reads its guardrails, never rewrites them.
--   * DELETE additionally on prospector.recognize_cache ONLY (cache
--     eviction); nothing else in the schema is ever deleted — jobs,
--     attempts, and events are append/update-only history.
--   * NOTHING outside the `prospector` schema. All CRM writes go through
--     the HubSpot API in app code, never through this role.
--
-- Apply order: 01_create_schema.sql -> 02_create_role.sql
--              -> 03_views.sql -> 04_seed.sql
--   (This file grants on the tables 01 creates. The monitoring views in
--   03 are deliberately NOT granted here — they are for admin use; the
--   service never reads its own rollups.)
--
-- Idempotency: safe to apply twice. CREATE ROLE is guarded in a DO
-- block; GRANTs are naturally re-runnable.
--
-- Password: the role is created WITHOUT a password on purpose, so it
-- cannot authenticate until one manual step (run as an admin/superuser):
--   1. Generate a strong password (a password manager, or:
--        python3 -c "import secrets; print(secrets.token_urlsafe(32))"
--      ).
--   2. ALTER ROLE prospector_service WITH PASSWORD '<that password>';
--   3. Put it in PROSPECTOR_DATABASE_URL in this app's .env, e.g. (Supabase
--      session pooler; the direct host is IPv6-only):
--        postgresql://prospector_service.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
--   * Never commit a filled-in password anywhere, this file included.
--
-- RLS note: the prospector tables carry no RLS (the schema is not
-- API-exposed and only this role is granted in), so plain GRANTs are
-- sufficient. If RLS is ever enabled on these tables, this role will
-- silently see ZERO rows until policies are added.
-- ============================================================

-- 1. Create the role, idempotently. No PASSWORD here on purpose: it is
--    set out-of-band via ALTER ROLE (see header) so it never lands in a
--    committed file or a migration record.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'prospector_service') THEN
        CREATE ROLE prospector_service LOGIN
          NOSUPERUSER NOCREATEDB NOCREATEROLE;
    ELSE
        RAISE NOTICE 'role prospector_service already exists, skipping CREATE ROLE';
    END IF;
END
$$;

-- 2. Allow it to connect to the database.
GRANT CONNECT ON DATABASE postgres TO prospector_service;

-- 3. Its own schema: usage + working access on each table, named
--    explicitly (NOT "ALL TABLES IN SCHEMA") so a future view or table
--    added to the schema is not granted by accident.
GRANT USAGE ON SCHEMA prospector TO prospector_service;

-- Config tables: SELECT ONLY. The service must not be able to rewrite its
-- own guardrails — a leaked token could otherwise mint reps or raise its
-- own spend caps. Rep/provider/waterfall changes are made by an admin,
-- never by the app. The REVOKEs make re-applying this file converge a DB
-- that was provisioned under older, wider grants.
GRANT SELECT ON prospector.reps       TO prospector_service;
GRANT SELECT ON prospector.providers  TO prospector_service;
GRANT SELECT ON prospector.waterfalls TO prospector_service;
REVOKE INSERT, UPDATE ON prospector.reps       FROM prospector_service;
REVOKE INSERT, UPDATE ON prospector.providers  FROM prospector_service;
REVOKE INSERT, UPDATE ON prospector.waterfalls FROM prospector_service;

-- Working tables: full S/I/U (append/update-only history; no DELETE).
GRANT SELECT, INSERT, UPDATE ON prospector.jobs            TO prospector_service;
GRANT SELECT, INSERT, UPDATE ON prospector.attempts        TO prospector_service;
GRANT SELECT, INSERT, UPDATE ON prospector.events          TO prospector_service;
GRANT SELECT, INSERT, UPDATE ON prospector.recognize_cache TO prospector_service;

-- 3b. DELETE on the cache ONLY — eviction of expired recognize rows.
--     Every other table is history and stays delete-proof at the DB
--     level, not just by app convention.
GRANT DELETE ON prospector.recognize_cache TO prospector_service;

-- 3c. Identity sequences (attempts, events use GENERATED ALWAYS AS
--     IDENTITY; INSERT needs sequence USAGE). Granted per-sequence via
--     pg_get_serial_sequence — a sequence added to the schema later is not
--     granted by accident.
DO $$
DECLARE
    tbl text;
    seq text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['attempts', 'events'] LOOP
        seq := pg_get_serial_sequence('prospector.' || tbl, 'id');
        IF seq IS NULL THEN
            RAISE EXCEPTION 'no identity sequence found for prospector.%.id', tbl;
        END IF;
        EXECUTE format('GRANT USAGE ON SEQUENCE %s TO prospector_service', seq);
    END LOOP;
END
$$;

-- ============================================================
-- 4. Verify — every can_* column should be TRUE, and every
--    *_MUST_BE_FALSE column should be exactly that: FALSE.
--    (Supabase's SQL Editor runs as a restricted admin that cannot
--    SET ROLE into other accounts, so we check privileges directly
--    instead of impersonating the role.)
--    NOTE on has_table_privilege semantics: with a comma-separated
--    list it returns TRUE if ANY of the listed privileges is held —
--    right for the negatives, too weak for the positives, so the
--    can_use_* checks AND one call per privilege instead.
-- ============================================================
SELECT
  -- config tables: SELECT-only (read the guardrails, never rewrite them)
  has_table_privilege('prospector_service', 'prospector.reps',       'SELECT') AS can_read_reps,
  has_table_privilege('prospector_service', 'prospector.providers',  'SELECT') AS can_read_providers,
  has_table_privilege('prospector_service', 'prospector.waterfalls', 'SELECT') AS can_read_waterfalls,

  -- working tables: working access present (ALL of S/I/U)
  (has_table_privilege('prospector_service', 'prospector.jobs', 'SELECT')
     AND has_table_privilege('prospector_service', 'prospector.jobs', 'INSERT')
     AND has_table_privilege('prospector_service', 'prospector.jobs', 'UPDATE'))            AS can_use_jobs,
  (has_table_privilege('prospector_service', 'prospector.attempts', 'SELECT')
     AND has_table_privilege('prospector_service', 'prospector.attempts', 'INSERT')
     AND has_table_privilege('prospector_service', 'prospector.attempts', 'UPDATE'))        AS can_use_attempts,
  (has_table_privilege('prospector_service', 'prospector.events', 'SELECT')
     AND has_table_privilege('prospector_service', 'prospector.events', 'INSERT')
     AND has_table_privilege('prospector_service', 'prospector.events', 'UPDATE'))          AS can_use_events,
  (has_table_privilege('prospector_service', 'prospector.recognize_cache', 'SELECT')
     AND has_table_privilege('prospector_service', 'prospector.recognize_cache', 'INSERT')
     AND has_table_privilege('prospector_service', 'prospector.recognize_cache', 'UPDATE')
     AND has_table_privilege('prospector_service', 'prospector.recognize_cache', 'DELETE')) AS can_use_cache_incl_delete,

  -- config tables stay read-only: no minting reps or raising caps with a
  -- leaked service token
  has_table_privilege('prospector_service', 'prospector.reps',       'INSERT') AS can_insert_reps_MUST_BE_FALSE,
  has_table_privilege('prospector_service', 'prospector.reps',       'UPDATE') AS can_update_reps_MUST_BE_FALSE,
  has_table_privilege('prospector_service', 'prospector.waterfalls', 'UPDATE') AS can_update_waterfalls_MUST_BE_FALSE,

  -- delete stays cache-only
  has_table_privilege('prospector_service', 'prospector.events',   'DELETE') AS can_delete_events_MUST_BE_FALSE,
  has_table_privilege('prospector_service', 'prospector.jobs',     'DELETE') AS can_delete_jobs_MUST_BE_FALSE,
  has_table_privilege('prospector_service', 'prospector.attempts', 'DELETE') AS can_delete_attempts_MUST_BE_FALSE;
