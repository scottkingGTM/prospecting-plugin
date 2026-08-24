-- ============================================================
-- Prospecting Plugin — schema + tables
--
-- Purpose: create the `prospector` schema — the state store for the
-- rep-facing prospecting backend: rep allowlist + caps, provider/waterfall
-- config, the enrichment job queue (with the one-in-flight-per-profile
-- lock), the per-provider attempt ledger, the append-only rep event log,
-- and the recognize cache.
--
-- Apply order: 01_create_schema.sql -> 02_create_role.sql
--              -> 03_views.sql -> 04_seed.sql
--
-- Idempotency: safe to apply twice. Every CREATE uses IF NOT EXISTS;
-- COMMENT ON simply overwrites. Re-running changes nothing on a live DB.
--
-- Secrets: NONE live here. Provider API keys stay in the app's env;
-- rep auth is a sha256 hash of a token that is generated and hashed
-- out-of-band (see reps.token_hash comment + 04_seed.sql template).
--
-- RLS note: these tables are new and are accessed ONLY by the
-- prospector_service role created in 02_create_role.sql. No RLS is
-- enabled here on purpose — the schema is not exposed to PostgREST/anon
-- and no reader roles are granted into it. If this schema is ever added
-- to the API-exposed list, enable RLS first (the reporting-schema
-- convention: every exposed table needs a policy or it leaks).
-- ============================================================

CREATE SCHEMA IF NOT EXISTS prospector;

-- ------------------------------------------------------------
-- reps: the allowlist. A rep who is not in this table (or has
-- active = false) cannot authenticate at all. Also carries the
-- per-day spend/action caps enforced by the app.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prospector.reps (
    id                  smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email               text NOT NULL UNIQUE,
    display_name        text NOT NULL,
    hubspot_owner_id    text NOT NULL,
    token_hash          text NOT NULL,
    active              boolean NOT NULL DEFAULT true,
    daily_credit_cap    numeric NOT NULL DEFAULT 50,
    daily_promote_cap   int NOT NULL DEFAULT 25,
    daily_t1_cap        int NOT NULL DEFAULT 3,
    created_at          timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  prospector.reps IS
    'Rep allowlist + auth + daily caps. Not in this table (or active=false) = no access, period.';
COMMENT ON COLUMN prospector.reps.token_hash IS
    'sha256 hex of a >=32-char bearer token. The token itself is generated out-of-band and handed to the rep; only the hash is stored, so a DB read never yields a usable credential.';
COMMENT ON COLUMN prospector.reps.hubspot_owner_id IS
    'HubSpot owner id used when committing contacts/promotions so ownership lands on the right rep.';
COMMENT ON COLUMN prospector.reps.daily_credit_cap IS
    'Max enrichment credits a rep may spend per calendar day (reserved counts against it, not just billed) — the blast-radius limiter.';
COMMENT ON COLUMN prospector.reps.daily_promote_cap IS
    'Max Tier-2 promotions per day.';
COMMENT ON COLUMN prospector.reps.daily_t1_cap IS
    'Max Tier-1 promotions per day — deliberately small; Tier 1 is your highest-touch tier.';

-- ------------------------------------------------------------
-- providers: which enrichment/inference vendors exist and whether
-- they are on. Config knobs only — never API keys.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prospector.providers (
    id       text PRIMARY KEY,
    kind     text NOT NULL CHECK (kind IN ('lookup', 'inference')),
    enabled  boolean NOT NULL DEFAULT true,
    config   jsonb NOT NULL DEFAULT '{}'
);

COMMENT ON TABLE  prospector.providers IS
    'Enrichment providers by id (e.g. ''fullenrich''). Externalized so enabling/disabling a vendor is an UPDATE, not a deploy.';
COMMENT ON COLUMN prospector.providers.kind IS
    '''lookup'' = queries a real data source; ''inference'' = model-generated (held to stricter rules — an inference provider must never write an email as identity).';
COMMENT ON COLUMN prospector.providers.config IS
    'Non-secret knobs only (feature flags, thresholds). API keys stay in the app''s env, never in this table.';

-- ------------------------------------------------------------
-- waterfalls: ordered provider chain per field. The app walks
-- positions in order and stops when stop_on is satisfied.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prospector.waterfalls (
    field        text NOT NULL CHECK (field IN ('work_email', 'mobile', 'personal_email')),
    position     int NOT NULL,
    provider_id  text NOT NULL REFERENCES prospector.providers (id),
    stop_on      text NOT NULL DEFAULT 'verified',
    max_cost     numeric NOT NULL,
    enabled      boolean NOT NULL DEFAULT true,
    PRIMARY KEY (field, position)
);

COMMENT ON TABLE  prospector.waterfalls IS
    'Per-field provider order. Waterfall shape lives in data, not code, so re-ordering or capping a leg is an UPDATE.';
COMMENT ON COLUMN prospector.waterfalls.stop_on IS
    'Result quality that ends the walk for this field (default ''verified'') — a weaker hit falls through to the next position.';
COMMENT ON COLUMN prospector.waterfalls.max_cost IS
    'Max credits this leg may bill for one profile; the app skips the leg rather than exceed it.';

-- ------------------------------------------------------------
-- jobs: the enrichment queue. One row per requested enrichment
-- of one LinkedIn profile.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prospector.jobs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rep_id            smallint NOT NULL REFERENCES prospector.reps (id),
    kind              text NOT NULL DEFAULT 'enrich',
    norm_linkedin_url text NOT NULL,
    fields            text[] NOT NULL,
    state             text NOT NULL DEFAULT 'queued'
                      CHECK (state IN ('queued', 'running', 'done', 'failed', 'expired')),
    input             jsonb NOT NULL,
    result            jsonb,
    credits_reserved  numeric NOT NULL DEFAULT 0,
    credits_billed    numeric NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz
);

COMMENT ON TABLE  prospector.jobs IS
    'Enrichment job queue: one row per profile enrichment request, carrying reservation vs actual spend.';
COMMENT ON COLUMN prospector.jobs.norm_linkedin_url IS
    'Normalized LinkedIn profile URL — the dedupe identity. Normalization happens in the app BEFORE insert so the in-flight unique index can actually bite.';
COMMENT ON COLUMN prospector.jobs.credits_reserved IS
    'Credits held against the rep''s daily cap at enqueue time (worst case for the requested fields).';
COMMENT ON COLUMN prospector.jobs.credits_billed IS
    'What the providers actually charged; the difference is released back to the cap at finish.';

-- THE in-flight lock (two reps, one enrichment): a partial unique index
-- on the normalized profile URL while a job is queued/running means the
-- second rep's insert fails — the app turns that into a 409 that carries
-- the LIVE job id, so rep #2 attaches to rep #1's job instead of paying
-- for a second enrichment of the same person.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_inflight_one_per_profile
    ON prospector.jobs (norm_linkedin_url)
    WHERE state IN ('queued', 'running');

COMMENT ON INDEX prospector.jobs_inflight_one_per_profile IS
    'One in-flight job per profile: the second rep gets a 409 + the live job id, not a second bill.';

CREATE INDEX IF NOT EXISTS jobs_rep_created
    ON prospector.jobs (rep_id, created_at);

-- Reuse-done-results lookups: "has this profile been enriched before?"
-- The partial unique index above only covers in-flight states
-- (queued/running), so finished jobs need their own index.
CREATE INDEX IF NOT EXISTS jobs_by_profile
    ON prospector.jobs (norm_linkedin_url);

-- ------------------------------------------------------------
-- attempts: one row per provider call inside a job — the raw
-- ledger v_health is computed from.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prospector.attempts (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id        uuid NOT NULL REFERENCES prospector.jobs (id),
    provider_id   text NOT NULL,
    field         text NOT NULL,
    status        text NOT NULL,
    cost_credits  numeric NOT NULL DEFAULT 0,
    latency_ms    int,
    dnc_flag      boolean,
    raw_response  jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  prospector.attempts IS
    'Per-provider-call ledger: every waterfall leg fired for a job, hit or miss, with cost and latency.';
COMMENT ON COLUMN prospector.attempts.provider_id IS
    'Provider that fired this leg. FK to prospector.providers deliberately omitted — the attempt ledger must survive a provider row being deleted; v_health tolerates orphan ids.';
COMMENT ON COLUMN prospector.attempts.status IS
    'found | not_found | rejected_* | error. rejected_* statuses record a hit the app refused (e.g. failed validation) so hit_rate stays honest.';
COMMENT ON COLUMN prospector.attempts.dnc_flag IS
    'Do-not-call flag from the provider. Retained for compliance filtering; NEVER surfaced to reps.';
COMMENT ON COLUMN prospector.attempts.raw_response IS
    'Full provider payload, kept for dispute/debug — the parsed result lives on jobs.result.';

CREATE INDEX IF NOT EXISTS attempts_job
    ON prospector.attempts (job_id);

-- (provider_id, created_at) serves v_health's per-provider-per-day rollup.
CREATE INDEX IF NOT EXISTS attempts_provider_created
    ON prospector.attempts (provider_id, created_at);

-- ------------------------------------------------------------
-- events: append-only log of every rep-visible action — the
-- audit trail, the cap counter source, and the idempotency store.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prospector.events (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rep_id           smallint NOT NULL REFERENCES prospector.reps (id),
    action           text NOT NULL,
    status           text NOT NULL
                     CHECK (status IN ('attempt', 'done', 'failed', 'rejected')),
    target           jsonb NOT NULL,
    idempotency_key  text,
    reason           text,
    cost_credits     numeric,
    dry_run          boolean NOT NULL,
    detail           jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  prospector.events IS
    'Append-only rep action log. Feeds v_usage, enforces caps, and backs write idempotency.';
COMMENT ON COLUMN prospector.events.action IS
    'recognize | enrich | resolve | commit | promote_t2 | promote_t1 | blocked_cap | guard_hold | merge_candidate | record_view. No CHECK by design — new actions may be added; consumers must never depend on unlisted values.';
COMMENT ON COLUMN prospector.events.status IS
    'attempt | done | failed | rejected';
COMMENT ON COLUMN prospector.events.target IS
    'What the action pointed at (profile URL, hs_contact_id, company id...) — jsonb because targets differ by action.';
COMMENT ON COLUMN prospector.events.dry_run IS
    'NOT NULL on purpose: every event must declare whether it touched the real world, so audits never guess.';

-- Idempotency: at most ONE completed LIVE (status='done', NOT dry_run)
-- event per (rep, action, key). A retried write with the same key hits
-- this index and the app returns the original outcome instead of
-- double-committing. Partial on status='done' so failed attempts don't
-- burn the key, and on NOT dry_run (from a hardening pass) so a
-- dry-run rehearsal never satisfies -- or uniquely blocks -- a LIVE
-- commit's replay: the dry-run-confirm -> flip-live -> re-confirm-the-
-- same-flow sequence must WRITE, not replay the rehearsal report.
CREATE UNIQUE INDEX IF NOT EXISTS events_idem
    ON prospector.events (rep_id, action, idempotency_key)
    WHERE status = 'done' AND idempotency_key IS NOT NULL AND NOT dry_run;

COMMENT ON INDEX prospector.events_idem IS
    'One LIVE done event per (rep, action, idempotency_key): a retry returns the original outcome instead of committing twice; dry-run rehearsals never satisfy a live replay.';

CREATE INDEX IF NOT EXISTS events_rep_created
    ON prospector.events (rep_id, created_at);

-- ------------------------------------------------------------
-- recognize_cache: short-TTL cache for recognize lookups so
-- repeated hits on the same profile/domain cost nothing.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prospector.recognize_cache (
    cache_key   text PRIMARY KEY,
    payload     jsonb NOT NULL,
    expires_at  timestamptz NOT NULL
);

COMMENT ON TABLE  prospector.recognize_cache IS
    'Recognize-result cache. The service role gets DELETE here (and only here) so it can evict expired rows.';
COMMENT ON COLUMN prospector.recognize_cache.cache_key IS
    '''li:''||norm_linkedin for profile lookups, ''dom:''||registered_domain for company lookups — prefixed so the two key spaces can never collide.';

-- Serves eviction sweeps (DELETE ... WHERE expires_at < now()).
CREATE INDEX IF NOT EXISTS recognize_cache_expires
    ON prospector.recognize_cache (expires_at);
