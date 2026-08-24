-- ============================================================
-- Prospecting Plugin — seed data
--
-- Purpose: initial config rows — the enrichment provider(s) and the
-- per-field waterfalls — plus a commented template for the `reps` table,
-- which is deliberately NOT seeded here (every rep is added by hand).
--
-- Apply order: 01_create_schema.sql -> 02_create_role.sql
--              -> 03_views.sql -> 04_seed.sql
--
-- Idempotency: safe to apply twice — every INSERT is ON CONFLICT DO
-- NOTHING, so a re-run never duplicates rows AND never overwrites config
-- that has since been tuned live (an intentional choice: re-seeding must
-- not silently revert an UPDATE you made).
--
-- Secrets: none. Rep tokens are hashed out-of-band (template below);
-- provider API keys live in the app's env, never in providers.config.
-- ============================================================

-- ------------------------------------------------------------
-- 1. Providers. The bundled reference adapter is 'fullenrich'
--    (prospector/providers/fullenrich.py). To add your own vendor:
--    write an adapter, register it in build_registry(), then INSERT a
--    row here — see PROVIDERS.md.
--
--    'kind' is 'lookup' (queries a real data source) or 'inference'
--    (model-generated). An inference provider is held to stricter rules
--    and must never be allowed to write a guessed email as someone's
--    identity — pattern-guessed addresses land on the wrong company.
-- ------------------------------------------------------------
INSERT INTO prospector.providers (id, kind, enabled, config) VALUES
    ('fullenrich', 'lookup', true, '{}')
ON CONFLICT (id) DO NOTHING;

-- Example of adding a second, disabled provider (uncomment + adapt once
-- you have written and registered its adapter):
--
-- INSERT INTO prospector.providers (id, kind, enabled, config) VALUES
--     ('your_vendor', 'lookup', false, '{}')
-- ON CONFLICT (id) DO NOTHING;

-- ------------------------------------------------------------
-- 2. Waterfalls: ordered provider chain per field. Single-leg
--    FullEnrich chains to start; append higher-position legs (a second
--    vendor as a fallback) as you add providers. max_cost is in credits
--    and mirrors the vendor's pricing — the walk skips a leg rather than
--    exceed it.
-- ------------------------------------------------------------
INSERT INTO prospector.waterfalls (field, position, provider_id, stop_on, max_cost, enabled) VALUES
    ('work_email',     1, 'fullenrich', 'verified',  1, true),
    ('personal_email', 1, 'fullenrich', 'verified',  3, true),
    ('mobile',         1, 'fullenrich', 'verified', 10, true)
ON CONFLICT (field, position) DO NOTHING;

-- ------------------------------------------------------------
-- 3. Reps: NO seed rows on purpose — every rep is added by hand so the
--    allowlist never contains anyone by default.
--
--    How to add a rep (or use scripts/add_rep.py, which does a+b for you):
--      a. Generate a token, >= 32 chars:
--           python3 -c "import secrets; print(secrets.token_urlsafe(32))"
--         Hand the TOKEN to the rep (they paste it into the extension).
--      b. Hash it locally — only the hash goes in the DB:
--           printf '%s' '<token>' | shasum -a 256
--      c. hubspot_owner_id comes from HubSpot (Settings -> Users, or the
--         owners API); newly-created contacts are assigned to this owner.
--
-- INSERT INTO prospector.reps
--     (email, display_name, hubspot_owner_id, token_hash)
-- VALUES
--     ('alice@example.com', 'Alice', '123456789',
--      '<64-char sha256 hex of the token — never the token itself>')
-- ON CONFLICT (email) DO NOTHING;
-- ------------------------------------------------------------
