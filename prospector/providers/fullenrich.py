"""FullEnrich adapter -- the live lookup vendor.

Wire shape is the vendor's REAL v2 API (docs.fullenrich.com/api/v2/...),
verified live. The traps this module exists to respect:

*   THE V1-FOLKLORE INCIDENT. The first cut of this adapter inherited its
    wire shape from an earlier internal enrichment job, believed
    live-proven. It was not: that project's enrichment path never ran
    (blocked on its API key), and the inherited shape struck out three
    ways at once against the live API -- `datas` vs `data` (the request
    array key), `firstname`/`lastname` vs `first_name`/`last_name` (the
    datum name keys), and a v1-flavored payload against the v2 endpoints.
    Every strike failed OPAQUELY as `error.enrichment.data.empty` (the
    vendor drops keys it does not recognize and then sees an empty
    request). The lesson generalizes past FullEnrich: NOTHING is proven
    until the live smoke bills or answers -- code that never ran is
    folklore, whatever its comments claim.

*   POLL, NOT WEBHOOK. The spec originally assumed a completion webhook;
    the audit found FullEnrich has no proven one. Enrichment is start
    (POST /contact/enrich/bulk) then poll (GET /contact/enrich/bulk/{id})
    every poll_seconds up to max_polls. A poll that times out raises
    ProviderError("... still_running ...") and the adapter NEVER re-buys:
    starting a second job for the same person could bill twice for one
    answer. What to do next is the waterfall's decision, not this module's.

*   V2 POLL STATUSES are a documented enum: CREATED | IN_PROGRESS |
    CANCELED | CREDITS_INSUFFICIENT | FINISHED | RATE_LIMIT | UNKNOWN
    (compared case-insensitively; the vendor has been seen shouting).
    FINISHED is the one success. CANCELED / CREDITS_INSUFFICIENT /
    RATE_LIMIT are terminal failures that name themselves in the raised
    error -- CREDITS_INSUFFICIENT says "out of credits" plainly, because
    the extension panel shows that message to a rep. UNKNOWN (and any
    unrecognized status) keeps polling: the vendor documents it as a
    transient, and the max_polls / RESOLVE_DEADLINE_SECONDS bounds already
    guarantee the loop ends.

*   BILL-ON-HIT. Work email lists at 1 credit but bills ONLY when an email
    is found (measured live). cost() still quotes the full list price -- it
    is a worst-case reservation figure by contract. What actually gets
    BOOKED per call is the waterfall's job: it reads meta['billed_credits']
    -- the vendor's authoritative job-level figure, v2's top-level
    cost.credits -- and otherwise books the field's list price once per
    call, never a sum of per-hit prices (a review found this).

*   KNOWN-INVALID EMAILS ARE DROPPED. v2 grades emails DELIVERABLE /
    HIGH_PROBABILITY / CATCH_ALL / INVALID / INVALID_DOMAIN. INVALID and
    INVALID_DOMAIN entries are dropped entirely in parsing -- an address
    the vendor itself calls invalid must never surface to a rep, not even
    marked 'unknown'.

*   SALES NAV IS UNENRICHABLE. /recognize rejects Sales Nav URLs upstream;
    resolve() keeps a last-line ValueError on '/sales/' anyway, because a
    Sales Nav URL reaching a paid vendor call means an upstream guard broke
    and the correct outcome is a loud caller-bug error, not a silent miss.

Transport posture mirrors prospector/hubspot.py: injectable transport +
sleep (tests run the full retry matrix with zero network and zero real
sleeping), 429/5xx retried with Retry-After honored (capped at 30s) over
MAX_ATTEMPTS total attempts, 401/403 never retried (a bad key cannot fix
itself), and the API key is set on the httpx client ONCE at construction --
never logged, never in exception text.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Sequence

import httpx

from . import ProviderAdapter, ProviderError
from .types import FIELDS, ContactResult, EmailHit, EnrichInput, PhoneHit

logger = logging.getLogger(__name__)

BASE_URL = "https://app.fullenrich.com/api/v2"
TIMEOUT_SECONDS = 60.0

# Total attempts per HTTP request (1 initial + 3 retries) -- same posture as
# prospector/hubspot.py.
MAX_ATTEMPTS = 4

# Poll cadence: 5s x 33 = 165s worst case, inside the 180s resolve
# deadline. Sized by the live smoke: the vendor routinely takes 1-3
# minutes -- an earlier 3s x 20 = 60s ceiling abandoned a job that
# FINISHED with a DELIVERABLE email ~a minute later, leaving the vendor's
# bill unmatched by our ledger. Abandoning a started enrichment is the most
# expensive kind of failure: we pay and get nothing.
POLL_SECONDS = 5.0
MAX_POLLS = 33

# Overall wall-clock bound on ONE resolve() call, retries and polling
# included (from a hardening pass). max_polls bounds the number of
# polls but not the retry waits inside each poll's HTTP call, so a vendor
# alternating 429s and slow answers could park a worker far past any
# per-request timeout -- and jobs.STALE_AFTER_SECONDS (900s) is sized
# against THIS number (3 single-leg fields x 180s + slack < 900). Breach
# raises ProviderError('...deadline...'); sleeps are truncated so the
# adapter never sleeps past the deadline.
RESOLVE_DEADLINE_SECONDS = 180.0

# List price per field, in credits (measured live). Matches the max_cost
# column seeded on the fullenrich waterfall legs in sql/04_seed.sql.
# work_email is the bill-on-hit one (see module docstring).
LIST_PRICE = {
    "work_email": 1.0,
    "personal_email": 3.0,
    "mobile": 10.0,
}

# Our field name -> the v2 enrich_fields option. All three spellings are
# the vendor's OFFICIAL v2 values (docs.fullenrich.com/api/v2), and
# contact.work_emails is additionally PROVEN LIVE: the same
# datum that 400'd with the folklore spelling "contact.emails"
# (error.enrichment.data.empty) was accepted instantly with
# "contact.work_emails". contact.personal_emails IS an official v2 value
# -- the personal_email waterfall leg gets re-enabled on the strength of
# the vendor's own docs.
_ENRICH_OPTIONS = {
    "work_email": "contact.work_emails",
    "mobile": "contact.phones",
    "personal_email": "contact.personal_emails",
}

# v2 poll statuses (module docstring). Everything not in these two sets --
# CREATED, IN_PROGRESS, UNKNOWN, and anything the vendor invents later --
# keeps polling; max_polls / RESOLVE_DEADLINE_SECONDS end the loop.
# 'cancelled' (double-L) is tolerated alongside the documented 'canceled'.
_TERMINAL_OK = {"finished"}
_TERMINAL_FAIL = {"canceled", "cancelled", "credits_insufficient",
                  "rate_limit"}

# Vendor v2 email verification grade -> our status. None means DROP the
# entry entirely (see module docstring: a known-invalid address never
# surfaces). Anything unrecognized maps to 'unknown' -- a new vendor grade
# must never silently pass as verified.
_EMAIL_STATUS: dict[str, str | None] = {
    "deliverable": "verified",
    "high_probability": "risky",
    "catch_all": "unknown",
    "invalid": None,
    "invalid_domain": None,
}

# Identity fields passed through to ContactResult.profile from the v2
# data[0].profile object, both snake_case (v2's own convention) and the
# older squashed spellings tolerated. Best-effort passthrough only: nothing
# downstream may REQUIRE a key from these dicts.
_PROFILE_KEYS = (
    "first_name", "last_name", "full_name", "firstname", "lastname",
    "fullname", "title", "job_title", "headline", "linkedin_url",
    "location", "country",
)
_COMPANY_KEYS = (
    "company", "company_name", "domain", "company_domain",
    "company_linkedin_url", "company_website",
)


def _number(value) -> float | None:
    """A float from a vendor billing field -- None unless it is a real
    number (bool is an int subclass; a vendor `"billed": true` must not
    become 1.0 credits)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _job_billed_credits(result: dict) -> float | None:
    """The vendor's JOB-LEVEL billed figure from the poll payload, or None
    when it does not carry one -- the adapter then omits the
    meta['billed_credits'] key and the waterfall falls back to list price
    per call (a review found this).

    v2's authoritative location is top-level cost.credits (docs). The flat
    key-scan below is a harmless tolerant fallback only. Bare 'credits' is
    deliberately NOT read: at the job level it could as easily mean a
    remaining balance, and misreading a balance as a charge would book the
    whole wallet against one attempt."""
    cost = result.get("cost")
    if isinstance(cost, dict):
        value = _number(cost.get("credits"))
        if value is not None:
            return value
    for key in ("credits_billed", "billed_credits", "credits_used",
                "credits_spent", "cost_credits"):
        value = _number(result.get(key))
        if value is not None:
            return value
    return None


class FullEnrichAdapter(ProviderAdapter):
    """ProviderAdapter for FullEnrich. See the module docstring for the
    traps this class exists to respect."""

    id = "fullenrich"
    kind = "lookup"
    supports = frozenset(FIELDS)

    def __init__(
        self,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
        *,
        config: dict | None = None,
        poll_seconds: float = POLL_SECONDS,
        max_polls: int = MAX_POLLS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            # build_registry() excludes the keyless case before it gets
            # here; this guard catches direct construction.
            raise ValueError("FullEnrich API key is empty")
        self._config = config or {}
        self._poll_seconds = float(poll_seconds)
        self._max_polls = int(max_polls)
        self._sleep = sleep
        # Injectable like sleep, for the same reason: tests drive the
        # RESOLVE_DEADLINE_SECONDS wall clock without real waiting.
        self._clock = clock
        # Auth header set once, here, and never touched again -- no other
        # code path in this module ever sees or formats the key.
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # -- ProviderAdapter interface -------------------------------------------

    def cost(self, fields: Sequence[str]) -> float:
        """Worst-case credits for one profile: the sum of list prices for
        the requested fields (deduplicated). Work email actually bills only
        on hit -- the reservation is still 1.0 by contract; the unspent
        difference is released when jobs.py settles the job at what the
        waterfall booked per call."""
        requested = set(fields)
        unknown = requested - set(FIELDS)
        if unknown:
            raise ValueError(
                f"unknown enrichment field(s): {sorted(unknown)} "
                f"(valid: {list(FIELDS)})"
            )
        return sum(LIST_PRICE[f] for f in requested)

    def resolve(self, inp: EnrichInput, fields: Sequence[str]) -> ContactResult:
        """Start one single-person enrichment job and poll it to completion.

        ValueError for caller bugs (unknown field, Sales Nav URL, no
        identity); ProviderError for anything the vendor or the wire did
        wrong. A timed-out poll is ProviderError with 'still_running' in the
        message -- the job may yet finish on the vendor's side, and the
        WATERFALL decides what to do about that. This adapter never starts
        a second job for the same person (never re-buys).
        """
        enrich_fields = self._enrich_options(fields)

        # Last-line Sales Nav defense: /recognize must have rejected these
        # already, and reaching a paid vendor call with one means an
        # upstream guard broke. Loud caller-bug error, not a silent miss.
        if "/sales/" in (inp.linkedin_url or ""):
            raise ValueError(
                "Sales Nav URLs are unenrichable and must be rejected "
                "upstream -- refusing to spend credits on one"
            )

        datum = self._datum(inp, enrich_fields)

        # One wall-clock deadline covers the WHOLE resolve -- the start
        # POST's retries, every poll, and every retry wait inside a poll
        # (from a hardening pass; jobs.STALE_AFTER_SECONDS is sized
        # against this bound).
        started_at = self._clock()
        deadline = started_at + RESOLVE_DEADLINE_SECONDS
        started = self._request_json(
            "POST",
            "/contact/enrich/bulk",
            json_body={"name": "prospecting_plugin", "data": [datum]},
            deadline=deadline,
        )
        enrichment_id = started.get("enrichment_id") or started.get("id")
        if not enrichment_id:
            raise ProviderError(
                f"FullEnrich enrichment start returned no id: "
                f"{str(started)[:200]}"
            )

        result = self._poll(str(enrichment_id), deadline)
        latency_ms = int((self._clock() - started_at) * 1000)

        contact_result = _parse_result(result)
        contact_result.meta = {
            "provider_id": self.id,
            "request_id": str(enrichment_id),
            "latency_ms": latency_ms,
            "raw_response": result,
        }
        # Documented optional meta key (waterfall._attempt_cost reads it):
        # the vendor's own JOB-LEVEL billed figure -- v2's cost.credits --
        # set only when the payload carries one, never invented (a review
        # found attempt accounting must bill per CALL, not per hit).
        billed = _job_billed_credits(result)
        if billed is not None:
            contact_result.meta["billed_credits"] = billed
        return contact_result

    # -- balance ---------------------------------------------------------------

    def get_balance(self) -> float | None:
        """Current credit balance, or None when it cannot be determined.

        Never raises: /status must be able to show 'balance unknown'
        without an enrichment vendor hiccup taking the whole route down.

        PROVEN LIVE (returning real balances) -- leave alone.
        """
        try:
            payload = self._request_json("GET", "/account/credits")
        except Exception:
            logger.warning("FullEnrich balance check failed", exc_info=True)
            return None
        for key in ("credits", "balance", "remaining"):
            value = _number(payload.get(key))
            if value is not None:
                return value
        return None

    # -- request construction ---------------------------------------------------

    def _enrich_options(self, fields: Sequence[str]) -> list[str]:
        """Requested fields -> deduplicated v2 enrich_fields options, in
        the caller's order. Unknown field names are a ValueError -- never
        forwarded to the vendor, whose unknown-option failures are opaque."""
        options: list[str] = []
        for field in fields:
            if field not in _ENRICH_OPTIONS:
                raise ValueError(
                    f"unknown enrichment field {field!r} (valid: {list(FIELDS)})"
                )
            option = _ENRICH_OPTIONS[field]
            if option not in options:
                options.append(option)
        if not options:
            raise ValueError("no enrichment fields requested")
        return options

    @staticmethod
    def _datum(inp: EnrichInput, enrich_fields: list[str]) -> dict:
        """One v2 bulk-endpoint datum: first_name/last_name always,
        linkedin_url/domain/company_name only when present. The vendor
        requires at least one usable identity, so an input with neither a
        URL nor a full name is a caller bug caught here -- before any
        credits move."""
        first = (inp.first_name or "").strip()
        last = (inp.last_name or "").strip()
        url = (inp.linkedin_url or "").strip()
        if not url and not (first and last):
            raise ValueError(
                "EnrichInput needs a linkedin_url or a first+last name -- "
                "refusing to start an unidentifiable enrichment"
            )
        datum: dict = {
            "first_name": first,
            "last_name": last,
            "enrich_fields": enrich_fields,
        }
        if url:
            # The app's canonical form is protocol-less (linkedin.com/in/x)
            # but the vendor requires a real URL -- a bare host+path reads
            # as no identity at all (live smoke returned
            # error.enrichment.data.empty; the same https-is-mandatory
            # lesson applies to other URL-based vendors).
            if not url.lower().startswith(("http://", "https://")):
                url = "https://www." + url.lstrip("/")
            datum["linkedin_url"] = url
        if inp.company_domain:
            datum["domain"] = inp.company_domain
        if inp.company_name:
            datum["company_name"] = inp.company_name
        return datum

    # -- polling ------------------------------------------------------------------

    def _sleep_within(self, delay: float, deadline: float | None) -> None:
        """Sleep, but never past the resolve deadline: the sleep is
        truncated to the time remaining, and a sleep requested at or after
        the deadline raises instead (from a hardening pass). deadline
        None (get_balance's plain requests) sleeps normally."""
        if deadline is None:
            self._sleep(delay)
            return
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise ProviderError(
                f"FullEnrich resolve deadline exceeded "
                f"({RESOLVE_DEADLINE_SECONDS:.0f}s) -- giving up; not "
                "re-buying, waterfall decides what happens next"
            )
        self._sleep(min(delay, remaining))

    def _poll(self, enrichment_id: str, deadline: float | None = None) -> dict:
        """GET the job every poll_seconds until terminal, max_polls times
        or until the resolve deadline -- whichever bound trips first.

        v2 statuses (module docstring): FINISHED returns; CANCELED /
        CREDITS_INSUFFICIENT / RATE_LIMIT raise naming the status; CREATED
        / IN_PROGRESS / UNKNOWN / anything unrecognized keeps polling.
        Sleep-first: the job never finishes in the same instant it started,
        so polling immediately just burns a request. Timeout is
        ProviderError with 'still_running' -- see resolve() for why the
        adapter stops there.
        """
        for _ in range(self._max_polls):
            self._sleep_within(self._poll_seconds, deadline)
            result = self._request_json(
                "GET", f"/contact/enrich/bulk/{enrichment_id}",
                deadline=deadline)
            status = str(result.get("status", "")).lower()
            if status in _TERMINAL_OK:
                return result
            if status == "credits_insufficient":
                # Plain words on purpose: the extension panel shows this
                # message to a rep, who needs "out of credits", not an
                # enum token.
                raise ProviderError(
                    f"FullEnrich enrichment {enrichment_id} failed: "
                    "CREDITS_INSUFFICIENT -- the FullEnrich account is out "
                    "of credits; top up before retrying"
                )
            if status in _TERMINAL_FAIL:
                raise ProviderError(
                    f"FullEnrich enrichment {enrichment_id} ended "
                    f"{status.upper()}")
        raise ProviderError(
            f"FullEnrich enrichment {enrichment_id} still_running after "
            f"{self._max_polls} polls ({self._max_polls * self._poll_seconds:.0f}s)"
            " -- not re-buying; waterfall decides what happens next"
        )

    # -- transport ------------------------------------------------------------------

    def _request_json(self, method: str, path: str, *,
                      json_body: dict | None = None,
                      deadline: float | None = None) -> dict:
        """One HTTP call with the module's retry posture (module docstring).
        Retry waits respect the resolve deadline when one is given -- a
        retry that would sleep past it raises ProviderError instead.

        Raises ProviderError on any non-2xx outcome; messages are built
        from status/method/path/truncated-body only -- the API key can
        never appear in one.
        """
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = self._client.request(method, path, json=json_body)
            except httpx.HTTPError as exc:
                # Transport-level failure. Retry like a 5xx; `from None` so
                # the httpx exception -- which can carry the full request,
                # Authorization header included -- never rides along.
                if attempt < MAX_ATTEMPTS - 1:
                    delay = self._backoff_delay(attempt, retry_after=None)
                    logger.warning(
                        "FullEnrich %s %s transport error (%s); retrying in "
                        "%.1fs (attempt %d/%d)",
                        method, path, type(exc).__name__, delay,
                        attempt + 1, MAX_ATTEMPTS,
                    )
                    self._sleep_within(delay, deadline)
                    continue
                raise ProviderError(
                    f"FullEnrich {method} {path} failed after {MAX_ATTEMPTS} "
                    f"attempts: {type(exc).__name__}"
                ) from None

            if resp.status_code in (401, 403):
                # Never retried: a bad key cannot fix itself.
                raise ProviderError(
                    f"FullEnrich {method} {path} returned HTTP "
                    f"{resp.status_code} -- check FULLENRICH_API_KEY"
                )

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < MAX_ATTEMPTS - 1:
                    delay = self._backoff_delay(
                        attempt, retry_after=resp.headers.get("Retry-After"))
                    logger.warning(
                        "FullEnrich %s %s returned HTTP %s; retrying in "
                        "%.1fs (attempt %d/%d)",
                        method, path, resp.status_code, delay,
                        attempt + 1, MAX_ATTEMPTS,
                    )
                    self._sleep_within(delay, deadline)
                    continue
                raise ProviderError(
                    f"FullEnrich {method} {path} returned HTTP "
                    f"{resp.status_code} after {MAX_ATTEMPTS} attempts: "
                    f"{resp.text[:200]}"
                )

            if resp.status_code >= 400:
                raise ProviderError(
                    f"FullEnrich {method} {path} returned HTTP "
                    f"{resp.status_code}: {resp.text[:200]}"
                )

            try:
                return resp.json() or {}
            except ValueError:
                raise ProviderError(
                    f"FullEnrich {method} {path} returned non-JSON: "
                    f"{resp.text[:200]}"
                ) from None

        raise ProviderError("unreachable")  # pragma: no cover

    @staticmethod
    def _backoff_delay(attempt: int, retry_after: str | None) -> float:
        """Retry-After wins when usable -- capped at 30s so a pathological
        header can't park a thread -- else exponential backoff with jitter
        (same as prospector/hubspot.py)."""
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), 30.0))
            except ValueError:
                pass  # non-numeric Retry-After -> fall through to backoff
        return float(2 ** attempt) + random.uniform(0.0, 0.5)


# ---------------------------------------------------------------------------
# Response mapping (pure -- unit tested without a client)
# ---------------------------------------------------------------------------


def map_email_status(raw: object) -> str | None:
    """Vendor v2 verification grade -> 'verified' | 'risky' | 'unknown',
    or None meaning DROP the entry (INVALID / INVALID_DOMAIN -- a
    known-invalid address never surfaces, see module docstring).
    Unrecognized (including empty) is 'unknown' by design."""
    key = str(raw or "").lower()
    if key in _EMAIL_STATUS:
        return _EMAIL_STATUS[key]
    return "unknown"


def _parse_result(result: dict) -> ContactResult:
    """Vendor v2 job payload -> ContactResult (meta filled in by resolve()).

    v2 shape: {id, name, status, data: [{input, custom, contact_info,
    profile}], cost: {credits}}. We sent ONE datum, so only data[0] is
    read. Mild shape tolerance kept on purpose (data as a bare dict,
    email/phone entries as bare strings) -- the vendor has drifted before.
    """
    data = result.get("data") or result.get("datas") or []
    if isinstance(data, dict):
        data = [data]
    item = data[0] if data and isinstance(data[0], dict) else {}

    contact_info = item.get("contact_info")
    if not isinstance(contact_info, dict):
        contact_info = {}

    emails = _parse_emails(contact_info)
    phones = _parse_phones(contact_info)

    raw_profile = item.get("profile")
    if not isinstance(raw_profile, dict):
        raw_profile = {}
    profile = {k: raw_profile[k] for k in _PROFILE_KEYS if raw_profile.get(k)}

    # Company: best-effort from the profile object -- a nested company
    # dict wins, flat company_* keys fill in around it.
    company: dict = {}
    nested_company = raw_profile.get("company")
    if isinstance(nested_company, dict):
        company.update(nested_company)
    for key in _COMPANY_KEYS:
        value = raw_profile.get(key)
        if value and not isinstance(value, (dict, list)) and key not in company:
            company[key] = value

    return ContactResult(
        emails=emails, phones=phones, profile=profile, company=company, meta={},
    )


def _parse_emails(contact_info: dict) -> list[EmailHit]:
    """v2 contact_info -> EmailHits: work_emails[] are type 'work',
    personal_emails[] are type 'personal'. Entries are {email, status};
    INVALID / INVALID_DOMAIN entries are dropped entirely (module
    docstring). cost_credits is the field's list price -- display-only
    per-hit info; billing is job-level (cost.credits)."""
    hits: list[EmailHit] = []

    def add_all(entries, *, email_type: str, fallback: float) -> None:
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return
        for entry in entries:
            if isinstance(entry, str):
                address, raw = entry, {}
            elif isinstance(entry, dict):
                address = entry.get("email") or entry.get("address") or ""
                raw = entry
            else:
                continue
            if not address:
                continue
            status = map_email_status(raw.get("status") or raw.get("grade"))
            if status is None:
                continue  # known-invalid: never surfaced
            hits.append(EmailHit(
                address=address,
                type=email_type,
                status=status,
                provider="fullenrich",
                cost_credits=fallback,
            ))

    add_all(contact_info.get("work_emails"), email_type="work",
            fallback=LIST_PRICE["work_email"])
    add_all(contact_info.get("personal_emails"), email_type="personal",
            fallback=LIST_PRICE["personal_email"])
    return hits


def _parse_phones(contact_info: dict) -> list[PhoneHit]:
    """v2 contact_info.phones -> PhoneHits. v2 gives {number, region} only:
    every hit is type 'mobile' (FullEnrich's phone product is the 10-credit
    mobile finder), status 'unknown', dnc_flag None ('not stated' must
    never be presented to a rep as 'cleared to call'). region rides only
    in meta['raw_response'] for the audit trail -- PhoneHit has no slot
    for it."""
    hits: list[PhoneHit] = []
    raw_phones = contact_info.get("phones") or []
    if isinstance(raw_phones, dict):
        raw_phones = [raw_phones]
    for entry in raw_phones:
        if isinstance(entry, str):
            number = entry
        elif isinstance(entry, dict):
            number = entry.get("number") or entry.get("phone") or ""
        else:
            continue
        if not number:
            continue
        hits.append(PhoneHit(
            number=number,
            type="mobile",
            status="unknown",
            dnc_flag=None,
            provider="fullenrich",
            cost_credits=LIST_PRICE["mobile"],
        ))
    return hits
