"""Unit tests for prospector.jobs -- the enrichment job queue.

No live network or DB: the Database is replaced with a StubDb whose
in-memory jobs dict honors the two contracts jobs.py leans on --
cursor() = one transaction (staged writes commit on clean exit, vanish on
exception) and the jobs_inflight_one_per_profile PARTIAL unique index
(unique on norm_linkedin_url only while state is queued/running). The
budget module -- being written concurrently against a pinned interface --
is replaced with a call-recording fake injected via sys.modules, which
works because jobs.py imports it lazily inside each function.
"""

from __future__ import annotations

import itertools
import json
import sys
import threading
import time
import types
from contextlib import contextmanager
from types import SimpleNamespace

import psycopg2.errors
import pytest

import prospector
from prospector import jobs as jobs_mod
from prospector.jobs import (
    MAX_CONCURRENT_JOBS,
    STALE_AFTER_SECONDS,
    InFlightConflict,
    JobRunError,
    create_job,
    expire_stale,
    get_job,
    run_job_async,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubCursor:
    """One transaction's worth of work. execute() computes results
    immediately (RETURNING must be fetchable inside the block) but stages
    every MUTATION as a closure; StubDb.cursor() applies the stage on
    clean exit and discards it on exception -- the same commit/rollback
    contract as database.Database.cursor()."""

    def __init__(self, db: "StubDb") -> None:
        self.db = db
        self.staged: list = []
        self._results: list[dict] = []
        self.rowcount = -1

    def execute(self, sql: str, params=None) -> None:
        db = self.db
        params = params or {}

        if "INSERT INTO prospector.jobs" in sql:
            # The partial unique index: fires only against IN-FLIGHT rows.
            with db.mutex:
                for job in db.jobs.values():
                    if (job["norm_linkedin_url"] == params["norm_url"]
                            and job["state"] in ("queued", "running")):
                        raise psycopg2.errors.UniqueViolation(
                            "duplicate key value violates unique constraint "
                            '"jobs_inflight_one_per_profile"'
)
            row = {
                "id": f"job-{next(db.next_id)}",
                "rep_id": params["rep_id"],
                "kind": "enrich",
                "norm_linkedin_url": params["norm_url"],
                "fields": list(params["fields"]),
                "state": "queued",
                "input": json.loads(params["input"]),
                "result": None,
                "credits_reserved": params["credits_reserved"],
                "credits_billed": 0,
                "created_at": time.time(),
                "finished_at": None,
            }
            self._results = [dict(row)]
            self.staged.append(lambda r=row: db.jobs.__setitem__(r["id"], r))
            return

        if "SET state = 'running'" in sql:
            with db.mutex:
                job = db.jobs.get(params["job_id"])
                if job is None or job["state"] != "queued":
                    self._results = []
                    return
                updated = dict(job, state="running")
            self._results = [dict(updated)]
            self.staged.append(lambda u=updated: db.jobs.__setitem__(u["id"], u))
            return

        if "SET state = %(state)s" in sql:  # _FINISH_SQL (done/failed)
            # The state='running' guard (a hardening pass): a job
            # expired mid-flight must not be resurrected by a late finish.
            with db.mutex:
                job = db.jobs.get(params["job_id"])
                running = job is not None and job["state"] == "running"
            self.rowcount = 1 if running else 0
            self._results = []
            if not running:
                return

            def apply_finish(p=dict(params)) -> None:
                job = db.jobs[p["job_id"]]
                job["state"] = p["state"]
                job["result"] = json.loads(p["result"])
                job["finished_at"] = time.time()
            self.staged.append(apply_finish)
            return

        if "SET state = 'expired'" in sql:
            # now() - make_interval(...): the stub stands in for the DB
            # clock with time.time(); tests age rows by editing created_at.
            cutoff = time.time() - params["stale"]
            with db.mutex:
                stale = [j for j in db.jobs.values()
                         if j["state"] in ("queued", "running")
                         and j["created_at"] < cutoff]
            self._results = [{"id": j["id"]} for j in stale]

            def apply_expire(rows=stale) -> None:
                for job in rows:
                    job["state"] = "expired"
                    job["finished_at"] = time.time()
            self.staged.append(apply_expire)
            return

        raise AssertionError(f"unexpected execute: {sql}")

    def fetchone(self):
        return self._results[0] if self._results else None

    def fetchall(self):
        return list(self._results)


class StubDb:
    """In-memory prospector.jobs (+ a reps roster for the holder join)."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.reps = {
            1: {"id": 1, "display_name": "TJ"},
            2: {"id": 2, "display_name": "Nick"},
        }
        self.commits = 0
        self.rollbacks = 0
        self.mutex = threading.Lock()
        self.next_id = itertools.count(1)

    @contextmanager
    def cursor(self):
        cur = StubCursor(self)
        try:
            yield cur
        except BaseException:
            self.rollbacks += 1  # staged writes discarded, never applied
            raise
        with self.mutex:
            for apply in cur.staged:
                apply()
            self.commits += 1

    def query(self, sql: str, params=None) -> list[dict]:
        params = params or {}
        with self.mutex:
            if "JOIN prospector.reps" in sql:  # _HOLDER_SQL
                for job in self.jobs.values():
                    if (job["norm_linkedin_url"] == params["norm_url"]
                            and job["state"] in ("queued", "running")):
                        rep = self.reps[job["rep_id"]]
                        return [{"job_id": job["id"],
                                 "display_name": rep["display_name"]}]
                return []
            if "FROM prospector.jobs WHERE id" in sql:  # _GET_JOB_SQL
                job = self.jobs.get(params["job_id"])
                return [dict(job)] if job else []
        raise AssertionError(f"unexpected query: {sql}")


REP_TJ = SimpleNamespace(id=1, display_name="TJ", daily_credit_cap=50)
REP_NICK = SimpleNamespace(id=2, display_name="Nick", daily_credit_cap=50)

URL = "linkedin.com/in/jane-doe"


@pytest.fixture()
def db() -> StubDb:
    return StubDb()


@pytest.fixture()
def fake_budget(monkeypatch):
    """Call-recording fake for the pinned prospector.budget interface,
    injected into sys.modules so jobs.py's lazy `from . import budget`
    resolves to it. Each check_and_reserve call also records how many
    writes the caller's cursor had staged at that moment, so ordering
    (reserve BEFORE insert) is assertable."""

    class CapExceeded(Exception):
        def __init__(self, cap, spent, requested):
            self.cap, self.spent, self.requested = cap, spent, requested
            super().__init__(f"cap {cap} exceeded (spent {spent}, wanted {requested})")

    mod = types.ModuleType("prospector.budget")
    mod.CapExceeded = CapExceeded
    mod.calls = []
    mod.reserve_error = None
    calls_lock = threading.Lock()

    def check_and_reserve(cur, rep_id, daily_cap, requested):
        with calls_lock:
            mod.calls.append(
                ("check_and_reserve", rep_id, daily_cap, requested,
                 len(cur.staged))
)
        if mod.reserve_error is not None:
            raise mod.reserve_error

    def settle(db_, job_id, billed):
        with calls_lock:
            mod.calls.append(("settle", job_id, billed))

    def expire_refund(db_, job_id):
        with calls_lock:
            mod.calls.append(("expire_refund", job_id))

    mod.check_and_reserve = check_and_reserve
    mod.settle = settle
    mod.expire_refund = expire_refund

    monkeypatch.setitem(sys.modules, "prospector.budget", mod)
    monkeypatch.setattr(prospector, "budget", mod, raising=False)
    return mod


def _join(thread: threading.Thread) -> None:
    thread.join(timeout=10)
    assert not thread.is_alive(), "worker thread did not finish"


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------


def test_create_job_happy_path_reserves_before_insert(db, fake_budget):
    row = create_job(db, REP_TJ, URL, ["work_email", "mobile"],
                     {"first": "Jane"}, worst_cost=3.0)

    assert row["state"] == "queued"
    assert row["rep_id"] == 1
    assert row["norm_linkedin_url"] == URL
    assert row["fields"] == ["work_email", "mobile"]
    assert row["input"] == {"first": "Jane"}
    assert row["credits_reserved"] == 3.0
    assert db.jobs[row["id"]]["state"] == "queued"  # committed

    # Exactly one reserve call, with the rep's cap, and made while ZERO
    # writes were staged -- i.e. strictly before the INSERT.
    assert fake_budget.calls == [("check_and_reserve", 1, 50, 3.0, 0)]


def test_create_job_rejects_unknown_fields_before_spending(db, fake_budget):
    with pytest.raises(ValueError):
        create_job(db, REP_TJ, URL, ["work_email", "shoe_size"], {}, 1.0)
    with pytest.raises(ValueError):
        create_job(db, REP_TJ, URL, [], {}, 1.0)
    assert fake_budget.calls == []  # invalid input never reaches the budget
    assert db.jobs == {}


def test_second_rep_gets_inflight_conflict_with_live_job_and_holder(db, fake_budget):
    first = create_job(db, REP_TJ, URL, ["work_email"], {}, 2.0)

    with pytest.raises(InFlightConflict) as excinfo:
        create_job(db, REP_NICK, URL, ["work_email"], {}, 2.0)

    conflict = excinfo.value
    assert conflict.job_id == first["id"]
    assert conflict.holder_display_name == "TJ"
    # Rep B's attempt left no row behind -- one job, one bill.
    assert len(db.jobs) == 1


def test_create_succeeds_again_after_first_job_is_done(db, fake_budget):
    first = create_job(db, REP_TJ, URL, ["work_email"], {}, 2.0)
    db.jobs[first["id"]]["state"] = "done"  # index is PARTIAL: done rows don't lock

    second = create_job(db, REP_NICK, URL, ["work_email"], {}, 2.0)
    assert second["id"] != first["id"]
    assert second["state"] == "queued"


def test_cap_exceeded_propagates_and_rolls_back(db, fake_budget):
    fake_budget.reserve_error = fake_budget.CapExceeded(50, 49.5, 3.0)

    with pytest.raises(fake_budget.CapExceeded):
        create_job(db, REP_TJ, URL, ["work_email"], {}, 3.0)

    assert db.jobs == {}          # no job row survived
    assert db.commits == 0        # the transaction never committed...
    assert db.rollbacks == 1      # ...it rolled back


# ---------------------------------------------------------------------------
# run_job_async
# ---------------------------------------------------------------------------


def test_run_job_success_settles_billed_and_stores_result(db, fake_budget):
    job = create_job(db, REP_TJ, URL, ["work_email"], {}, 3.0)
    payload = {"work_email": "jane@acmepest.com", "status": "verified"}

    thread = run_job_async(db, job["id"], lambda row: (payload, 2.0))
    _join(thread)

    stored = db.jobs[job["id"]]
    assert stored["state"] == "done"
    assert stored["result"] == payload
    assert stored["finished_at"] is not None
    assert ("settle", job["id"], 2.0) in fake_budget.calls


def test_runner_receives_the_running_job_row(db, fake_budget):
    job = create_job(db, REP_TJ, URL, ["work_email"], {"first": "Jane"}, 3.0)
    seen = {}

    def runner(row):
        seen.update(row)
        return ({}, 0.0)

    _join(run_job_async(db, job["id"], runner))
    assert seen["id"] == job["id"]
    assert seen["state"] == "running"
    assert seen["input"] == {"first": "Jane"}


def test_job_run_error_fails_job_and_settles_partial_billing(db, fake_budget):
    job = create_job(db, REP_TJ, URL, ["work_email", "mobile"], {}, 3.0)

    def runner(row):
        raise JobRunError("mobile leg failed after work_email billed", billed=1.0)

    _join(run_job_async(db, job["id"], runner))

    stored = db.jobs[job["id"]]
    assert stored["state"] == "failed"
    assert stored["result"] == {"error": "mobile leg failed after work_email billed"}
    assert stored["finished_at"] is not None
    assert ("settle", job["id"], 1.0) in fake_budget.calls


def test_unexpected_exception_fails_job_settles_zero_and_leaks_nothing(db, fake_budget):
    job = create_job(db, REP_TJ, URL, ["work_email"], {}, 3.0)

    def runner(row):
        raise RuntimeError("apikey=SUPERSECRET url=https://provider/x?key=abc")

    _join(run_job_async(db, job["id"], runner))

    stored = db.jobs[job["id"]]
    assert stored["state"] == "failed"
    # Class name only -- the raw message (which can carry keys/URLs/PII)
    # must never land in the rep-visible result.
    assert stored["result"] == {"error": "internal: RuntimeError"}
    assert "SUPERSECRET" not in json.dumps(stored["result"])
    assert ("settle", job["id"], 0.0) in fake_budget.calls


def test_worker_skips_job_that_is_no_longer_queued(db, fake_budget):
    job = create_job(db, REP_TJ, URL, ["work_email"], {}, 3.0)
    db.jobs[job["id"]]["state"] = "expired"  # expire_stale won the race
    ran = threading.Event()

    _join(run_job_async(db, job["id"], lambda row: (ran.set() or {}, 0.0)))

    assert not ran.is_set()  # runner never called
    assert db.jobs[job["id"]]["state"] == "expired"  # untouched
    assert not any(call[0] == "settle" for call in fake_budget.calls)


def test_finish_after_expire_settles_spend_but_never_resurrects(db, fake_budget):
    """a worker finishing AFTER expire_stale()
    expired its job must not flip the row back to done/failed (the expiry
    already released the profile lock) -- but the runner's real spend must
    still be settled so the money stays on the books."""
    job = create_job(db, REP_TJ, URL, ["work_email"], {}, 3.0)

    def runner(row):
        # Simulate expire_stale() winning the race while the runner is on
        # the wire: the job goes 'expired' before the finish UPDATE runs.
        with db.mutex:
            db.jobs[job["id"]]["state"] = "expired"
        return ({"emails": [{"address": "jane@acmepest.com"}]}, 2.0)

    _join(run_job_async(db, job["id"], runner))

    stored = db.jobs[job["id"]]
    assert stored["state"] == "expired"   # never resurrected
    assert stored["result"] is None       # result never overwritten
    # ...but the spend still landed: settle was called with the real bill.
    assert ("settle", job["id"], 2.0) in fake_budget.calls


def test_failed_after_expire_settles_partial_billing_without_overwriting(db, fake_budget):
    """Same guard on the failure path: JobRunError after expiry settles
    what was billed, and the expired state/result stay untouched."""
    job = create_job(db, REP_TJ, URL, ["work_email", "mobile"], {}, 11.0)

    def runner(row):
        with db.mutex:
            db.jobs[job["id"]]["state"] = "expired"
        raise JobRunError("mobile leg failed after work_email billed", billed=1.0)

    _join(run_job_async(db, job["id"], runner))

    stored = db.jobs[job["id"]]
    assert stored["state"] == "expired"
    assert stored["result"] is None
    assert ("settle", job["id"], 1.0) in fake_budget.calls


def test_finish_sql_carries_the_running_guard():
    # The guard lives in the SQL itself so no code path can forget it.
    assert "AND state = 'running'" in jobs_mod._FINISH_SQL


def test_stale_window_is_sized_against_the_adapter_deadline():
    """STALE_AFTER_SECONDS must stay ABOVE the worst-case healthy job (3
    single-leg fields x the adapter's 180s resolve deadline + slack), or
    expire_stale() starts killing jobs that are merely slow."""
    from prospector.providers.fullenrich import RESOLVE_DEADLINE_SECONDS
    assert STALE_AFTER_SECONDS == 900
    assert STALE_AFTER_SECONDS > 3 * RESOLVE_DEADLINE_SECONDS


def test_semaphore_caps_simultaneous_runners_at_four(db, fake_budget):
    """5 jobs, 4 slots: the first four runners park on a gate; the fifth
    thread exists (202-fast) but cannot ENTER its runner until a slot
    frees. Event-based throughout -- no sleeps."""
    assert MAX_CONCURRENT_JOBS == 4

    created = [
        create_job(db, REP_TJ, f"linkedin.com/in/p{i}", ["work_email"], {}, 1.0)
        for i in range(5)
    ]

    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "started": 0}
    four_running = threading.Event()
    release = threading.Event()

    def runner(row):
        with lock:
            state["started"] += 1
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            if state["active"] == MAX_CONCURRENT_JOBS:
                four_running.set()
        assert release.wait(timeout=10), "gate never released"
        with lock:
            state["active"] -= 1
        return ({}, 0.0)

    threads = [run_job_async(db, job["id"], runner) for job in created]
    try:
        assert four_running.wait(timeout=10), "never reached 4 concurrent runners"
        with lock:
            # All 4 slots are held by parked runners, so the 5th CANNOT
            # have started -- this needs no sleep to be deterministic.
            assert state["started"] == MAX_CONCURRENT_JOBS
    finally:
        release.set()  # never leave runners parked, even on assert failure

    for thread in threads:
        _join(thread)

    assert state["peak"] == MAX_CONCURRENT_JOBS  # never a 5th in flight
    assert state["started"] == 5                 # but all 5 DID run
    assert all(db.jobs[j["id"]]["state"] == "done" for j in created)


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


def test_get_job_returns_row_or_none(db, fake_budget):
    job = create_job(db, REP_TJ, URL, ["work_email"], {}, 1.0)
    fetched = get_job(db, job["id"])
    assert fetched is not None
    assert fetched["id"] == job["id"]
    assert get_job(db, "job-does-not-exist") is None


# ---------------------------------------------------------------------------
# expire_stale
# ---------------------------------------------------------------------------


def test_expire_stale_expires_old_refunds_each_and_spares_fresh(db, fake_budget):
    old_queued = create_job(db, REP_TJ, "linkedin.com/in/old-a", ["work_email"], {}, 1.0)
    old_running = create_job(db, REP_TJ, "linkedin.com/in/old-b", ["work_email"], {}, 1.0)
    fresh = create_job(db, REP_NICK, "linkedin.com/in/fresh", ["work_email"], {}, 1.0)

    db.jobs[old_running["id"]]["state"] = "running"
    for job in (old_queued, old_running):
        db.jobs[job["id"]]["created_at"] -= STALE_AFTER_SECONDS + 60

    count = expire_stale(db)

    assert count == 2
    for job in (old_queued, old_running):
        assert db.jobs[job["id"]]["state"] == "expired"
        assert db.jobs[job["id"]]["finished_at"] is not None
        assert ("expire_refund", job["id"]) in fake_budget.calls
    assert db.jobs[fresh["id"]]["state"] == "queued"
    assert ("expire_refund", fresh["id"]) not in fake_budget.calls


def test_expire_stale_noop_when_nothing_is_stale(db, fake_budget):
    create_job(db, REP_TJ, URL, ["work_email"], {}, 1.0)
    assert expire_stale(db) == 0
    assert not any(call[0] == "expire_refund" for call in fake_budget.calls)


def test_expired_profile_can_be_enriched_again(db, fake_budget):
    """Expiry releases the in-flight lock: the partial index no longer sees
    the row, so a fresh create for the same profile succeeds."""
    job = create_job(db, REP_TJ, URL, ["work_email"], {}, 1.0)
    db.jobs[job["id"]]["created_at"] -= STALE_AFTER_SECONDS + 60
    assert expire_stale(db) == 1

    again = create_job(db, REP_NICK, URL, ["work_email"], {}, 1.0)
    assert again["state"] == "queued"
    assert again["id"] != job["id"]
