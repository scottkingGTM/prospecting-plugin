"""Pure unit tests for prospector.budget -- NO live DB, no psycopg2.

Covers:
  * check_and_reserve takes the advisory xact lock BEFORE the SUM
  * cap exactly reached is allowed; one credit over raises CapExceeded
    carrying the correct cap/spent/requested
  * float slack: 0.1 + 0.2 style sums don't false-trip the cap
  * settle lowers reserved to billed via GREATEST (never lowers billed)
  * expire_refund consults the ATTEMPTS ledger first: real ledger spend
    converts the hold to billed instead of
    refunding; only a true zero (empty ledger AND credits_billed = 0)
    releases the hold; cache rows never count as spend
  * race simulation: two threads with a fake advisory lock racing the
    last credit -- exactly one CapExceeded
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from decimal import Decimal

import pytest

from prospector.budget import (
    CapExceeded,
    check_and_reserve,
    expire_refund,
    settle,
    spent_today,
)


# -- stubs ---------------------------------------------------------------------


class StubCursor:
    """Records every execute(); answers the spent-SUM from `spent` and the
    attempts-ledger SUM from `attempts_cost`."""

    def __init__(self, spent=0.0, rowcount=1, attempts_cost=0.0):
        self.calls: list[tuple[str, tuple]] = []
        self.spent = spent
        self.rowcount = rowcount
        self.attempts_cost = attempts_cost
        self._last_sql = ""

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._last_sql = sql

    def fetchone(self):
        if "FROM prospector.attempts" in self._last_sql:
            return {"cost": self.attempts_cost}
        if "SUM" in self._last_sql.upper():
            return {"spent": self.spent}
        return None


class StubDB:
    """db.cursor() context manager over a single StubCursor -- mirrors the
    real Database contract: one `with db.cursor() as cur:` block = one
    transaction."""

    def __init__(self, cur: StubCursor):
        self.cur = cur
        self.commits = 0

    @contextmanager
    def cursor(self):
        yield self.cur
        self.commits += 1


# -- check_and_reserve: lock ordering ------------------------------------------


def test_lock_taken_before_sum():
    cur = StubCursor(spent=0.0)
    check_and_reserve(cur, rep_id=7, daily_cap=50.0, requested=2.0)

    assert len(cur.calls) == 2
    lock_sql, lock_params = cur.calls[0]
    sum_sql, sum_params = cur.calls[1]
    assert "pg_advisory_xact_lock" in lock_sql
    assert lock_params == ("prospector_budget_7",)
    assert "SUM" in sum_sql.upper()
    assert sum_params == (7,)


def test_sum_excludes_expired_and_is_utc_day_scoped():
    cur = StubCursor(spent=0.0)
    check_and_reserve(cur, rep_id=1, daily_cap=50.0, requested=1.0)
    sum_sql = cur.calls[1][0]
    # Expired rows drop out ONLY when nothing was billed: an
    # expired-but-billed row is real vendor spend parked by expire_refund
    # and must keep counting (a hardening pass).
    assert "(state != 'expired' OR credits_billed > 0)" in sum_sql
    assert "AT TIME ZONE 'UTC'" in sum_sql


# -- check_and_reserve: cap math -------------------------------------------------


def test_cap_exactly_reached_is_allowed():
    cur = StubCursor(spent=45.0)
    check_and_reserve(cur, rep_id=1, daily_cap=50.0, requested=5.0)  # no raise


def test_one_credit_over_raises_with_correct_fields():
    cur = StubCursor(spent=45.0)
    with pytest.raises(CapExceeded) as excinfo:
        check_and_reserve(cur, rep_id=1, daily_cap=50.0, requested=6.0)
    exc = excinfo.value
    assert exc.cap == 50.0
    assert exc.spent == 45.0
    assert exc.requested == 6.0


def test_float_slack_does_not_false_trip():
    # 0.1 + 0.2 == 0.30000000000000004 in binary; reserving up to a 0.3-ish
    # cap must not lose the last affordable reservation to rounding.
    cur = StubCursor(spent=0.1 + 0.2)
    check_and_reserve(cur, rep_id=1, daily_cap=0.6, requested=0.3)  # no raise


def test_decimal_spent_from_numeric_column_is_handled():
    # psycopg2 returns numeric as Decimal; the SUM result must not blow up
    # or mis-compare against float cap/requested.
    cur = StubCursor(spent=Decimal("45.5"))
    with pytest.raises(CapExceeded):
        check_and_reserve(cur, rep_id=1, daily_cap=50.0, requested=5.0)


def test_negative_request_is_rejected():
    cur = StubCursor(spent=0.0)
    with pytest.raises(ValueError):
        check_and_reserve(cur, rep_id=1, daily_cap=50.0, requested=-1.0)
    assert cur.calls == []  # rejected before any SQL


# -- settle ----------------------------------------------------------------------


def test_settle_lowers_reserved_to_billed_never_lowers_billed():
    cur = StubCursor()
    db = StubDB(cur)
    settle(db, "job-123", billed=2.5)

    assert len(cur.calls) == 1
    sql, params = cur.calls[0]
    # Both columns settle to GREATEST(old billed, new billed): reserved
    # collapses to billed, and a stale/duplicate settle can never LOWER
    # what was already billed.
    assert "credits_billed" in sql
    assert "credits_reserved" in sql
    assert sql.count("GREATEST(credits_billed, %s)") == 2
    assert params == (2.5, 2.5, "job-123")
    assert db.commits == 1  # one cursor block = one transaction


def test_settle_missing_job_does_not_raise(caplog):
    cur = StubCursor(rowcount=0)
    settle(StubDB(cur), "no-such-job", billed=1.0)  # logs a warning, no raise


# -- expire_refund ----------------------------------------------------------------


def test_expire_refund_true_zero_releases_the_hold():
    # Case 1: empty attempts ledger AND nothing billed -- a true zero.
    # The ledger is consulted FIRST (a hardening pass), then the
    # guarded refund UPDATE releases the reservation.
    cur = StubCursor(rowcount=1, attempts_cost=0.0)
    db = StubDB(cur)
    expire_refund(db, "job-abc")

    assert len(cur.calls) == 2
    ledger_sql, ledger_params = cur.calls[0]
    assert "FROM prospector.attempts" in ledger_sql
    assert "provider_id != 'cache'" in ledger_sql  # cache rows are not spend
    assert ledger_params == ("job-abc",)

    sql, params = cur.calls[1]
    assert "credits_reserved = 0" in sql
    assert "credits_billed = 0" in sql  # the guard
    assert "state = 'expired'" in sql
    assert params == ("job-abc",)
    assert db.commits == 1  # ledger read + write = ONE transaction


def test_expire_refund_ledger_spend_parks_charge_instead_of_refunding():
    # Case 2: the worker died between a billed vendor call and settle --
    # jobs.credits_billed is still 0 but the attempts ledger holds the
    # real charge. NOT a refund: billed AND reserved both become the
    # ledger sum, so the money stays visible and held (a hardening pass).
    cur = StubCursor(attempts_cost=2.5)
    db = StubDB(cur)
    expire_refund(db, "job-def")

    assert len(cur.calls) == 2
    sql, params = cur.calls[1]
    assert "credits_billed = %s" in sql
    assert "credits_reserved = %s" in sql
    assert "state = 'expired'" in sql
    assert "credits_reserved = 0" not in sql  # never the refund statement
    assert params == (2.5, 2.5, "job-def")
    assert db.commits == 1


def test_expire_refund_keeps_charge_when_billed_mid_flight():
    # Case 3: ledger empty (e.g. audit rows lost) but credits_billed > 0
    # on the row -- the guarded refund UPDATE matches nothing (rowcount 0);
    # the charge stays visible, and no other statement forces the refund
    # through.
    cur = StubCursor(rowcount=0, attempts_cost=0.0)
    db = StubDB(cur)
    expire_refund(db, "job-ghi")

    assert len(cur.calls) == 2  # ledger read + the one guarded UPDATE
    assert "credits_billed = 0" in cur.calls[1][0]


# -- spent_today -------------------------------------------------------------------


def test_spent_today_returns_float_of_sum():
    cur = StubCursor(spent=Decimal("12.5"))
    value = spent_today(StubDB(cur), rep_id=3)
    assert value == 12.5
    assert isinstance(value, float)
    sql, params = cur.calls[0]
    assert "SUM" in sql.upper()
    assert params == (3,)


def test_spent_today_none_row_is_zero():
    class EmptyCursor(StubCursor):
        def fetchone(self):
            return None

    assert spent_today(StubDB(EmptyCursor()), rep_id=3) == 0.0


# -- race simulation ---------------------------------------------------------------


class FakeJobsTable:
    """Shared 'jobs' state for the race: a list of reservations."""

    def __init__(self, rows=None):
        self.rows: list[float] = list(rows or [])


class RaceCursor:
    """Maps pg_advisory_xact_lock -> acquiring a threading.Lock; answers
    the SUM from the shared table. The lock is released on transaction end
    (RaceDB.cursor context exit), matching xact-lock semantics."""

    def __init__(self, table: FakeJobsTable, lock: threading.Lock):
        self.table = table
        self.lock = lock
        self.holds_lock = False
        self._last_sql = ""

    def execute(self, sql, params=None):
        self._last_sql = sql
        if "pg_advisory_xact_lock" in sql:
            self.lock.acquire()
            self.holds_lock = True

    def fetchone(self):
        if "SUM" in self._last_sql.upper():
            return {"spent": sum(self.table.rows)}
        return None


class RaceDB:
    def __init__(self, table: FakeJobsTable, lock: threading.Lock):
        self.table = table
        self.lock = lock

    @contextmanager
    def cursor(self):
        cur = RaceCursor(self.table, self.lock)
        try:
            yield cur
        finally:
            # Transaction end (commit OR rollback) releases the xact lock.
            if cur.holds_lock:
                cur.holds_lock = False
                self.lock.release()


def test_race_for_last_credit_exactly_one_cap_exceeded():
    table = FakeJobsTable(rows=[9.0])  # 9 of 10 already reserved today
    fake_advisory_lock = threading.Lock()
    db = RaceDB(table, fake_advisory_lock)

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    outcomes_guard = threading.Lock()

    def contender():
        barrier.wait()
        try:
            # One transaction: lock -> cap check -> "INSERT" of the job row
            # with credits_reserved=requested, exactly the caller contract.
            with db.cursor() as cur:
                check_and_reserve(cur, rep_id=1, daily_cap=10.0, requested=1.0)
                table.rows.append(1.0)
            result = "reserved"
        except CapExceeded:
            result = "capped"
        with outcomes_guard:
            outcomes.append(result)

    threads = [threading.Thread(target=contender) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(outcomes) == ["capped", "reserved"]
    assert sum(table.rows) == 10.0  # cap held exactly, never overshot
    assert not fake_advisory_lock.locked()  # released on transaction end
