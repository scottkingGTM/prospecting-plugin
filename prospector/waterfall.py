"""The waterfall runner: walk provider legs per field, log every attempt,
return the merged result.

This is the `runner` callable jobs.run_job_async() expects -- the piece
between the job queue (jobs.py) and the vendor adapters (providers/). It
owns three disciplines, all inherited from an earlier enrichment-waterfall
runner:

  * Never re-buy a known miss. Before spending a credit on a field, the
    runner asks prospector.attempts whether ANY job for this profile got an
    authoritative answer ('found' or 'not_found') for that field within the
    last 30 days. A recent 'not_found' means the vendors already told us no:
    the runner records a zero-cost attempt row tagged {"cached_miss": true}
    and skips the field entirely -- the rep gets the same honest miss for
    free. (A recent 'found' is the odd case: the job should never have been
    created, but historical result payloads are stored per-JOB on jobs.result,
    not per-field, so there is nothing cheap to replay -- the runner simply
    re-runs the legs. Rare, correct, and simple beats a second result store.)

  * Every leg leaves an attempt row, IMMEDIATELY, in its own transaction.
    The audit of a provider call that already happened (and may already be
    billed) must survive a crash two legs later, so each row commits on its
    own. And the insert itself follows the waterfall service's never-raise
    rule: a failed audit write is logged and the job continues -- losing one
    ledger row is bad, but killing a half-billed job over it is worse.

  * Partial success = success. A job asking for three fields that finds one
    returns that one, with the other two listed in fields_missed -- a rep
    holding a verified work email must not see 'failed' because the mobile
    vendor was down. The runner raises (as jobs.JobRunError, carrying
    exactly what was billed so budget.settle() burns the right amount) ONLY
    when EVERY requested field ended in provider errors -- i.e. the job
    produced no hit AND no authoritative miss, so there is genuinely nothing
    to report but the outage.

Four-status attempt vocabulary (matches the CHECK-free column comment on
prospector.attempts):

  found                   -- the leg returned >= 1 hit for the field;
                             cost = what the CALL cost (see _attempt_cost):
                             the vendor's job-level billed figure from
                             meta['billed_credits'] when the adapter
                             surfaced one (capped at the leg's max_cost),
                             else the adapter's list price for the field
                             ONCE. Never the sum of per-hit prices -- one
                             mobile call answering with mobile+direct+HQ
                             is still one 10-credit call (a review found
                             this). The hits' own cost_credits stay
                             display-only per-hit info.
  not_found               -- the leg answered and had nothing. cost 0 for
                             bill-on-hit vendors; a vendor that bills its
                             misses reports that via meta["billed_credits"]
                             (FullEnrich does not set it today, so its
                             misses cost 0 -- documented assumption).
  rejected_over_max_cost  -- the leg was never called: adapter.cost() for
                             the field exceeded the leg's max_cost. Recorded
                             so hit_rate math can see the skip.
  error                   -- ProviderError: the vendor or the wire failed.
                             The runner moves on to the NEXT leg -- one
                             vendor being down must not kill the whole job.

Budget note: this module only REPORTS billed credits (the second element of
the runner's return, or JobRunError.billed). Settling the reservation is
jobs.py's job -- never done here.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from .providers import ContactResult, EnrichInput, ProviderAdapter, ProviderError

logger = logging.getLogger(__name__)

# How long an authoritative attempt outcome ('found'/'not_found') suppresses
# re-buying the same field for the same profile. Inlined in the SQL below;
# this constant exists for docs/tests.
REBUY_WINDOW_DAYS = 30

# provider_id stamped on cached-miss attempt rows. Not a real provider --
# and deliberately allowed: prospector.attempts carries no FK on provider_id
# (see its column comment) precisely so ledger rows can outlive provider
# config, and v_health tolerates unknown ids.
CACHE_PROVIDER_ID = "cache"

# Enabled legs joined to enabled providers, in walk order. Both enabled
# flags are honored HERE so a disabled row is invisible to the runner the
# moment the operator flips it -- no code path needs to re-check.
_LOAD_WATERFALLS_SQL = """
    SELECT w.field, w.position, w.provider_id, w.stop_on, w.max_cost
      FROM prospector.waterfalls w
      JOIN prospector.providers p ON p.id = w.provider_id AND p.enabled
     WHERE w.enabled
     ORDER BY w.field, w.position;
"""

# The never-re-buy lookup: most recent authoritative outcome for this
# (profile, field) across ALL jobs (any rep -- a miss one rep paid for is a
# miss another rep should not pay for again) inside the 30-day window.
# rejected_* and error rows are deliberately excluded: neither is an answer
# about the person, only about our config or the vendor's uptime. Cache rows
# are excluded too (a review found this): a cached_miss is an ECHO of a
# vendor answer, not a new one -- letting it anchor the window would
# re-arm the 30 days on every free replay and a miss could stay "known"
# forever without any vendor ever being asked again.
_PRIOR_OUTCOME_SQL = """
    SELECT status
      FROM prospector.attempts a
      JOIN prospector.jobs j ON j.id = a.job_id
     WHERE j.norm_linkedin_url = %s
       AND a.field = %s
       AND a.status IN ('found', 'not_found')
       AND a.provider_id != 'cache'
       AND a.created_at > now() - interval '30 days'
     ORDER BY a.created_at DESC
     LIMIT 1;
"""

_INSERT_ATTEMPT_SQL = """
    INSERT INTO prospector.attempts
        (job_id, provider_id, field, status, cost_credits, latency_ms,
         dnc_flag, raw_response)
    VALUES (%(job_id)s, %(provider_id)s, %(field)s, %(status)s,
            %(cost_credits)s, %(latency_ms)s, %(dnc_flag)s,
            %(raw_response)s);
"""


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------


def load_waterfalls(db: Any) -> dict[str, list[dict]]:
    """field -> ordered list of enabled legs whose provider is enabled.

    Each leg is a plain dict {field, position, provider_id, stop_on,
    max_cost} with max_cost coerced to float (psycopg2 hands numeric back
    as Decimal). Ordering comes from the SQL (field, position) so the list
    is already the walk order. A field with no enabled legs simply has no
    key -- the runner treats that as an immediate miss for the field.
    """
    out: dict[str, list[dict]] = {}
    for row in db.query(_LOAD_WATERFALLS_SQL):
        field = str(row["field"])
        out.setdefault(field, []).append({
            "field": field,
            "position": int(row["position"]),
            "provider_id": str(row["provider_id"]),
            "stop_on": str(row.get("stop_on") or "verified"),
            "max_cost": float(row["max_cost"]),
        })
    return out


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _merge_first_nonempty(dst: dict, src: dict) -> None:
    """First non-empty value wins, in leg order: an earlier leg's answer is
    never overwritten by a later one, but a hole IS filled."""
    for key, value in (src or {}).items():
        if not _empty(value) and _empty(dst.get(key)):
            dst[key] = value


def _hits_for_field(result: ContactResult, field: str) -> list:
    """Which of the result's hits answer THIS field. Emails split on their
    declared type; ANY phone answers 'mobile' (the mobile waterfall is the
    only phone waterfall, and a vendor-typed 'direct' line for the person is
    still the answer the rep asked for -- its type travels in the payload)."""
    if field == "work_email":
        return [e for e in result.emails if e.type == "work"]
    if field == "personal_email":
        return [e for e in result.emails if e.type == "personal"]
    if field == "mobile":
        return list(result.phones)
    return []


def _dnc(hits: list) -> bool | None:
    """Collapse the hits' DNC flags for the attempt row: True if any hit is
    flagged, False if the vendor said anything at all and none were, None if
    the vendor never said (None = 'vendor did not say', per PhoneHit)."""
    flags = [getattr(h, "dnc_flag", None) for h in hits]
    if any(f is True for f in flags):
        return True
    if any(f is False for f in flags):
        return False
    return None


def _stop_satisfied(stop_on: str, hits: list) -> bool:
    """Does this leg's outcome end the walk for the field? 'verified' needs
    a verified-status hit (a 'risky' address falls through to the next,
    stronger leg); any other stop_on is satisfied by any hit at all."""
    if not hits:
        return False
    if stop_on == "verified":
        return any(getattr(h, "status", None) == "verified" for h in hits)
    return True


def _attempt_cost(meta: dict, adapter: ProviderAdapter, field: str,
                  max_cost: float) -> float:
    """What a FOUND leg's call cost -- billed per CALL, never per hit
    (a review found a single mobile response carrying mobile+direct+HQ hits
    summed to 30 credits against a 10-credit reservation). Precedence:

      1. meta['billed_credits'] -- the vendor's own job-level billed
         figure, surfaced by the adapter only when the payload carries one
         -- capped at the leg's max_cost (the cap the reservation was
         sized against).
      2. adapter.cost([field]) -- the list price for THIS field, once,
         regardless of how many hits the call returned.

    The hits' own cost_credits stay display-only per-hit info; attempt
    accounting deliberately ignores their sum. Bool guard mirrors
    _number() in the adapters (a vendor "billed": true is not 1.0)."""
    value = (meta or {}).get("billed_credits")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return min(float(value), float(max_cost))
    return float(adapter.cost([field]))


def _miss_cost(meta: dict) -> float:
    """What a MISS cost. Bill-on-hit vendors (FullEnrich work email) charge
    nothing, and FullEnrich's adapter sets no billing key on a miss, so the
    default is 0. An adapter for a vendor that bills regardless of outcome
    declares it via meta["billed_credits"]; the bool guard mirrors the
    adapters' own _number() discipline (a vendor "billed": true must not
    become 1.0 credits)."""
    value = (meta or {}).get("billed_credits")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _build_input(job_row: dict) -> EnrichInput:
    """jobs.input (jsonb) -> EnrichInput. norm_linkedin_url from the job row
    is the fallback identity -- it is always present (NOT NULL) even when
    the extension sent a sparse input payload."""
    raw = job_row.get("input") or {}
    if isinstance(raw, str):  # psycopg2 may hand jsonb back as text
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    return EnrichInput(
        linkedin_url=str(raw.get("linkedin_url")
                         or job_row.get("norm_linkedin_url") or ""),
        first_name=str(raw.get("first_name") or ""),
        last_name=str(raw.get("last_name") or ""),
        company_domain=str(raw.get("company_domain") or ""),
        company_name=str(raw.get("company_name") or ""),
    )


# ---------------------------------------------------------------------------
# Attempt ledger writes (own transaction each, never raise)
# ---------------------------------------------------------------------------


def _record_attempt(db: Any, job_id: Any, provider_id: str, field: str,
                    status: str, cost_credits: float, latency_ms: int | None,
                    dnc_flag: bool | None, raw_response: Any) -> None:
    """One ledger row, committed NOW in its own transaction, so the audit of
    a call that already happened (and may already be billed) survives a
    crash on a later leg. Never raises -- the waterfall service's rule: a
    failed audit insert is logged and the job continues, because losing one
    ledger row must not kill a job that may already carry paid-for hits."""
    try:
        with db.cursor() as cur:
            cur.execute(_INSERT_ATTEMPT_SQL, {
                "job_id": job_id,
                "provider_id": provider_id,
                "field": field,
                "status": status,
                "cost_credits": float(cost_credits),
                "latency_ms": latency_ms,
                "dnc_flag": dnc_flag,
                # default=str: a raw vendor payload with a stray Decimal or
                # datetime must degrade to text, not lose the whole row.
                "raw_response": (json.dumps(raw_response, default=str)
                                 if raw_response is not None else None),
            })
    except Exception:
        logger.warning(
            "job %s: attempt insert failed (%s / %s / %s) -- continuing, "
            "audit row lost", job_id, provider_id, field, status,
            exc_info=True,
        )


def _prior_outcome(db: Any, norm_url: str, field: str) -> str | None:
    """Most recent authoritative outcome ('found'/'not_found') for this
    (profile, field) within the re-buy window, or None. A failed lookup is
    logged and treated as no-prior: the worst case of proceeding is paying
    for an answer we might have had, which beats failing the job over a
    read-side hiccup."""
    try:
        rows = db.query(_PRIOR_OUTCOME_SQL, (norm_url, field))
    except Exception:
        logger.warning(
            "prior-attempt lookup failed for field %s -- proceeding as if "
            "no prior attempt exists", field, exc_info=True,
        )
        return None
    return str(rows[0]["status"]) if rows else None


# ---------------------------------------------------------------------------
# One field's walk
# ---------------------------------------------------------------------------


class _FieldOutcome:
    """Everything one field's walk produced. state is one of:
      'found'   -- >= 1 hit collected
      'missed'  -- no hit, but SOMETHING authoritative said no (a vendor
                   not_found, a cached miss, or simply no runnable legs)
      'errored' -- no hit and no authoritative answer: every leg that
                   actually ran raised ProviderError
    Only 'errored' can contribute to failing the whole job, and only when
    EVERY field landed there (partial success = success)."""

    def __init__(self) -> None:
        self.state = "missed"
        self.emails: list = []
        self.phones: list = []
        self.profile: dict = {}
        self.company: dict = {}
        self.providers: list[str] = []
        self.billed = 0.0


def _walk_field(db: Any, registry: dict[str, ProviderAdapter],
                legs: list[dict], job_id: Any, norm_url: str,
                inp: EnrichInput, field: str) -> _FieldOutcome:
    out = _FieldOutcome()

    # -- never re-buy a known miss ------------------------------------------
    prior = _prior_outcome(db, norm_url, field)
    if prior == "not_found":
        _record_attempt(db, job_id, CACHE_PROVIDER_ID, field, "not_found",
                        0.0, 0, None, {"cached_miss": True})
        logger.info(
            "job %s: field %s skipped -- authoritative miss within the "
            "%d-day window (free)", job_id, field, REBUY_WINDOW_DAYS,
        )
        return out  # 'missed', zero adapter calls, zero credits
    # prior == 'found': the job should not have been created for this field,
    # but result payloads are stored per-JOB (jobs.result), not per-field,
    # so there is nothing to replay cheaply -- fall through and re-run the
    # legs. See the module docstring.

    errors = 0
    answered = False  # any vendor not_found = an authoritative "no"

    for leg in legs:
        if not leg.get("enabled", True):
            continue
        adapter = registry.get(leg["provider_id"])
        if adapter is None:
            # Enabled in the DB, no adapter in this build (build_registry
            # already warned at boot) -- skip without an attempt row: the
            # leg was never a real call and never could have been.
            logger.debug(
                "job %s: field %s leg %s has no adapter -- skipped",
                job_id, field, leg["provider_id"],
            )
            continue

        # -- max_cost guard: skip rather than overspend -----------------------
        if adapter.cost([field]) > float(leg["max_cost"]):
            _record_attempt(db, job_id, leg["provider_id"], field,
                            "rejected_over_max_cost", 0.0, None, None, None)
            continue

        started = time.monotonic()
        try:
            result = adapter.resolve(inp, [field])
        except ProviderError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            # str() of a ProviderError is adapter-authored (never raw vendor
            # bytes) and attempts is an operator ledger, not rep-visible.
            _record_attempt(db, job_id, leg["provider_id"], field, "error",
                            0.0, latency_ms, None, {"error": str(exc)})
            errors += 1
            continue  # a vendor being down must not kill the whole job
        latency_ms = int((time.monotonic() - started) * 1000)

        meta = result.meta or {}
        raw = meta.get("raw_response")
        hits = _hits_for_field(result, field)

        # Profile/company scraps are kept from hits AND misses alike -- a
        # miss that still echoed the person's title fills holes for free.
        _merge_first_nonempty(out.profile, result.profile)
        _merge_first_nonempty(out.company, result.company)

        if hits:
            cost = _attempt_cost(meta, adapter, field, leg["max_cost"])
            _record_attempt(db, job_id, leg["provider_id"], field, "found",
                            cost, latency_ms, _dnc(hits), raw)
            out.billed += cost
            out.state = "found"
            out.providers.append(leg["provider_id"])
            if field == "mobile":
                out.phones.extend(hits)
            else:
                out.emails.extend(hits)
            if _stop_satisfied(leg["stop_on"], hits):
                break  # stop_on met -- this field's walk is over
            continue  # weaker hit than stop_on wants: keep it, keep walking

        # -- vendor answered: no ------------------------------------------------
        miss = _miss_cost(meta)
        _record_attempt(db, job_id, leg["provider_id"], field, "not_found",
                        miss, latency_ms, None, raw)
        out.billed += miss
        answered = True

    if out.state != "found" and errors and not answered:
        out.state = "errored"
    return out


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


def make_runner(db: Any, registry: dict[str, ProviderAdapter],
                waterfalls: dict[str, list[dict]],
                ) -> Callable[[dict], tuple[dict, float]]:
    """Build the `runner(job_row) -> (result_payload, credits_billed)`
    callable jobs.run_job_async() expects.

    On total failure (every requested field ended in provider errors -- see
    _FieldOutcome) it raises jobs.JobRunError carrying exactly the credits
    billed before the failure, so budget.settle() burns the right amount.
    Anything less than total failure returns a normal payload with the
    unresolved fields in fields_missed: partial success = success.
    """
    # Imported lazily, same reason as jobs.py's own lazy imports: modules
    # written concurrently against pinned interfaces, and tests swap fakes in.
    from . import jobs

    def runner(job_row: dict) -> tuple[dict, float]:
        inp = _build_input(job_row)
        norm_url = str(job_row.get("norm_linkedin_url") or "")
        job_id = job_row.get("id")

        # Dedupe, preserve order. Validity was enforced at create_job();
        # an unknown field surviving to here is a caller bug and will raise
        # ValueError out of adapter.cost(), which jobs.py reports safely.
        requested: list[str] = []
        for field in job_row.get("fields") or []:
            if field not in requested:
                requested.append(field)

        emails: list = []
        phones: list = []
        profile: dict = {}
        company: dict = {}
        providers_used: list[str] = []
        found: list[str] = []
        total_billed = 0.0
        states: dict[str, str] = {}

        for field in requested:
            outcome = _walk_field(
                db, registry, waterfalls.get(field) or [],
                job_id, norm_url, inp, field,
            )
            states[field] = outcome.state
            total_billed += outcome.billed
            emails.extend(outcome.emails)
            phones.extend(outcome.phones)
            _merge_first_nonempty(profile, outcome.profile)
            _merge_first_nonempty(company, outcome.company)
            for provider_id in outcome.providers:
                if provider_id not in providers_used:
                    providers_used.append(provider_id)
            if outcome.state == "found":
                found.append(field)

        # Total failure ONLY: every field errored (so nothing was found and
        # nothing authoritatively missed). Message is ours -- vendor error
        # text stays in the attempt rows, never in the rep-visible result.
        if requested and all(states[f] == "errored" for f in requested):
            raise jobs.JobRunError(
                "enrichment failed: every provider leg errored for "
                f"{', '.join(requested)} -- nothing was resolved",
                billed=total_billed,
            )

        merged = ContactResult(
            emails=emails, phones=phones, profile=profile, company=company,
            meta={"providers": providers_used},
        )
        payload = merged.to_payload()
        payload["fields_requested"] = list(requested)
        payload["fields_found"] = found
        payload["fields_missed"] = [f for f in requested if f not in found]
        return payload, total_billed

    return runner
