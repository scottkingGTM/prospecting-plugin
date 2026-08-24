# Security overview

This document describes the security posture of the Prospecting Plugin. Every
claim here is verifiable in this repository (code, SQL migrations, tests).

## 1. Components and trust boundary

Two pieces:

- **Chrome extension** (Manifest V3 side panel), installed per rep. It is a
  **thin client**: it renders UI, holds the rep's own bearer token, and makes
  requests ONLY to your backend (an exact-origin allowlist is enforced in the
  extension *and* server-side). No business logic, no third-party calls, no
  content scripts, no host permissions for any site the rep browses.
- **Backend** (Python, stdlib HTTP server) in a container. It holds all logic
  and all secrets (HubSpot token, enrichment key, DB password). Its state
  lives in a dedicated `prospector` schema in Postgres. It runs as a non-root
  user and exposes a `/healthz` liveness endpoint.

Secrets live only on the backend. The browser never sees the HubSpot token,
the enrichment key, or the database password.

## 2. The extension reads almost nothing

- It reads the **active tab's URL and title** via the standard `tabs`
  permission — plain browser metadata, the same risk posture as the URL bar.
- **No content scripts.** It never reads page content, never touches the DOM
  of any site, and has **no host permissions for LinkedIn or any browsed
  site** — only for your backend origin(s).
- "Recognizing a profile" is the *backend* searching **your own HubSpot** (by
  the stored LinkedIn-URL property, or by company domain). The system never
  authenticates to, calls, or scrapes LinkedIn — there are no LinkedIn
  credentials anywhere.
- All rendering uses `createElement` + `textContent`. Server/CRM data is
  treated as untrusted; there is no `innerHTML` with server data anywhere in
  the panel.
- The rep token lives only in the service worker and is attached to requests
  in exactly one place. It is never written into the page, never put in a URL,
  and the service worker hard-refuses to attach it to any origin off the
  allowlist (defense in depth: MV3 `host_permissions` alone does not stop a
  fetch from sending an `Authorization` header to an arbitrary origin).

## 3. Credentials and secrets

| Credential | Scope | Storage |
|---|---|---|
| HubSpot private-app token | contacts + companies read/write, deals read-only — nothing else | Backend env var; local `.env` is gitignored; the repo carries only `.env.example` placeholders |
| Enrichment vendor API key | the enrichment vendor only | Backend env var, same handling |
| Rep bearer tokens | one per rep, ≥32 chars | Only SHA-256 hashes are stored (`prospector.reps.token_hash`); comparison is constant-time across the whole roster; a token in a URL is rejected with 400 before auth; request logging never records query strings |
| Postgres role `prospector_service` | least-privilege: read-only on its own config tables (it cannot raise its own caps or mint reps), no DELETE on history tables, nothing outside its schema | Password set out of band, never in a file or migration; privileges are verified at every boot, and over-privilege is a deploy blocker |

## 4. Authentication

- Per-rep bearer tokens; the token **is** the identity (no sessions/cookies).
- Only SHA-256 digests are stored, so a database dump leaks no usable
  credential.
- Comparison is **constant-time across the entire roster** — the token is
  hashed once and checked against every rep with `secrets.compare_digest`,
  with no early exit, so response time reveals nothing about the roster.
- A missing/bad token gets a uniform 401 after a small fixed delay, with no
  `WWW-Authenticate` header — an unauthenticated caller learns only that it
  was rejected.
- Deactivating a rep (`active = false`) removes access within the cache TTL;
  auth fails **closed** if the database is unreachable beyond a short window.

## 5. Writes are guarded and audited

Anything that spends a credit or writes to HubSpot requires an explicit rep
click and goes through a mandatory **resolve → preview → confirm** pipeline.
The commit path is the only code that writes to HubSpot, and it enforces:

- **Audit-before-action.** An `attempt` row is written to the append-only
  `prospector.events` log *before* any HubSpot write runs; if that insert
  fails, the write never happens. The outcome (`done`/`failed`/`rejected`) is
  a second immutable row. Idempotency keys make a retried write return the
  original outcome instead of double-writing.
- **Dedupe / identity guards** (`prospector/guards.py`), each blocking a real
  CRM-hygiene failure mode:
  - a contact whose email domain doesn't match its company (job-changer / wrong-company enrichment) — a hold for rep judgment;
  - an email with no company to check against (which would let HubSpot's
    auto-create mint a junk company) — blocked;
  - a **model-inferred** email — never committable as identity;
  - a consumer-provider email (gmail/yahoo/…) offered as a work email — flagged;
  - overwriting a *different* existing LinkedIn URL on a contact — blocked.
- **Server-side daily spend caps** per rep (enrichment credits, record
  creations, tier-1 promotions). Cap rejections are themselves logged.
- **Env-pinned dry-run.** `DRY_RUN` is decided once, at boot, from the
  environment. A request body can never enable live writes; flipping it
  requires a deployment-level change. In dry-run, a confirmed commit produces
  a full "what would happen" report and writes nothing.

Every vendor call lands one row in `prospector.attempts` (hit/miss/error,
cost, latency, raw payload) — the ledger you reconcile vendor invoices
against. Every created contact carries a provenance note on the HubSpot
record: who clicked, the source URL, which vendor returned what, at what cost.

## 6. Network hardening

- **CORS pinned to exact origin(s).** A browser request whose `Origin` isn't
  on the allowlist is refused server-side (403), not merely starved of CORS
  headers. Requests with no `Origin` (curl, server-to-server) fall through to
  bearer auth — CORS only ever constrains browsers.
- `/healthz` is public and returns liveness only (`{"ok": true}`) — no
  version, no counts, nothing to fingerprint.
- Request bodies are capped (16KB) and refused before being read; chunked
  bodies are rejected; the server never logs request query strings or the raw
  request line.

## 7. Triggers

Rep-action only. No schedules, no cron, no autonomous writes. Tab-change
recognition is read-only and free; anything that spends or writes requires an
explicit click.

## Before you publish / deploy

- Keep `.env`, any extension **signing key** (`*.pem`), and any packed build
  out of git (they're in `.gitignore`).
- Scope the HubSpot token to exactly contacts/companies read-write + deals
  read; nothing else.
- Run `python run.py --check` — it refuses to serve if the DB role is over- or
  under-privileged.
- Leave `DRY_RUN=true` until you've verified an end-to-end dry run.
