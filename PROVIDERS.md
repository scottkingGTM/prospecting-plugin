# Plugging in an enrichment provider

Enrichment (finding a verified work email, personal email, or mobile) runs
through a small, uniform adapter interface. The project ships with a complete
[FullEnrich](https://fullenrich.com) adapter as the reference implementation;
this guide shows how to add any other vendor.

## The shape of it

Three moving parts:

1. **An adapter** — a Python class that wraps one vendor's API behind two
   methods, `cost()` and `resolve()`.
2. **The registry** — one line in `build_registry()` mapping a provider id to
   your adapter class + its API key.
3. **Config rows in the database** — a `prospector.providers` row (the on/off
   switch) and `prospector.waterfalls` legs (which provider answers which
   field, and in what order). These live in *data*, so enabling, disabling, or
   re-ordering a vendor is an `UPDATE`, never a redeploy.

The extension and the HTTP routes never know which vendor answered — they only
ever see the shared `ContactResult` type. That is the whole point of the
abstraction.

## 1. Write the adapter

Copy [`prospector/providers/example_provider.py`](prospector/providers/example_provider.py)
— it's an annotated skeleton — or read
[`prospector/providers/fullenrich.py`](prospector/providers/fullenrich.py) for
a real one with retries, polling, and billing reconciliation.

Your class subclasses `ProviderAdapter` and sets three class attributes and two
methods:

```python
from collections.abc import Sequence
from . import ContactResult, EmailHit, EnrichInput, ProviderAdapter, ProviderError

class AcmeAdapter(ProviderAdapter):
    id = "acme"           # must match the prospector.providers.id you insert
    kind = "lookup"       # "lookup" = real data source; "inference" = model-guessed
    supports = frozenset(("work_email", "personal_email", "mobile"))

    def __init__(self, api_key: str, *, config: dict | None = None) -> None:
        # build_registry() calls Adapter(api_key, config=<row.config dict>)
        self._api_key = api_key

    def cost(self, fields: Sequence[str]) -> float:
        # WORST-CASE credits to enrich one person for these fields. The budget
        # layer reserves this against the rep's daily cap before the call.
        ...

    def resolve(self, inp: EnrichInput, fields: Sequence[str]) -> ContactResult:
        # Call your vendor with inp (linkedin_url / first_name / last_name /
        # company_domain / company_name). Map the response into EmailHit /
        # PhoneHit objects and return a ContactResult.
        ...
```

**The contract that matters:**

- Raise `ValueError` for a *caller* bug (unknown field, no usable identity).
- Raise `ProviderError` for a *vendor/transport* failure. The waterfall will
  **not** retry a `ProviderError` — do your own transport retries inside
  `resolve()`, then raise. (Re-running a whole enrichment risks paying twice
  for one answer.)
- Email `status` is `verified` | `risky` | `unknown` | `inferred`. An
  `inferred` (pattern-guessed) address can **never** be committed to the CRM —
  `guards.inferred_email_guard` blocks it — so mark guessed addresses honestly.
- Never let your API key appear in a log line or an exception message.
- Put the raw vendor payload in `ContactResult.meta["raw_response"]` for the
  audit ledger; it is stripped before anything reaches the browser.

## 2. Register it

Add the key to `AppConfig` in `prospector/config.py` (read from an env var,
exactly like `fullenrich_api_key`), then add one line to `adapter_specs` in
`build_registry()` (`prospector/providers/__init__.py`):

```python
adapter_specs = {
    "fullenrich": (FullEnrichAdapter, cfg.fullenrich_api_key),
    "acme":       (AcmeAdapter,       cfg.acme_api_key),   # <-- your line
}
```

A provider that's enabled in the DB but missing here is skipped with a warning
(it never crashes boot); a registered provider with a blank key is excluded
(the `/enrich` route then returns a clean 503 instead of failing per request).

## 3. Turn it on in the database

```sql
-- The provider exists and is on:
INSERT INTO prospector.providers (id, kind, enabled, config)
VALUES ('acme', 'lookup', true, '{}')
ON CONFLICT (id) DO NOTHING;

-- It answers the work_email field, as the first (and here only) leg:
INSERT INTO prospector.waterfalls (field, position, provider_id, stop_on, max_cost, enabled)
VALUES ('work_email', 1, 'acme', 'verified', 1, true)
ON CONFLICT (field, position) DO NOTHING;
```

## Waterfalls (multi-vendor fallback)

A "waterfall" is the ordered list of providers tried for one field. The runner
walks legs by `position` and stops when `stop_on` is satisfied (default:
`verified` — a merely `risky` hit falls through to the next, stronger leg).
`max_cost` caps what a leg may bill for one profile; the runner skips a leg
rather than exceed it.

So a two-vendor work-email waterfall — try the cheap vendor first, fall back to
the pricier one only if the first misses or returns an unverified address —
is just two rows:

```sql
INSERT INTO prospector.waterfalls (field, position, provider_id, stop_on, max_cost, enabled) VALUES
  ('work_email', 1, 'acme',       'verified', 1, true),
  ('work_email', 2, 'fullenrich', 'verified', 1, true);
```

The runner also **never re-buys a known miss**: if any job authoritatively
answered "not found" for this person+field in the last 30 days, it records a
zero-cost cached miss and skips the spend.

## Cost, caps, and honesty

- `cost()` quotes the worst case; the reservation is sized against it and the
  unspent difference is released when the job settles at what was actually
  billed.
- Per-rep daily caps (credit / promote / tier-1) live on `prospector.reps` and
  are enforced server-side. A cap rejection is itself logged.
- Every vendor call lands one row in `prospector.attempts` (hit/miss/error,
  cost, latency, raw payload) — the ledger you reconcile vendor invoices
  against. `prospector.v_health` rolls it up per vendor per day.
