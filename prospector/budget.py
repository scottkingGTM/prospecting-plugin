"""Per-rep daily credit budget: reserve BEFORE the provider call.

Lifted from an earlier enrichment-waterfall budget philosophy: a cap checked
AFTER the call is a cap in name only -- by the time you know what a vendor
billed, the money is spent. So credits are reserved against the rep's daily
cap at enqueue time, at the worst case for the requested fields, and the
unspent difference is released only when the job settles.

The worst-case invariant: at every instant, SUM(credits_reserved) over the
rep's non-expired jobs today is >= what today can still possibly cost, so
the daily cap holds even if every in-flight item bills its maximum.

There is no separate reservation ledger table -- the reservation LIVES on
the jobs row (prospector.jobs.credits_reserved). Settling lowers
credits_reserved to credits_billed, so one SUM over today's rows is always
the true worst-case exposure; an expired job drops out of the SUM only
when nothing was billed -- expired-but-billed rows (real vendor spend
parked by expire_refund from the attempts ledger) keep counting (from a
hardening pass).

Concurrency: prospector.reps is SELECT-only for the service role, so the
usual SELECT ... FOR UPDATE row lock is not available. Instead the
check+insert is serialized per rep with a transaction-scoped advisory lock
(pg_advisory_xact_lock) taken INSIDE the caller's transaction, before the
SUM. Two racing requests for the same rep queue on the lock; the second one
re-runs the SUM after the first has committed its reservation, so the cap
can never be double-spent. The lock releases itself at commit/rollback --
nothing to clean up, nothing that can leak.

Day = UTC calendar day (house convention, matches v_usage).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Float slack: whole- and half-credit sums are exact in binary, but a
# fractional unit like 0.1 must not trip the cap on the last affordable
# reservation through accumulated rounding (0.1 + 0.2 != 0.3).
_EPSILON = 1e-9

# One advisory-lock keyspace per rep. hashtext() maps the string to the
# int4 pg_advisory_xact_lock wants; the prefix keeps rep budget locks from
# ever colliding with some future advisory-lock use of bare integers.
_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext(%s));"
_LOCK_KEY_PREFIX = "prospector_budget_"

# Spent-today = SUM of reservations over the rep's jobs created today
# (UTC). Settled jobs already had credits_reserved lowered to billed, so
# this single SUM is always the true worst-case exposure. Expired rows drop
# out ONLY when nothing was billed: an expired-but-billed row is real vendor
# spend parked by expire_refund() and must keep counting against the cap
# (from a hardening pass -- see expire_refund).
_SPENT_TODAY_SQL = """
    SELECT COALESCE(SUM(credits_reserved), 0) AS spent
    FROM prospector.jobs
    WHERE rep_id = %s
      AND (created_at AT TIME ZONE 'UTC')::date = (now() AT TIME ZONE 'UTC')::date
      AND (state != 'expired' OR credits_billed > 0);
"""

# Terminal accounting: billed becomes what the providers actually charged
# (GREATEST so a replayed/late settle can never LOWER billed), and the
# reservation collapses to that same figure -- the unspent hold goes back
# to the rep's day the moment the transaction commits.
_SETTLE_SQL = """
    UPDATE prospector.jobs
    SET credits_billed   = GREATEST(credits_billed, %s),
        credits_reserved = GREATEST(credits_billed, %s),
        finished_at      = COALESCE(finished_at, now())
    WHERE id = %s;
"""

# The attempts ledger is the ground truth for whether money actually moved
# on a dead job: a worker killed between a billed vendor call and settle
# (Railway redeploy mid-job) leaves jobs.credits_billed = 0 while
# prospector.attempts already holds the vendor's charge -- so a refund gate
# that trusts credits_billed alone refunds money a vendor invoiced (from a
# hardening pass). Cache rows are bookkeeping, never spend, so they are
# excluded.
_ATTEMPTS_LEDGER_COST_SQL = """
    SELECT COALESCE(SUM(cost_credits), 0) AS cost
    FROM prospector.attempts
    WHERE job_id = %s
      AND provider_id != 'cache';
"""

# The job died mid-flight but the attempts ledger shows real vendor spend:
# park the charge on the row (billed AND reserved = the ledger sum) so the
# money stays visible AND held against today's cap -- see the
# expired-but-billed exception in _SPENT_TODAY_SQL.
_EXPIRE_BILLED_SQL = """
    UPDATE prospector.jobs
    SET credits_billed = %s,
        credits_reserved = %s
    WHERE id = %s
      AND state = 'expired';
"""

# Refund on expiry, gated on credits_billed = 0 AND an empty attempts
# ledger (checked by the caller, expire_refund): only a job that truly
# never billed anything gives its hold back.
_EXPIRE_REFUND_SQL = """
    UPDATE prospector.jobs
    SET credits_reserved = 0
    WHERE id = %s
      AND state = 'expired'
      AND credits_billed = 0;
"""


class CapExceeded(Exception):
    """The requested reservation would push the rep past the daily cap."""

    def __init__(self, cap: float, spent: float, requested: float):
        self.cap = float(cap)
        self.spent = float(spent)
        self.requested = float(requested)
        super().__init__(
            f"daily credit cap exceeded: spent {self.spent:g} + "
            f"requested {self.requested:g} > cap {self.cap:g}"
        )


def check_and_reserve(cur, rep_id: int, daily_cap: float, requested: float) -> None:
    """Call INSIDE the caller's transaction, BEFORE the jobs INSERT.

    Takes the per-rep advisory xact lock, sums today's reservations, and
    raises CapExceeded if spent + requested > cap (with 1e-9 float slack;
    landing EXACTLY on the cap is allowed). The caller then INSERTs the job
    with credits_reserved=requested in the SAME transaction -- commit makes
    the reservation atomic with the cap check, and the lock's release at
    commit lets the next request in line see it.

    Raises ValueError on a negative request: a negative "reservation" would
    be a cap bypass, never a legitimate call.
    """
    if requested < 0:
        raise ValueError(f"requested credits must be >= 0, got {requested}")

    # Lock FIRST -- summing before serializing would let two requests both
    # read the same "spent" and both fit under the cap.
    cur.execute(_LOCK_SQL, (f"{_LOCK_KEY_PREFIX}{rep_id}",))

    cur.execute(_SPENT_TODAY_SQL, (rep_id,))
    row = cur.fetchone()
    spent = float(row["spent"] or 0) if row else 0.0

    if spent + requested > daily_cap + _EPSILON:
        logger.info(
            "cap check FAILED for rep_id=%s: spent=%s requested=%s cap=%s",
            rep_id, spent, requested, daily_cap,
        )
        raise CapExceeded(daily_cap, spent, requested)

    logger.debug(
        "cap check ok for rep_id=%s: spent=%s requested=%s cap=%s",
        rep_id, spent, requested, daily_cap,
    )


def settle(db, job_id: str, billed: float) -> None:
    """Terminal accounting for a finished job: credits_billed = billed AND
    credits_reserved = billed -- releasing the unspent hold back to the
    rep's day. NEVER lowers billed (GREATEST in SQL, so a duplicate or
    stale settle is harmless). Worst-case invariant: between reserve and
    settle the full reservation counts against the cap; only settling
    releases the difference.
    """
    with db.cursor() as cur:
        cur.execute(_SETTLE_SQL, (billed, billed, job_id))
        if cur.rowcount == 0:
            logger.warning("settle: no such job id=%s", job_id)
        else:
            logger.info("settled job id=%s at billed=%s", job_id, billed)


def expire_refund(db, job_id: str) -> None:
    """A job that died without reaching settle: state was set to 'expired'
    by jobs.py. Consult the ATTEMPTS LEDGER first -- jobs.credits_billed is
    written only by settle, and a worker killed between a billed vendor
    call and settle (Railway redeploy mid-job) leaves billed = 0 while
    prospector.attempts already holds the real charge. The old code's
    "keep the charge visible" claim was only true when settle ran; this
    version makes it true always (from a hardening pass):

      * ledger sum > 0: NOT a refund. credits_billed AND credits_reserved
        both become the ledger sum, so the money stays visible and stays
        held against today's cap (the expired-but-billed exception in
        _SPENT_TODAY_SQL keeps counting it).
      * ledger sum = 0: a true zero -- release the hold (credits_reserved
        to 0), still gated on credits_billed = 0.

    One cursor block = one transaction: the ledger read and the write
    commit together.
    """
    with db.cursor() as cur:
        cur.execute(_ATTEMPTS_LEDGER_COST_SQL, (job_id,))
        row = cur.fetchone()
        ledger = float(row["cost"] or 0) if row else 0.0

        if ledger > 0:
            cur.execute(_EXPIRE_BILLED_SQL, (ledger, ledger, job_id))
            logger.warning(
                "expire_refund: job id=%s expired with %s credits in the "
                "attempts ledger but nothing settled -- parking the charge "
                "as billed, NOT refunding", job_id, ledger,
            )
            return

        cur.execute(_EXPIRE_REFUND_SQL, (job_id,))
        if cur.rowcount == 0:
            logger.info(
                "expire_refund: no refund for job id=%s "
                "(not expired, already refunded, or credits were billed mid-flight)",
                job_id,
            )
        else:
            logger.info("expire_refund: released reservation for job id=%s", job_id)


def spent_today(db, rep_id: int) -> float:
    """Worst-case credits counted against rep_id's cap so far today (UTC).
    Read-only; for /status display -- enforcement uses check_and_reserve.
    """
    with db.cursor() as cur:
        cur.execute(_SPENT_TODAY_SQL, (rep_id,))
        row = cur.fetchone()
    return float(row["spent"] or 0) if row else 0.0
