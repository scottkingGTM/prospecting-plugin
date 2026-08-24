"""Unit tests for prospector.providers — mocked transports only, no network.

Every FullEnrich test drives the adapter through httpx.MockTransport with a
recorded (never slept) sleep, so the start/poll cycle, the retry matrix, and
the timeout path all run instantly and offline. The tests that matter most:

  * the v2 WIRE SHAPE — request array key `data` (not v1's `datas`), datum
    names `first_name`/`last_name` (not `firstname`/`lastname`), and the
    enrich_fields spelling 'contact.work_emails'. All three v1-folklore
    spellings 400'd opaquely against the live API (2026-08-19,
    error.enrichment.data.empty) from code that had never run live;
  * known-INVALID emails are dropped entirely — never surfaced, not even
    as 'unknown';
  * the adapter never re-buys — a timed-out poll raises, it does not
    restart the job;
  * key hygiene — the API key must never surface in an exception message
    or a log line, whichever failure path produced it.
"""

from __future__ import annotations

import json as jsonlib
import logging
from types import SimpleNamespace

import httpx
import pytest

from prospector.providers import (
    FIELDS,
    ContactResult,
    EmailHit,
    PhoneHit,
    ProviderAdapter,
    ProviderError,
    build_registry,
)
from prospector.providers.fullenrich import (
    FullEnrichAdapter,
    LIST_PRICE,
    MAX_ATTEMPTS,
    RESOLVE_DEADLINE_SECONDS,
    map_email_status,
)
from prospector.providers.types import EnrichInput

API_KEY = "fe-super-secret-api-key-value"

START_PATH = "/api/v2/contact/enrich/bulk"
POLL_PATH = "/api/v2/contact/enrich/bulk/enr_123"


def make_adapter(handler, *, poll_seconds: float = 3.0, max_polls: int = 20):
    """Adapter wired to a MockTransport, with sleeps recorded not slept."""
    sleeps: list[float] = []
    adapter = FullEnrichAdapter(
        API_KEY,
        transport=httpx.MockTransport(handler),
        poll_seconds=poll_seconds,
        max_polls=max_polls,
        sleep=sleeps.append,
)
    return adapter, sleeps


def make_clocked_adapter(handler, *, poll_seconds: float = 3.0,
                         max_polls: int = 10_000):
    """Adapter whose fake clock ADVANCES by every recorded sleep, so the
    RESOLVE_DEADLINE_SECONDS wall clock is driven deterministically with
    zero real waiting."""
    sleeps: list[float] = []
    fake = {"t": 0.0}

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        fake["t"] += seconds

    adapter = FullEnrichAdapter(
        API_KEY,
        transport=httpx.MockTransport(handler),
        poll_seconds=poll_seconds,
        max_polls=max_polls,
        sleep=sleep,
        clock=lambda: fake["t"],
)
    return adapter, sleeps, fake


def started_response() -> httpx.Response:
    return httpx.Response(200, json={"enrichment_id": "enr_123"})


def finished_payload(*, contact_info: dict | None = None,
                     profile: dict | None = None,
                     cost: dict | None = None) -> dict:
    """A realistic v2 FINISHED payload, built from the vendor doc shape
    (docs.fullenrich.com/api/v2): {id, name, status, data: [{input,
    custom, contact_info, profile}], cost: {credits}}."""
    if contact_info is None:
        contact_info = {
            "most_probable_work_email": "jane@acme.com",
            "work_emails": [
                {"email": "jane@acme.com", "status": "DELIVERABLE"},
            ],
            "most_probable_personal_email": None,
            "personal_emails": [],
            "most_probable_phone": "+15550100",
            "phones": [
                {"number": "+15550100", "region": "US"},
            ],
        }
    if profile is None:
        profile = {
            "first_name": "Jane",
            "last_name": "Doe",
            "title": "VP Operations",
            "linkedin_url": "https://www.linkedin.com/in/jane-doe",
            "company_name": "Acme HVAC",
            "company_domain": "acme.com",
        }
    payload = {
        "id": "enr_123",
        "name": "prospecting_plugin",
        # Uppercase on purpose: the doc enum is uppercase and the
        # terminal-status check must be case-insensitive.
        "status": "FINISHED",
        "data": [{
            "input": {"first_name": "Jane", "last_name": "Doe"},
            "custom": {},
            "contact_info": contact_info,
            "profile": profile,
        }],
    }
    if cost is not None:
        payload["cost"] = cost
    return payload


def start_then_finish(payload: dict, polls_running: int = 0):
    """Handler: POST starts the job, then `polls_running` IN_PROGRESS
    polls, then the finished payload. Records every request for
    assertions."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            assert request.url.path == START_PATH
            return started_response()
        assert request.url.path == POLL_PATH
        polls_so_far = sum(1 for r in seen if r.method == "GET")
        if polls_so_far <= polls_running:
            return httpx.Response(200, json={"status": "IN_PROGRESS"})
        return httpx.Response(200, json=payload)

    return handler, seen


INPUT = EnrichInput(
    linkedin_url="linkedin.com/in/jane-doe",
    first_name="Jane",
    last_name="Doe",
    company_domain="acme.com",
    company_name="Acme HVAC",
)


# -- cost math -------------------------------------------------------------------


@pytest.mark.parametrize("fields,expected", [
    (["work_email"], 1.0),
    (["personal_email"], 3.0),
    (["mobile"], 10.0),
    (["work_email", "mobile"], 11.0),
    (["work_email", "personal_email"], 4.0),
    (["personal_email", "mobile"], 13.0),
    (["work_email", "mobile", "personal_email"], 14.0),
    (["work_email", "work_email"], 1.0),  # dedupe: never double-reserved
])
def test_cost_is_worst_case_sum_of_list_prices(fields, expected):
    """cost() is a worst-case RESERVATION figure: work email actually bills
    only on hit, but the refund happens at settle, never in the quote."""
    adapter, _ = make_adapter(lambda request: httpx.Response(500))
    assert adapter.cost(fields) == expected


def test_cost_unknown_field_raises_value_error():
    adapter, _ = make_adapter(lambda request: httpx.Response(500))
    with pytest.raises(ValueError) as excinfo:
        adapter.cost(["work_email", "fax"])
    assert "fax" in str(excinfo.value)


def test_list_price_matches_seeded_waterfall_max_cost():
    """sql/04_seed.sql seeds max_cost 1/3/10 on the fullenrich legs; the
    Python list prices must agree or budgets and legs drift apart."""
    assert LIST_PRICE == {"work_email": 1.0, "personal_email": 3.0,
                          "mobile": 10.0}
    assert set(LIST_PRICE) == set(FIELDS)


# -- resolve: request construction -------------------------------------------------


def test_resolve_posts_to_v2_path_with_data_array_key():
    """THE v1-folklore test: the request must hit the v2 bulk path and
    carry its contacts under `data` — v1's `datas` 400'd opaquely
    (error.enrichment.data.empty, a live smoke test)."""
    handler, seen = start_then_finish(finished_payload())
    adapter, _ = make_adapter(handler)

    adapter.resolve(INPUT, ["work_email"])

    assert seen[0].url.path == "/api/v2/contact/enrich/bulk"
    body = jsonlib.loads(seen[0].content)
    assert "data" in body
    assert "datas" not in body
    assert len(body["data"]) == 1


def test_resolve_sends_v2_enrich_fields_spellings():
    """enrich_fields values are the official v2 set. 'contact.work_emails'
    is additionally PROVEN LIVE — the same datum 400'd with the
    folklore spelling 'contact.emails' (the vendor drops unknown field
    names and then sees an empty request)."""
    handler, seen = start_then_finish(finished_payload())
    adapter, _ = make_adapter(handler)

    adapter.resolve(INPUT, ["work_email", "mobile", "personal_email"])

    body = jsonlib.loads(seen[0].content)
    (datum,) = body["data"]
    assert datum["enrich_fields"] == [
        "contact.work_emails",      # PROVEN LIVE
        "contact.phones",
        "contact.personal_emails",  # official v2 value (vendor docs)
    ]
    assert "contact.emails" not in datum["enrich_fields"]


def test_resolve_datum_carries_identity_and_company():
    handler, seen = start_then_finish(finished_payload())
    adapter, _ = make_adapter(handler)

    adapter.resolve(INPUT, ["work_email"])

    body = jsonlib.loads(seen[0].content)
    assert body["name"] == "prospecting_plugin"
    (datum,) = body["data"]
    # v2 name keys are snake_case — 'firstname'/'lastname' was v1 folklore
    # that never ran live.
    assert datum["first_name"] == "Jane"
    assert datum["last_name"] == "Doe"
    assert "firstname" not in datum
    assert "lastname" not in datum
    # Canonical protocol-less form is up-converted to a real URL — the
    # vendor rejects bare host+path as "data empty" (a live smoke test).
    assert datum["linkedin_url"] == "https://www.linkedin.com/in/jane-doe"
    assert datum["domain"] == "acme.com"
    assert datum["company_name"] == "Acme HVAC"


def test_resolve_omits_blank_optional_identity_fields():
    handler, seen = start_then_finish(finished_payload())
    adapter, _ = make_adapter(handler)

    adapter.resolve(EnrichInput(first_name="Jane", last_name="Doe"),
                    ["work_email"])

    (datum,) = jsonlib.loads(seen[0].content)["data"]
    assert "linkedin_url" not in datum
    assert "domain" not in datum
    assert "company_name" not in datum


def test_resolve_rejects_sales_nav_url_before_any_request():
    """Last line of the Sales Nav defense: these URLs are unenrichable, so
    one reaching a paid vendor call means an upstream guard broke -- loud
    ValueError, zero HTTP requests, zero credits."""
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request may be made for a Sales Nav URL")

    adapter, sleeps = make_adapter(handler)
    with pytest.raises(ValueError) as excinfo:
        adapter.resolve(
            EnrichInput(
                linkedin_url="linkedin.com/sales/lead/ACwAA123",
                first_name="Jane", last_name="Doe"),
            ["work_email"],
)
    assert "Sales Nav" in str(excinfo.value)
    assert sleeps == []


def test_resolve_rejects_unidentifiable_input_before_any_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request may be made without an identity")

    adapter, _ = make_adapter(handler)
    with pytest.raises(ValueError):
        adapter.resolve(EnrichInput(first_name="Jane"), ["work_email"])


def test_resolve_unknown_field_raises_value_error():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("unknown fields must never reach the vendor")

    adapter, _ = make_adapter(handler)
    with pytest.raises(ValueError):
        adapter.resolve(INPUT, ["work_email", "fax"])


# -- resolve: poll loop --------------------------------------------------------------


def test_poll_happy_path_in_progress_then_finished():
    """Sleep-first cadence, one sleep per poll: IN_PROGRESS, IN_PROGRESS,
    FINISHED is exactly three 3.0s sleeps and three GETs — against a
    realistic v2 FINISHED payload."""
    handler, seen = start_then_finish(finished_payload(), polls_running=2)
    adapter, sleeps = make_adapter(handler)

    result = adapter.resolve(INPUT, ["work_email", "mobile"])

    gets = [r for r in seen if r.method == "GET"]
    assert len(gets) == 3
    assert sleeps == [3.0, 3.0, 3.0]
    assert isinstance(result, ContactResult)
    assert result.meta["request_id"] == "enr_123"
    assert result.emails[0].address == "jane@acme.com"


def test_poll_created_status_keeps_polling():
    """CREATED is the pre-start state in the v2 enum — not terminal."""
    seen_statuses = iter(["CREATED", "IN_PROGRESS", "FINISHED"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return started_response()
        status = next(seen_statuses)
        if status == "FINISHED":
            return httpx.Response(200, json=finished_payload())
        return httpx.Response(200, json={"status": status})

    adapter, sleeps = make_adapter(handler)
    result = adapter.resolve(INPUT, ["work_email"])
    assert result.meta["request_id"] == "enr_123"
    assert sleeps == [3.0, 3.0, 3.0]


def test_poll_unknown_status_keeps_polling_until_finished():
    """UNKNOWN is documented as transient: keep polling (bounded by
    max_polls / the resolve deadline), never treat it as failure."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return started_response()
        polls = handler.polls = getattr(handler, "polls", 0) + 1
        if polls < 3:
            return httpx.Response(200, json={"status": "UNKNOWN"})
        return httpx.Response(200, json=finished_payload())

    adapter, sleeps = make_adapter(handler)
    result = adapter.resolve(INPUT, ["work_email"])
    assert result.meta["request_id"] == "enr_123"
    assert sleeps == [3.0, 3.0, 3.0]


def test_poll_unknown_status_forever_ends_in_still_running():
    """A job stuck on UNKNOWN drains max_polls and raises still_running —
    UNKNOWN never spins past the loop's bounds."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return started_response()
        return httpx.Response(200, json={"status": "UNKNOWN"})

    adapter, _ = make_adapter(handler, max_polls=3)
    with pytest.raises(ProviderError) as excinfo:
        adapter.resolve(INPUT, ["work_email"])
    assert "still_running" in str(excinfo.value)


def test_poll_timeout_raises_still_running_and_never_rebuys():
    """max_polls exhausted -> ProviderError('still_running'); the adapter
    must NOT start a second job (exactly ONE POST), because re-buying could
    bill twice for one answer. The waterfall owns the next move."""
    posts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(1)
            return started_response()
        return httpx.Response(200, json={"status": "in_progress"})

    adapter, sleeps = make_adapter(handler, max_polls=4)
    with pytest.raises(ProviderError) as excinfo:
        adapter.resolve(INPUT, ["work_email"])

    assert "still_running" in str(excinfo.value)
    assert posts == [1]  # never re-bought
    assert sleeps == [3.0, 3.0, 3.0, 3.0]


def test_resolve_deadline_bounds_poll_loop_and_truncates_final_sleep():
    """max_polls bounds poll COUNT, not wall
    clock -- retry waits inside each poll could park a worker for far
    longer. RESOLVE_DEADLINE_SECONDS (180s) bounds the whole resolve: the
    last sleep is truncated to the time remaining and the breach raises
    ProviderError('deadline'), never sleeping past the line."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return started_response()
        return httpx.Response(200, json={"status": "IN_PROGRESS"})

    # 7s polls: 25 full sleeps = 175s, then a 5s TRUNCATED sleep = 180s,
    # then the next iteration's check trips the deadline.
    adapter, sleeps, fake = make_clocked_adapter(handler, poll_seconds=7.0)
    with pytest.raises(ProviderError) as excinfo:
        adapter.resolve(INPUT, ["work_email"])

    assert "deadline" in str(excinfo.value)
    assert "still_running" not in str(excinfo.value)  # deadline, not max_polls
    assert fake["t"] == RESOLVE_DEADLINE_SECONDS  # never slept past it
    assert sleeps[-1] == pytest.approx(5.0)       # truncated final sleep
    assert all(s == 7.0 for s in sleeps[:-1])


def test_resolve_deadline_also_bounds_retry_waits():
    """A Retry-After wait inside a poll's HTTP call checks the same
    deadline: it is truncated to what remains, and the retry AFTER it
    raises 'deadline' instead of sleeping again."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return started_response()
        return httpx.Response(429, headers={"Retry-After": "30"}, json={})

    # One 170s poll sleep leaves 10s; the 30s Retry-After is truncated to
    # 10s; the next retry finds zero remaining and raises.
    adapter, sleeps, fake = make_clocked_adapter(handler, poll_seconds=170.0)
    with pytest.raises(ProviderError) as excinfo:
        adapter.resolve(INPUT, ["work_email"])

    assert "deadline" in str(excinfo.value)
    assert sleeps == [170.0, 10.0]
    assert fake["t"] == RESOLVE_DEADLINE_SECONDS


@pytest.mark.parametrize("status", ["CANCELED", "cancelled", "RATE_LIMIT"])
def test_poll_terminal_failure_raises_provider_error_naming_status(status):
    """CANCELED / RATE_LIMIT (and the double-L spelling, tolerated) are
    terminal v2 failures: the raised error names the status."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return started_response()
        return httpx.Response(200, json={"status": status})

    adapter, _ = make_adapter(handler)
    with pytest.raises(ProviderError) as excinfo:
        adapter.resolve(INPUT, ["work_email"])
    assert status.upper() in str(excinfo.value)


def test_poll_credits_insufficient_says_out_of_credits_plainly():
    """CREDITS_INSUFFICIENT is shown to a rep in the extension panel, so
    the message must say 'out of credits' in plain words, not just echo an
    enum token."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return started_response()
        return httpx.Response(200, json={"status": "CREDITS_INSUFFICIENT"})

    adapter, _ = make_adapter(handler)
    with pytest.raises(ProviderError) as excinfo:
        adapter.resolve(INPUT, ["work_email"])
    message = str(excinfo.value)
    assert "CREDITS_INSUFFICIENT" in message
    assert "out of credits" in message


def test_start_without_enrichment_id_raises_provider_error():
    adapter, _ = make_adapter(
        lambda request: httpx.Response(200, json={"ok": True}))
    with pytest.raises(ProviderError) as excinfo:
        adapter.resolve(INPUT, ["work_email"])
    assert "no id" in str(excinfo.value)


# -- resolve: response mapping ----------------------------------------------------------


def test_result_maps_emails_phones_profile_company_and_meta():
    handler, _ = start_then_finish(finished_payload(cost={"credits": 1}))
    adapter, _ = make_adapter(handler)

    result = adapter.resolve(INPUT, ["work_email", "mobile"])

    assert result.emails == [EmailHit(
        address="jane@acme.com", type="work", status="verified",
        provider="fullenrich", cost_credits=1.0)]
    # v2 phones carry {number, region} only: type is always 'mobile',
    # status 'unknown', dnc_flag None ('not stated' must never read as
    # 'cleared to call'); region stays behind in raw_response.
    assert result.phones == [PhoneHit(
        number="+15550100", type="mobile", status="unknown", dnc_flag=None,
        provider="fullenrich", cost_credits=10.0)]
    assert result.profile["first_name"] == "Jane"
    assert result.profile["title"] == "VP Operations"
    assert result.company == {"company_name": "Acme HVAC",
                              "company_domain": "acme.com"}
    assert result.meta["provider_id"] == "fullenrich"
    assert result.meta["request_id"] == "enr_123"
    assert result.meta["latency_ms"] >= 0
    assert result.meta["billed_credits"] == 1.0
    # The FULL vendor payload rides in meta for the attempts audit trail.
    assert result.meta["raw_response"]["status"] == "FINISHED"


@pytest.mark.parametrize("raw,expected", [
    # The v2 doc enum, pinned: DELIVERABLE is the only 'verified' grade.
    ("DELIVERABLE", "verified"),
    ("deliverable", "verified"),  # case-insensitive
    ("HIGH_PROBABILITY", "risky"),
    ("CATCH_ALL", "unknown"),
    # Known-invalid grades mean DROP (None) — never surfaced at all.
    ("INVALID", None),
    ("INVALID_DOMAIN", None),
    # Unrecognized/new vendor grades must never pass as verified.
    ("shiny_new_grade", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_email_status_mapping_table(raw, expected):
    assert map_email_status(raw) == expected


def test_invalid_emails_are_dropped_entirely():
    """INVALID / INVALID_DOMAIN entries never surface — a known-invalid
    address must not appear even marked 'unknown'."""
    payload = finished_payload(contact_info={
        "work_emails": [
            {"email": "jane@acme.com", "status": "DELIVERABLE"},
            {"email": "bogus@acme.com", "status": "INVALID"},
            {"email": "jane@gone-domain.com", "status": "INVALID_DOMAIN"},
        ],
        "personal_emails": [
            {"email": "dead@gmail.com", "status": "INVALID"},
        ],
        "phones": [],
    })
    handler, _ = start_then_finish(payload)
    adapter, _ = make_adapter(handler)

    result = adapter.resolve(INPUT, ["work_email", "personal_email"])
    assert [e.address for e in result.emails] == ["jane@acme.com"]


def test_email_grade_mapping_and_list_price_per_type():
    payload = finished_payload(contact_info={
        "work_emails": [
            {"email": "jane@acme.com", "status": "DELIVERABLE"},
            {"email": "j.doe@acme.com", "status": "HIGH_PROBABILITY"},
        ],
        "personal_emails": [
            {"email": "jane@gmail.com", "status": "CATCH_ALL"},
        ],
        "phones": [],
    })
    handler, _ = start_then_finish(payload)
    adapter, _ = make_adapter(handler)

    result = adapter.resolve(INPUT, ["work_email", "personal_email"])

    by_addr = {e.address: e for e in result.emails}
    assert by_addr["jane@acme.com"].type == "work"
    assert by_addr["jane@acme.com"].status == "verified"
    assert by_addr["jane@acme.com"].cost_credits == 1.0
    assert by_addr["j.doe@acme.com"].status == "risky"
    assert by_addr["jane@gmail.com"].type == "personal"
    assert by_addr["jane@gmail.com"].status == "unknown"
    assert by_addr["jane@gmail.com"].cost_credits == 3.0  # personal list price


def test_personal_emails_array_is_personal_by_definition():
    payload = finished_payload(contact_info={
        "work_emails": [],
        "personal_emails": [{"email": "jd@icloud.com",
                             "status": "DELIVERABLE"}],
        "phones": [],
    })
    handler, _ = start_then_finish(payload)
    adapter, _ = make_adapter(handler)

    result = adapter.resolve(INPUT, ["personal_email"])

    personal = [e for e in result.emails if e.type == "personal"]
    assert [e.address for e in personal] == ["jd@icloud.com"]


def test_cost_credits_surfaces_as_meta_billed_credits():
    """v2's top-level cost.credits is the vendor's authoritative JOB-LEVEL
    billed figure — exactly what waterfall._attempt_cost reads via the
    documented optional meta key 'billed_credits'."""
    payload = finished_payload(cost={"credits": 2})
    handler, _ = start_then_finish(payload)
    adapter, _ = make_adapter(handler)

    result = adapter.resolve(INPUT, ["work_email"])
    assert result.meta["billed_credits"] == 2.0


def test_legacy_flat_billing_keys_still_read_as_harmless_fallback():
    """cost.credits is authoritative, but the tolerant flat-key fallback
    stays (harmless): a payload carrying only credits_used still books."""
    payload = finished_payload()
    payload["credits_used"] = 2.0
    handler, _ = start_then_finish(payload)
    adapter, _ = make_adapter(handler)

    result = adapter.resolve(INPUT, ["work_email"])
    assert result.meta["billed_credits"] == 2.0


def test_meta_billed_credits_omitted_when_payload_has_no_billing_field():
    """No cost object and no fallback key -> the key is OMITTED (never
    invented), so the waterfall falls back to list price per call."""
    handler, _ = start_then_finish(finished_payload())
    adapter, _ = make_adapter(handler)

    result = adapter.resolve(INPUT, ["work_email"])
    assert "billed_credits" not in result.meta


def test_bare_credits_key_is_not_read_as_billed():
    """A job-level 'credits' could as easily be a remaining BALANCE;
    misreading it would book the whole wallet against one attempt. Only
    cost.credits (nested, unambiguous) and the explicit billed-flavored
    flat keys count."""
    payload = finished_payload()
    payload["credits"] = 4999
    handler, _ = start_then_finish(payload)
    adapter, _ = make_adapter(handler)

    result = adapter.resolve(INPUT, ["work_email"])
    assert "billed_credits" not in result.meta


def test_phones_keep_region_out_of_the_hit_but_in_raw_response():
    """v2 gives {number, region} only. The PhoneHit shape has no region
    slot — the number rides as-is and region survives in raw_response for
    the audit trail."""
    payload = finished_payload(contact_info={
        "work_emails": [],
        "personal_emails": [],
        "phones": [{"number": "+33612345678", "region": "FR"}],
    })
    handler, _ = start_then_finish(payload)
    adapter, _ = make_adapter(handler)

    result = adapter.resolve(INPUT, ["mobile"])
    (phone,) = result.phones
    assert phone.number == "+33612345678"
    assert phone.type == "mobile"
    assert phone.status == "unknown"
    assert phone.dnc_flag is None
    assert phone.cost_credits == 10.0
    raw_phone = result.meta["raw_response"]["data"][0]["contact_info"]["phones"][0]
    assert raw_phone["region"] == "FR"


def test_empty_result_payload_yields_empty_hits_not_errors():
    """A finished job that found nothing is a legitimate MISS, not a
    failure -- the waterfall's next leg handles it."""
    handler, _ = start_then_finish({"status": "FINISHED", "data": []})
    adapter, _ = make_adapter(handler)

    result = adapter.resolve(INPUT, ["work_email"])
    assert result.emails == []
    assert result.phones == []
    assert result.profile == {}
    assert result.company == {}


# -- to_payload ------------------------------------------------------------------------


def test_to_payload_excludes_raw_response_and_is_json_safe():
    """to_payload() is what travels to the extension: the raw vendor
    payload stays behind (it goes to prospector.attempts), and everything
    else must survive json.dumps unaided."""
    handler, _ = start_then_finish(finished_payload())
    adapter, _ = make_adapter(handler)

    result = adapter.resolve(INPUT, ["work_email", "mobile"])
    payload = result.to_payload()

    assert "raw_response" not in payload["meta"]
    assert payload["meta"]["request_id"] == "enr_123"
    assert payload["emails"][0]["address"] == "jane@acme.com"
    assert payload["phones"][0]["dnc_flag"] is None
    jsonlib.dumps(payload)  # raises if anything non-JSON slipped through
    # ...and the audit copy on the result itself is untouched.
    assert "raw_response" in result.meta


# -- transport retry posture --------------------------------------------------------------


def test_429_on_start_honors_retry_after_then_succeeds():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST" and calls.count("POST") == 1:
            return httpx.Response(429, headers={"Retry-After": "7"},
                                  json={"message": "rate limited"})
        if request.method == "POST":
            return started_response()
        return httpx.Response(200, json=finished_payload())

    adapter, sleeps = make_adapter(handler)
    result = adapter.resolve(INPUT, ["work_email"])

    assert result.meta["request_id"] == "enr_123"
    # First sleep is the honored Retry-After (exactly 7.0, no jitter);
    # the second is the poll interval.
    assert sleeps == [7.0, 3.0]


def test_retry_after_is_capped_at_30s():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3600"},
                                  json={})
        return started_response() if len(calls) == 2 else httpx.Response(
            200, json=finished_payload())

    adapter, sleeps = make_adapter(handler)
    adapter.resolve(INPUT, ["work_email"])
    assert sleeps[0] == 30.0


def test_500_exhausts_attempts_then_raises_provider_error():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"message": "boom"})

    adapter, sleeps = make_adapter(handler)
    with pytest.raises(ProviderError) as excinfo:
        adapter.resolve(INPUT, ["work_email"])

    assert len(calls) == MAX_ATTEMPTS
    assert len(sleeps) == MAX_ATTEMPTS - 1
    assert "500" in str(excinfo.value)


def test_401_fails_fast_with_key_hint():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"message": "unauthorized"})

    adapter, sleeps = make_adapter(handler)
    with pytest.raises(ProviderError) as excinfo:
        adapter.resolve(INPUT, ["work_email"])

    assert len(calls) == 1  # never retried: a bad key cannot fix itself
    assert sleeps == []
    assert "check FULLENRICH_API_KEY" in str(excinfo.value)


# -- key hygiene ------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 500])
def test_key_never_appears_in_exceptions_or_logs(status, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        # Prove the header went out (auth works)...
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        return httpx.Response(status, json={"message": "nope"})

    adapter, _ = make_adapter(handler)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProviderError) as excinfo:
            adapter.resolve(INPUT, ["work_email"])

    # ...but it never comes back in the error or the logs.
    assert API_KEY not in str(excinfo.value)
    assert API_KEY not in repr(excinfo.value)
    assert API_KEY not in caplog.text


def test_key_never_appears_on_transport_failure(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    adapter, _ = make_adapter(handler)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProviderError) as excinfo:
            adapter.resolve(INPUT, ["work_email"])

    assert API_KEY not in str(excinfo.value)
    assert API_KEY not in caplog.text
    # `raise ... from None`: the httpx exception (which carries the request,
    # Authorization header included) must not ride along as the cause.
    assert excinfo.value.__cause__ is None


def test_blank_key_is_rejected_at_construction():
    with pytest.raises(ValueError):
        FullEnrichAdapter("")


# -- get_balance ------------------------------------------------------------------------
# PROVEN LIVE returning real balances -- implementation untouched.


@pytest.mark.parametrize("payload,expected", [
    ({"credits": 123}, 123.0),
    ({"balance": 42.5}, 42.5),
    ({"remaining": 7}, 7.0),
    ({"credits": True}, None),   # bool is not a balance
    ({"unexpected": 1}, None),
])
def test_get_balance_parses_known_keys(payload, expected):
    adapter, _ = make_adapter(
        lambda request: httpx.Response(200, json=payload))
    assert adapter.get_balance() == expected


def test_get_balance_returns_none_on_any_error_never_raises():
    """/status shows 'balance unknown' instead of failing -- so ANY error
    here (HTTP, transport, non-JSON) is a None, not an exception."""
    adapter, _ = make_adapter(lambda request: httpx.Response(500))
    assert adapter.get_balance() is None

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    adapter2, _ = make_adapter(boom)
    assert adapter2.get_balance() is None

    adapter3, _ = make_adapter(
        lambda request: httpx.Response(200, text="not json"))
    assert adapter3.get_balance() is None


# -- registry --------------------------------------------------------------------------


class StubDB:
    """Just enough of prospector.database.Database for build_registry."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.seen_sql: list[str] = []

    def query(self, sql: str, params=None) -> list[dict]:
        self.seen_sql.append(sql)
        return self.rows


def cfg_with_key(key: str) -> SimpleNamespace:
    return SimpleNamespace(fullenrich_api_key=key)


def test_registry_builds_fullenrich_from_enabled_row():
    db = StubDB([{"id": "fullenrich", "kind": "lookup", "enabled": True,
                  "config": {}}])
    registry = build_registry(db, cfg_with_key(API_KEY))

    assert set(registry) == {"fullenrich"}
    adapter = registry["fullenrich"]
    assert isinstance(adapter, FullEnrichAdapter)
    assert isinstance(adapter, ProviderAdapter)
    assert adapter.supports == frozenset(FIELDS)
    # Only ENABLED rows are ever read -- disabling a vendor is an UPDATE,
    # not a deploy, and this query is where that switch takes effect.
    assert "WHERE enabled" in db.seen_sql[0]


def test_registry_excludes_fullenrich_when_key_is_blank(caplog):
    """Enabled row + blank FULLENRICH_API_KEY -> excluded with a warning.
    The empty registry is what makes /enrich 503 rather than every request
    failing mid-flight."""
    db = StubDB([{"id": "fullenrich", "kind": "lookup", "enabled": True,
                  "config": {}}])
    with caplog.at_level(logging.WARNING):
        registry = build_registry(db, cfg_with_key(""))

    assert registry == {}
    assert "'fullenrich'" in caplog.text
    assert "API key is empty" in caplog.text


def test_registry_skips_unknown_enabled_ids_with_warning(caplog):
    """Only fullenrich has a registered adapter today. An enabled row whose
    id has no adapter (e.g. an operator adding a 'altenrich' row early) gets a
    warning and a skip, never a crash at boot."""
    db = StubDB([
        {"id": "fullenrich", "kind": "lookup", "enabled": True, "config": {}},
        {"id": "altenrich", "kind": "lookup", "enabled": True, "config": {}},
    ])
    with caplog.at_level(logging.WARNING):
        registry = build_registry(db, cfg_with_key(API_KEY))

    assert set(registry) == {"fullenrich"}
    assert "'altenrich'" in caplog.text
    assert "no adapter" in caplog.text


def test_registry_parses_jsonb_config_delivered_as_text():
    """psycopg2 can hand jsonb back as a string depending on adapters --
    a text config must not crash registry construction."""
    db = StubDB([{"id": "fullenrich", "kind": "lookup", "enabled": True,
                  "config": '{"flag": true}'}])
    registry = build_registry(db, cfg_with_key(API_KEY))
    assert set(registry) == {"fullenrich"}
