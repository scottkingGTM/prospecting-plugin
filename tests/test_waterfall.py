"""Unit tests for prospector.waterfall -- the waterfall runner.

No live network or DB: the Database is replaced with a StubDb that records
every attempt insert (each in its own cursor() block, mirroring the
per-leg-transaction discipline) and answers the prior-outcome lookup from a
scripted dict; providers are FakeAdapters that either return a canned
ContactResult or raise ProviderError, recording every resolve() call so
"never called" is directly assertable.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from decimal import Decimal

import pytest

from prospector import waterfall
from prospector.jobs import JobRunError
from prospector.providers import (
    ContactResult,
    EmailHit,
    EnrichInput,
    PhoneHit,
    ProviderAdapter,
    ProviderError,
)
from prospector.waterfall import CACHE_PROVIDER_ID, load_waterfalls, make_runner


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubCursor:
    def __init__(self, db: "StubDb") -> None:
        self.db = db

    def execute(self, sql: str, params=None) -> None:
        if "INSERT INTO prospector.attempts" in sql:
            if self.db.fail_attempt_inserts:
                raise RuntimeError("attempts table is on fire")
            row = dict(params)
            raw = row.get("raw_response")
            row["raw_response"] = json.loads(raw) if raw is not None else None
            self.db.attempts.append(row)
            return
        raise AssertionError(f"unexpected execute: {sql}")


class StubDb:
    """Records attempt inserts; scripts the prior-outcome lookup per field."""

    def __init__(self) -> None:
        self.attempts: list[dict] = []
        self.prior: dict[str, str] = {}   # field -> 'found' | 'not_found'
        self.fail_attempt_inserts = False
        self.waterfall_rows: list[dict] = []

    @contextmanager
    def cursor(self):
        yield StubCursor(self)

    def query(self, sql: str, params=None) -> list[dict]:
        if "FROM prospector.waterfalls" in sql:
            return [dict(r) for r in self.waterfall_rows]
        if "FROM prospector.attempts" in sql:
            norm_url, field = params
            assert norm_url == URL  # the lookup keys on the job's profile
            status = self.prior.get(field)
            return [{"status": status}] if status else []
        raise AssertionError(f"unexpected query: {sql}")


class FakeAdapter(ProviderAdapter):
    """Scripted adapter: resolve() pops the next item off `script` -- a
    ContactResult to return or an Exception to raise. The last item repeats
    so a single-entry script serves any number of calls."""

    kind = "lookup"
    supports = frozenset({"work_email", "mobile", "personal_email"})

    def __init__(self, provider_id: str, script: list, cost_value: float = 1.0):
        self.id = provider_id
        self._script = list(script)
        self._cost = float(cost_value)
        self.calls: list[tuple[EnrichInput, tuple[str, ...]]] = []

    def cost(self, fields) -> float:
        return self._cost

    def resolve(self, inp: EnrichInput, fields) -> ContactResult:
        self.calls.append((inp, tuple(fields)))
        item = self._script[0] if len(self._script) == 1 else self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def email_result(provider: str, address: str = "jane@acmepest.com",
                 status: str = "verified", cost: float = 1.0,
                 etype: str = "work", profile: dict | None = None,
                 raw: dict | None = None) -> ContactResult:
    return ContactResult(
        emails=[EmailHit(address, etype, status, provider, cost)],
        phones=[],
        profile=profile or {},
        company={},
        meta={"provider_id": provider,
              "raw_response": raw if raw is not None else {"vendor": provider}},
    )


def phone_result(provider: str, number: str = "+18015551234",
                 status: str = "verified", cost: float = 10.0,
                 dnc: bool | None = None) -> ContactResult:
    return ContactResult(
        emails=[],
        phones=[PhoneHit(number, "mobile", status, dnc, provider, cost)],
        profile={},
        company={},
        meta={"provider_id": provider, "raw_response": {"vendor": provider}},
    )


def miss_result(provider: str) -> ContactResult:
    return ContactResult(
        emails=[], phones=[], profile={}, company={},
        meta={"provider_id": provider, "raw_response": {"vendor": provider}},
    )


def leg(provider_id: str, position: int = 1, stop_on: str = "verified",
        max_cost: float = 5.0) -> dict:
    return {"field": "work_email", "position": position,
            "provider_id": provider_id, "stop_on": stop_on,
            "max_cost": max_cost}


URL = "linkedin.com/in/jane-doe"

JOB = {
    "id": "job-1",
    "norm_linkedin_url": URL,
    "fields": ["work_email"],
    "input": {"first_name": "Jane", "last_name": "Doe",
              "company_domain": "acmepest.com"},
}


def job_for(fields: list[str]) -> dict:
    return dict(JOB, fields=list(fields))


@pytest.fixture()
def db() -> StubDb:
    return StubDb()


def statuses(db: StubDb) -> list[tuple[str, str, str]]:
    return [(a["provider_id"], a["field"], a["status"]) for a in db.attempts]


# ---------------------------------------------------------------------------
# load_waterfalls
# ---------------------------------------------------------------------------


def test_load_waterfalls_groups_by_field_and_coerces_numeric(db):
    db.waterfall_rows = [
        {"field": "work_email", "position": 1, "provider_id": "fullenrich",
         "stop_on": "verified", "max_cost": Decimal("1.0")},
        {"field": "work_email", "position": 2, "provider_id": "altenrich",
         "stop_on": "any", "max_cost": Decimal("2.5")},
        {"field": "mobile", "position": 1, "provider_id": "fullenrich",
         "stop_on": "verified", "max_cost": Decimal("10")},
    ]
    loaded = load_waterfalls(db)

    assert list(loaded) == ["work_email", "mobile"]
    assert [l["provider_id"] for l in loaded["work_email"]] == ["fullenrich", "altenrich"]
    assert loaded["work_email"][1]["max_cost"] == 2.5      # Decimal -> float
    assert isinstance(loaded["mobile"][0]["max_cost"], float)


# ---------------------------------------------------------------------------
# stop_on semantics
# ---------------------------------------------------------------------------


def test_risky_hit_falls_through_to_next_leg_and_both_hits_kept(db):
    p1 = FakeAdapter("p1", [email_result("p1", "jane@guess.com", status="risky")])
    p2 = FakeAdapter("p2", [email_result("p2", "jane@acmepest.com",
                                         status="verified", cost=2.0)],
                     cost_value=2.0)
    runner = make_runner(db, {"p1": p1, "p2": p2},
                         {"work_email": [leg("p1", 1), leg("p2", 2)]})

    payload, billed = runner(JOB)

    assert len(p1.calls) == 1 and len(p2.calls) == 1
    assert statuses(db) == [("p1", "work_email", "found"),
                            ("p2", "work_email", "found")]
    addresses = [e["address"] for e in payload["emails"]]
    assert addresses == ["jane@guess.com", "jane@acmepest.com"]  # both kept
    assert payload["fields_found"] == ["work_email"]
    assert payload["fields_missed"] == []
    assert billed == 3.0  # 1.0 risky + 2.0 verified


def test_verified_hit_at_leg1_stops_the_walk(db):
    p1 = FakeAdapter("p1", [email_result("p1", status="verified")])
    p2 = FakeAdapter("p2", [email_result("p2")])
    runner = make_runner(db, {"p1": p1, "p2": p2},
                         {"work_email": [leg("p1", 1), leg("p2", 2)]})

    payload, billed = runner(JOB)

    assert len(p1.calls) == 1
    assert p2.calls == []  # never called: stop_on satisfied at leg 1
    assert statuses(db) == [("p1", "work_email", "found")]
    assert billed == 1.0
    assert payload["fields_found"] == ["work_email"]


# ---------------------------------------------------------------------------
# never re-buy a known miss
# ---------------------------------------------------------------------------


def test_known_miss_within_window_skips_field_for_free(db):
    db.prior["work_email"] = "not_found"
    p1 = FakeAdapter("p1", [email_result("p1")])
    runner = make_runner(db, {"p1": p1}, {"work_email": [leg("p1")]})

    payload, billed = runner(JOB)

    assert p1.calls == []  # zero adapter calls for the cached field
    assert billed == 0.0
    assert len(db.attempts) == 1
    row = db.attempts[0]
    assert row["provider_id"] == CACHE_PROVIDER_ID
    assert row["status"] == "not_found"
    assert row["cost_credits"] == 0.0
    assert row["raw_response"] == {"cached_miss": True}
    assert payload["fields_missed"] == ["work_email"]
    assert payload["fields_found"] == []


def test_prior_found_reruns_legs_gracefully(db):
    # Per-field result payloads aren't stored historically (jobs.result is
    # per-job), so a prior 'found' simply re-runs the legs -- documented.
    db.prior["work_email"] = "found"
    p1 = FakeAdapter("p1", [email_result("p1")])
    runner = make_runner(db, {"p1": p1}, {"work_email": [leg("p1")]})

    payload, billed = runner(JOB)

    assert len(p1.calls) == 1
    assert payload["fields_found"] == ["work_email"]
    assert billed == 1.0


def test_prior_outcome_sql_excludes_cache_rows():
    """A cached_miss row is an ECHO of a vendor answer, not a new one -- if
    it anchored the window, every free replay would re-arm the 30 days and a
    miss could stay 'known' forever without any vendor ever being asked
    again."""
    assert "a.provider_id != 'cache'" in waterfall._PRIOR_OUTCOME_SQL
    assert "interval '30 days'" in waterfall._PRIOR_OUTCOME_SQL


class WindowStubDb(StubDb):
    """StubDb whose prior-outcome lookup emulates the REAL SQL's
    predicates over scripted attempt rows: status in (found, not_found),
    provider_id != 'cache', created_at inside the 30-day window. Rows are
    {provider_id, status, days_ago}."""

    def __init__(self) -> None:
        super().__init__()
        self.prior_rows: list[dict] = []

    def query(self, sql: str, params=None) -> list[dict]:
        if "FROM prospector.attempts" in sql:
            live = [r for r in self.prior_rows
                    if r["status"] in ("found", "not_found")
                    and r["provider_id"] != "cache"
                    and r["days_ago"] < 30]
            live.sort(key=lambda r: r["days_ago"])  # most recent first
            return [{"status": live[0]["status"]}] if live else []
        return super().query(sql, params)


def test_cached_miss_does_not_rearm_the_rebuy_window():
    """Day 1: vendor says not_found. Day 29: a free cached_miss echo row
    lands. Day 32 (31+ days after the VENDOR miss): the vendor row is
    outside the window and the cache row never counts -- so there is no
    prior, and the runner asks the vendor again."""
    db = WindowStubDb()
    db.prior_rows = [
        {"provider_id": "p1", "status": "not_found", "days_ago": 31},
        {"provider_id": CACHE_PROVIDER_ID, "status": "not_found", "days_ago": 2},
    ]
    p1 = FakeAdapter("p1", [email_result("p1")])
    runner = make_runner(db, {"p1": p1}, {"work_email": [leg("p1")]})

    payload, billed = runner(JOB)

    assert len(p1.calls) == 1  # the vendor WAS asked again
    assert payload["fields_found"] == ["work_email"]
    assert billed == 1.0


def test_cached_miss_inside_vendor_window_still_skips_for_free():
    """Control: while the VENDOR miss is inside the window, the field is
    still skipped for free -- the cache exclusion only stops the echo
    rows from extending it."""
    db = WindowStubDb()
    db.prior_rows = [
        {"provider_id": "p1", "status": "not_found", "days_ago": 10},
        {"provider_id": CACHE_PROVIDER_ID, "status": "not_found", "days_ago": 2},
    ]
    p1 = FakeAdapter("p1", [email_result("p1")])
    runner = make_runner(db, {"p1": p1}, {"work_email": [leg("p1")]})

    payload, billed = runner(JOB)

    assert p1.calls == []  # no vendor call
    assert billed == 0.0
    assert payload["fields_missed"] == ["work_email"]


# ---------------------------------------------------------------------------
# error handling: continue to the next leg, fail only on total outage
# ---------------------------------------------------------------------------


def test_provider_error_at_leg1_then_hit_at_leg2_succeeds(db):
    p1 = FakeAdapter("p1", [ProviderError("FullEnrich 502 after retries")])
    p2 = FakeAdapter("p2", [email_result("p2", cost=2.0)], cost_value=2.0)
    runner = make_runner(db, {"p1": p1, "p2": p2},
                         {"work_email": [leg("p1", 1), leg("p2", 2)]})

    payload, billed = runner(JOB)

    assert statuses(db) == [("p1", "work_email", "error"),
                            ("p2", "work_email", "found")]
    assert db.attempts[0]["cost_credits"] == 0.0
    assert db.attempts[0]["raw_response"] == {
        "error": "FullEnrich 502 after retries"}
    assert payload["fields_found"] == ["work_email"]
    assert billed == 2.0


def test_all_legs_error_on_all_fields_raises_jobrunerror_billed_zero(db):
    p1 = FakeAdapter("p1", [ProviderError("down")])
    p2 = FakeAdapter("p2", [ProviderError("also down")])
    legs = {
        "work_email": [leg("p1", 1), leg("p2", 2)],
        "mobile": [leg("p1", 1), leg("p2", 2)],
    }
    runner = make_runner(db, {"p1": p1, "p2": p2}, legs)

    with pytest.raises(JobRunError) as excinfo:
        runner(job_for(["work_email", "mobile"]))

    assert excinfo.value.billed == 0.0
    # Every leg still left its audit row before the job failed.
    assert statuses(db) == [
        ("p1", "work_email", "error"), ("p2", "work_email", "error"),
        ("p1", "mobile", "error"), ("p2", "mobile", "error"),
    ]


def test_billed_then_fail_is_partial_success_not_failure(db):
    # work_email finds (bills 1.0); mobile's every leg errors. Partial
    # success = success: no raise, mobile lands in fields_missed.
    p1 = FakeAdapter("p1", [email_result("p1")])
    p_down = FakeAdapter("p_down", [ProviderError("mobile vendor outage")])
    legs = {"work_email": [leg("p1")], "mobile": [leg("p_down")]}
    runner = make_runner(db, {"p1": p1, "p_down": p_down}, legs)

    payload, billed = runner(job_for(["work_email", "mobile"]))

    assert billed == 1.0
    assert payload["fields_found"] == ["work_email"]
    assert payload["fields_missed"] == ["mobile"]
    assert statuses(db) == [("p1", "work_email", "found"),
                            ("p_down", "mobile", "error")]


def test_vendor_not_found_is_an_answer_so_errors_elsewhere_dont_fail_job(db):
    # work_email: vendor authoritatively says no; mobile: vendor errors.
    # NOT all fields errored -> success with both fields missed.
    p1 = FakeAdapter("p1", [miss_result("p1")])
    p_down = FakeAdapter("p_down", [ProviderError("outage")])
    legs = {"work_email": [leg("p1")], "mobile": [leg("p_down")]}
    runner = make_runner(db, {"p1": p1, "p_down": p_down}, legs)

    payload, billed = runner(job_for(["work_email", "mobile"]))

    assert billed == 0.0
    assert payload["fields_found"] == []
    assert payload["fields_missed"] == ["work_email", "mobile"]


# ---------------------------------------------------------------------------
# max_cost guard
# ---------------------------------------------------------------------------


def test_leg_over_max_cost_is_skipped_with_rejected_row(db):
    pricey = FakeAdapter("pricey", [email_result("pricey")], cost_value=3.0)
    runner = make_runner(db, {"pricey": pricey},
                         {"work_email": [leg("pricey", max_cost=2.0)]})

    payload, billed = runner(JOB)

    assert pricey.calls == []  # resolve never reached
    assert statuses(db) == [("pricey", "work_email", "rejected_over_max_cost")]
    assert db.attempts[0]["cost_credits"] == 0.0
    assert billed == 0.0
    assert payload["fields_missed"] == ["work_email"]  # a skip, not an outage


# ---------------------------------------------------------------------------
# bill per CALL, not per hit
# ---------------------------------------------------------------------------


def test_three_hit_mobile_response_books_exactly_one_call(db):
    """One 10-credit mobile call answering with mobile+direct+HQ used to
    sum the hits' display prices and book 30 on a 10-credit reservation.
    Attempt accounting is per CALL: adapter.cost(['mobile']) once."""
    result = ContactResult(
        emails=[],
        phones=[
            PhoneHit("+18015551234", "mobile", "verified", None, "p1", 10.0),
            PhoneHit("+18015551235", "direct", "verified", None, "p1", 10.0),
            PhoneHit("+18015551236", "hq", "verified", None, "p1", 10.0),
        ],
        profile={}, company={},
        meta={"provider_id": "p1", "raw_response": {"vendor": "p1"}},
    )
    p1 = FakeAdapter("p1", [result], cost_value=10.0)
    runner = make_runner(db, {"p1": p1},
                         {"mobile": [dict(leg("p1", max_cost=10.0),
                                          field="mobile")]})

    payload, billed = runner(job_for(["mobile"]))

    assert billed == 10.0                          # one call, one price
    assert db.attempts[0]["cost_credits"] == 10.0  # ledger agrees
    # The hits themselves survive with their display-only per-hit info.
    assert len(payload["phones"]) == 3


def test_multi_email_work_response_books_one_credit(db):
    result = ContactResult(
        emails=[
            EmailHit("jane@acmepest.com", "work", "verified", "p1", 1.0),
            EmailHit("j.doe@acmepest.com", "work", "risky", "p1", 1.0),
        ],
        phones=[], profile={}, company={},
        meta={"provider_id": "p1", "raw_response": {"vendor": "p1"}},
    )
    p1 = FakeAdapter("p1", [result], cost_value=1.0)
    runner = make_runner(db, {"p1": p1}, {"work_email": [leg("p1")]})

    payload, billed = runner(JOB)

    assert billed == 1.0
    assert db.attempts[0]["cost_credits"] == 1.0
    assert len(payload["emails"]) == 2


def test_vendor_billed_credits_meta_wins_over_list_price(db):
    """When the adapter surfaces the vendor's job-level billed figure
    (meta['billed_credits']), that is what the attempt books -- not the
    list price, not the hit sum."""
    result = email_result("p1")
    result.meta["billed_credits"] = 0.5
    p1 = FakeAdapter("p1", [result], cost_value=1.0)
    runner = make_runner(db, {"p1": p1}, {"work_email": [leg("p1")]})

    payload, billed = runner(JOB)

    assert billed == 0.5
    assert db.attempts[0]["cost_credits"] == 0.5


def test_vendor_billed_credits_is_capped_at_leg_max_cost(db):
    """A vendor-reported figure above the leg's max_cost is capped: the
    reservation was sized against max_cost, and a runaway vendor figure
    must not blow through it."""
    result = email_result("p1")
    result.meta["billed_credits"] = 50.0
    p1 = FakeAdapter("p1", [result], cost_value=1.0)
    runner = make_runner(db, {"p1": p1},
                         {"work_email": [leg("p1", max_cost=2.0)]})

    payload, billed = runner(JOB)

    assert billed == 2.0
    assert db.attempts[0]["cost_credits"] == 2.0


# ---------------------------------------------------------------------------
# attempt-insert failures never kill the job
# ---------------------------------------------------------------------------


def test_attempt_insert_failure_logs_and_job_continues(db, caplog):
    db.fail_attempt_inserts = True
    p1 = FakeAdapter("p1", [email_result("p1")])
    runner = make_runner(db, {"p1": p1}, {"work_email": [leg("p1")]})

    with caplog.at_level("WARNING"):
        payload, billed = runner(JOB)

    assert db.attempts == []  # nothing landed...
    assert payload["fields_found"] == ["work_email"]  # ...but the job lived
    assert billed == 1.0
    assert any("attempt insert failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# dnc_flag and payload shape
# ---------------------------------------------------------------------------


def test_dnc_flag_from_phone_hit_lands_on_attempt_row(db):
    p1 = FakeAdapter("p1", [phone_result("p1", dnc=True)], cost_value=10.0)
    runner = make_runner(db, {"p1": p1},
                         {"mobile": [dict(leg("p1", max_cost=10.0),
                                          field="mobile")]})

    payload, billed = runner(job_for(["mobile"]))

    assert statuses(db) == [("p1", "mobile", "found")]
    assert db.attempts[0]["dnc_flag"] is True
    assert billed == 10.0
    assert payload["phones"][0]["number"] == "+18015551234"
    assert payload["fields_found"] == ["mobile"]


def test_payload_merges_hits_and_excludes_raw_response(db):
    p1 = FakeAdapter("p1", [email_result(
        "p1", profile={"firstname": "Jane", "title": ""},
        raw={"secret_vendor_blob": True})])
    p2 = FakeAdapter("p2", [phone_result("p2")], cost_value=10.0)
    legs = {"work_email": [leg("p1")],
            "mobile": [dict(leg("p2", max_cost=10.0), field="mobile")]}
    runner = make_runner(db, {"p1": p1, "p2": p2}, legs)

    payload, billed = runner(job_for(["work_email", "mobile"]))

    assert payload["fields_requested"] == ["work_email", "mobile"]
    assert payload["fields_found"] == ["work_email", "mobile"]
    assert payload["fields_missed"] == []
    assert payload["profile"] == {"firstname": "Jane"}  # empties dropped
    assert [e["provider"] for e in payload["emails"]] == ["p1"]
    assert [p["provider"] for p in payload["phones"]] == ["p2"]
    # raw vendor payloads live in attempts only, never in the job result
    assert "secret_vendor_blob" not in json.dumps(payload)
    assert billed == 11.0
