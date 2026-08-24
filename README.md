# Prospecting Plugin

A Chrome extension that turns enables a CRM lookup and Contact enrichment without leaving LinkedIn:

1. **Recognize** — is this person/company already in HubSpot? With possible-duplicate matches surfaced before you try to enrich.
2. **Enrich** — find a verified work email, personal email, or mobile on
   demand, through the **enrichment provider of choice** (ships with a
   [FullEnrich](https://fullenrich.com) reference adapter; swap in any vendor —
   see [PROVIDERS.md](PROVIDERS.md)).
3. **Add** — create the contact (and company) in HubSpot through a
   resolve → preview → confirm flow, with dedupe guards that stop the classic
   CRM-hygiene mistakes.

It's the "look someone up and add them correctly" workflow a sales rep does
dozens of times a day, without leaving the tab they're on — and without a
data vendor's own heavyweight extension.

> **Why this exists.** It's a self-hosted, auditable alternative to a
> commercial sales-intelligence browser extension: you own the backend, you
> choose the enrichment vendor, every credit spent and every record written
> is logged, and the browser extension is a deliberately thin, low-permission
> client.

---

## How it works

```
        ┌────────────────────────────────┐
        │  Chrome extension (MV3)        │   • reads ONLY the tab URL/title
        │  side panel                    │   • holds the rep token
        └───────────────┬────────────────┘   • no content scripts
                        │
                        │  HTTPS + bearer token
                        ▼
        ┌────────────────────────────────┐  API   ┌──────────────────────┐
        │  Backend (Python, stdlib)      │───────▶│  HubSpot (CRM)       │
        │                                │        └──────────────────────┘
        │  • recognize / enrich /        │
        │    resolve / commit            │  API   ┌──────────────────────┐
        │  • per-rep auth + spend caps   │───────▶│  Enrichment vendor   │
        │  • audit log + dedupe guards   │        │  (pluggable)         │
        └───────────────┬────────────────┘        └──────────────────────┘
                        │
                        ▼
        ┌────────────────────────────────┐
        │  Postgres                      │
        │  (its own `prospector` schema) │
        └────────────────────────────────┘
```

- The **extension** is a thin client. It reads only the **active tab's URL and
  title** (the standard `tabs` permission) — no content scripts, no page
  scraping, no LinkedIn host permissions. It renders UI and forwards clicks to
  the backend. The rep's token lives only in the service worker.
- The **backend** is a single-file-per-concern Python HTTP service (stdlib
  `ThreadingHTTPServer`, no web framework). It holds all the logic, secrets,
  and CRM/vendor credentials. It talks to HubSpot and to your enrichment
  vendor; the browser never sees those keys.
- **Postgres** stores the backend's own state in a dedicated `prospector`
  schema: the rep allowlist + spend caps, provider/waterfall config, the
  enrichment job queue, an append-only audit log, and a short-lived
  recognize cache. Every enrichment credit and every write is recorded.

"Recognizing a profile" means the backend searches **your own HubSpot** — it
never logs into or scrapes LinkedIn. See [SECURITY.md](SECURITY.md).

---

## What each surface shows

| You're viewing | The panel does |
|---|---|
| A LinkedIn profile (`/in/…`) | Recognizes the person against HubSpot (green/red). Offers priced enrichment buttons and, on a net-new person, an **Add to HubSpot** flow. |
| A company website | Recognizes the company by domain — shows matches, ICP tier, target-account flag. |
| A LinkedIn company page | Same company recognition. |
| Sales Navigator | Tells you to open the public profile (enrichment vendors can't use Sales Nav URLs). |
| Anything else | Idle. |

---

## Access it needs

This is the full list of what the system touches — nothing more.

**Chrome extension permissions** (`manifest.json`):

| Permission | Why |
|---|---|
| `sidePanel` | The whole UI is a side panel. |
| `tabs` | Read the active tab's URL + title to know what you're looking at. **This is the only thing it reads from your browsing** — no page content. |
| `storage` | Store your backend URL and rep token locally; cache results per URL for the session. |
| `notifications` | Fire a desktop notification when a background enrichment job finishes. |
| `alarms` | Keep polling an enrichment job even if the panel is closed. |
| `host_permissions` | Restricted to your backend origin(s) only — nothing else. |

There are **no content scripts** and **no host permissions for LinkedIn or any
site you browse**.

**HubSpot** — a private-app token scoped to exactly:

- Contacts: read + write
- Companies: read + write
- Deals: read-only

**Database** — a dedicated least-privilege Postgres role (`prospector_service`)
that can only touch its own `prospector` schema: read-only on its config
tables (so a leaked token can't raise its own spend caps), read/write on the
working tables, and delete only on the cache. It has no access to any other
schema or to your CRM base tables. Provisioned by
[`sql/02_create_role.sql`](sql/02_create_role.sql), verified at every boot.

**Enrichment vendor** — one API key for the vendor you choose. Used only when
a rep explicitly clicks a priced enrichment button.

---

## Setup

**Prerequisites:** Python 3.12+, a Postgres database (works great on
[Supabase](https://supabase.com)), a HubSpot private-app token, and (for
enrichment) an enrichment-vendor API key.

### 1. Backend

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill it in — see the comments in that file
```

Apply the SQL in order (Supabase SQL editor, `psql`, or your migration tool):

```
sql/01_create_schema.sql   # the prospector schema + tables
sql/02_create_role.sql     # the least-privilege role (+ a verification query)
sql/03_views.sql           # usage/health monitoring views (admin only)
sql/04_seed.sql            # provider + waterfall config
```

Set the role's password out of band (see the header of `02_create_role.sql`),
put it in `.env`, then validate everything before serving:

```bash
.venv/bin/python run.py --check    # validates config + DB privileges, exits
.venv/bin/python run.py --serve    # starts the HTTP server
```

`--check` fails loudly if the role is over- or under-privileged — a
misconfigured deploy never starts.

> **`DRY_RUN` is env-pinned to `true` by default.** Reps can click every
> button and rehearse the whole flow; nothing writes to HubSpot and no credits
> are spent until you deliberately set `DRY_RUN=false` in the deployment
> environment. A request body can never flip it.

Add a rep (generates a token to hand them + the SQL to store only its hash):

```bash
.venv/bin/python scripts/add_rep.py "Alice" alice@example.com <hubspot_owner_id>
```

### 2. Extension

1. `chrome://extensions` → enable **Developer mode** → **Load unpacked** →
   select the `extension/` folder.
2. Open the extension's **Options** and set the **Backend URL** and your
   **rep token**.
3. Pin it and click the icon to open the side panel.

The extension only talks to backend origins on a hard allowlist. Point it at
your own backend by editing the origin in three places that must stay in sync:
`extension/manifest.json` (`host_permissions`), `extension/background.js`
(`ALLOWED_BACKEND_ORIGINS`), and `extension/options.js`
(`ALLOWED_BACKEND_ORIGINS`). See [`extension/README.md`](extension/README.md).

### 3. Deploy (optional)

The included `Dockerfile` runs the backend as a non-root user on port 8080
with a `/healthz` endpoint — deploy it anywhere that runs a container. Set the
same env vars there, and set `EXTENSION_ORIGIN` to your published extension's
origin.

---

## Plug in any enrichment API

Enrichment is fully pluggable. The bundled FullEnrich adapter is a complete
reference; adding another vendor is: write an adapter class, register it with
one line, and turn it on in the database — no redeploy to enable/disable.
Full walkthrough in **[PROVIDERS.md](PROVIDERS.md)**; the annotated template
is [`prospector/providers/example_provider.py`](prospector/providers/example_provider.py).

---

## Repo layout

```
run.py                     entrypoint: --check (validate) / --serve
prospector/                backend
  server.py                the HTTP surface (routes, auth, CORS)
  auth.py                  per-rep bearer-token auth (hashes only, at rest)
  recognize.py             URL → surface classification + HubSpot lookup
  providers/               pluggable enrichment adapters
    __init__.py            the ProviderAdapter interface + registry
    fullenrich.py          reference adapter (FullEnrich)
    example_provider.py    annotated template — copy this
    types.py               shared value types
  waterfall.py             walk provider legs per field, log every attempt
  budget.py / jobs.py      spend caps + async enrichment job queue
  guards.py                pre-write dedupe/identity guards
  writer.py                the guarded commit pipeline (the only path that writes)
  resolve.py               live duplicate-match lookups before a write
  hubspot.py               HubSpot API client
  database.py              Postgres access + boot-time privilege preflight
sql/                       schema, least-privilege role, views, seed
extension/                 Chrome MV3 side-panel extension (thin client)
tests/                     pytest suite (no network, no live DB)
```

## What's intentionally not included

- **No enrichment/CRM credentials.** `.env` is gitignored; only
  `.env.example` (placeholders) is committed.
- **Bring your own HubSpot property conventions.** The commit flow writes
  standard properties (name, email, phone, LinkedIn URL, ICP tier,
  target-account) — adapt `prospector/writer.py` to your schema.
- **Owner routing is a stub.** New records are assigned to the committing rep,
  or flagged for triage. Swap `prospector/owners.py` for your own rule
  (territory, round-robin, a CRM lookup).

## Security

The extension is a low-permission thin client; the backend enforces per-rep
auth with constant-time token comparison, exact-origin CORS, a mandatory
audit-before-write log, server-side spend caps, and env-pinned dry-run. Full
write-up in **[SECURITY.md](SECURITY.md)**.

## License

[MIT](LICENSE). Fork it, adapt it, ship it.
