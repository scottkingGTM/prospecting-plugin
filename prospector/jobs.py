"""The enrichment job queue: create, run, read, and expire prospector.jobs.

One row per requested enrichment of one LinkedIn profile (see
sql/01_create_schema.sql). This module owns the four verbs of that row's
life and encodes three contracts the API surface depends on:

  * 202-fast, bounded work. POST /enrich must answer the moment the row
    exists -- a rep watching the panel should never wait on OTHER reps'
    enrichments to learn their own was accepted. So run_job_async() starts
    the worker thread immediately and the thread acquires the module
    semaphore INSIDE itself: enqueue latency is constant, while actual
    provider concurrency stays capped at MAX_CONCURRENT_JOBS (one slot per
    rep is plenty -- there are four reps).

  * One in-flight job per profile (the 409 contract). The partial unique
    index jobs_inflight_one_per_profile makes the second INSERT for a
    profile that is already queued/running fail; create_job() converts
    that into InFlightConflict carrying the LIVE job's id and the holding
    rep's display name. The route turns that into a 409 whose body hands
    rep B rep A's job id -- rep B's panel simply polls that id and both
    reps read one result for one bill, instead of paying twice for the
    same person. (This is also why get_job() is NOT rep-scoped.)

  * No refund after submit. expire_stale() refunds only via
    budget.expire_refund(), which releases the RESERVED hold -- never
    credits already billed by a provider. Once a provider leg has billed,
    that money is spent whether or not our job row reaches 'done'; a
    refund policy that trusted our own bookkeeping over the provider's
    invoice would drift the cap optimistic, which is the wrong direction
    for a blast-radius limiter.

prospector.budget is imported lazily inside each function that spends or
refunds, never at module level: a budget import problem then surfaces on
the spend paths (where it is fatal anyway) instead of taking down read-only
routes, and tests can swap in a fake budget module without this module
holding a stale reference bound at import time.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

import psycopg2
import psycopg2.errors

logger = logging.getLogger(__name__)

# The only fields a rep may request; mirrors the waterfalls.field CHECK in
# sql/01_create_schema.sql. Anything else is a 400 at the route, and a
# ValueError here as defense in depth.
ALLOWED_FIELDS = frozenset({"work_email", "mobile", "personal_email"})

# A queued/running job older than this is presumed wedged (worker died,
# provider hung past every timeout) and gets expired so its profile lock
# releases and its unbilled reservation returns to the rep's cap. Sized
# against the adapter's wall-clock bound (a review found this): a
# worst-case job is 3 single-leg fields x RESOLVE_DEADLINE_SECONDS (180s,
# providers/fullenrich.py) = 540s, plus queue-wait and settle slack, still
# comfortably under 900 -- so a HEALTHY job can never be expired mid-run.
STALE_AFTER_SECONDS = 900

# Upper bound on SIMULTANEOUS provider work, process-wide. Deliberately
# small: four reps, and each provider call is seconds -- a burst simply
# queues on the semaphore while every /enrich still answers 202 instantly.
MAX_CONCURRENT_JOBS = 4

# Module-level on purpose: the cap is per PROCESS, not per server object,
# and there is exactly one serving process. Bounded so a release bug
# raises instead of silently widening the cap.
_JOB_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)

_INFLIGHT_INDEX = "jobs_inflight_one_per_profile"


class InFlightConflict(Exception):
    """Another rep already has this profile queued/running. Carries what
    the 409 body needs: the LIVE job's id (for rep B's panel to poll) and
    the holder's display name (for the human explanation)."""

    def __init__(self, job_id: Any, holder_display_name: str | None) -> None:
        self.job_id = job_id
        self.holder_display_name = holder_display_name
        super().__init__(
            f"profile already being enriched (job {job_id}, "
            f"held by {holder_display_name or 'unknown'})"
        )


class JobRunError(Exception):
    """Raised by a runner when the waterfall failed AFTER one or more legs
    already billed. `.billed` is what the providers actually charged before
    the failure, so budget.settle() can burn exactly that much of the
    reservation instead of refunding money a vendor already invoiced.
    str() of this exception is rep-visible -- runners must construct the
    message themselves and keep secrets out of it."""

    def __init__(self, message: str, billed: float = 0.0) -> None:
        super().__init__(message)
        self.billed = float(billed or 0.0)


_INSERT_JOB_SQL = """
    INSERT INTO prospector.jobs
        (rep_id, norm_linkedin_url, fields, state, input, credits_reserved)
    VALUES (%(rep_id)s, %(norm_url)s, %(fields)s, 'queued', %(input)s,
            %(credits_reserved)s)
    RETURNING *;
"""

# Who holds the live job for this profile? Separate lookup AFTER the
# failed insert's transaction rolled back -- joined to reps for the
# display name the 409 body shows.
_HOLDER_SQL = """
    SELECT j.id AS job_id, r.display_name
      FROM prospector.jobs j
      JOIN prospector.reps r ON r.id = j.rep_id
     WHERE j.norm_linkedin_url = %(norm_url)s
       AND j.state IN ('queued', 'running')
     LIMIT 1;
"""

# state='queued' guard: if expire_stale() won the race before the worker
# thread got a slot, this matches nothing and the worker walks away.
_MARK_RUNNING_SQL = """
    UPDATE prospector.jobs
       SET state = 'running'
     WHERE id = %(job_id)s
       AND state = 'queued'
    RETURNING *;
"""

# state='running' guard (from a hardening pass): a worker finishing
# AFTER expire_stale() already expired the job must not resurrect it --
# the expiry path may have released the profile lock and re-priced the
# hold, and a later finish overwriting state/result would fork the row's
# history. A finish that matches 0 rows still settles its real spend (see
# _run_job_slotted); it just never rewrites the terminal state.
_FINISH_SQL = """
    UPDATE prospector.jobs
       SET state = %(state)s,
           result = %(result)s,
           finished_at = now()
     WHERE id = %(job_id)s
       AND state = 'running';
"""

_GET_JOB_SQL = """
    SELECT * FROM prospector.jobs WHERE id = %(job_id)s;
"""

# now() - make_interval(...) on purpose: staleness is judged entirely by
# the DATABASE clock. The app's clock never enters the comparison, so a
# skewed container can neither expire fresh jobs nor keep wedged ones
# alive.
_EXPIRE_SQL = """
    UPDATE prospector.jobs
       SET state = 'expired',
           finished_at = now()
     WHERE state IN ('queued', 'running')
       AND created_at < now() - make_interval(secs => %(stale)s)
    RETURNING id;
"""


def create_job(db: Any, rep: Any, norm_url: str, fields: list[str],
               input_payload: dict, worst_cost: float) -> dict:
    """Reserve budget and enqueue one enrichment job, in ONE transaction.

    Inside a single db.cursor() block (= one transaction, see
    database.Database.cursor): budget.check_and_reserve() first -- it takes
    the per-rep advisory budget lock on the same connection and raises
    budget.CapExceeded if worst_cost would blow the daily cap -- then the
    INSERT. Ordering matters: the reservation and the row commit or roll
    back together, so there is never a job without a hold nor a hold
    without a job.

    Raises:
      ValueError         -- fields empty or not a subset of ALLOWED_FIELDS.
      budget.CapExceeded -- re-raised untouched; the route maps it to 402.
      InFlightConflict   -- the partial unique index fired: another job for
                            this profile is queued/running. Carries the
                            live job's id + holder display name (409 body).
    """
    requested = list(fields or [])
    unknown = set(requested) - ALLOWED_FIELDS
    if not requested or unknown:
        raise ValueError(
            f"fields must be a non-empty subset of {sorted(ALLOWED_FIELDS)}"
            + (f" (got unknown: {sorted(unknown)})" if unknown else " (got none)")
        )

    from . import budget  # lazy on purpose -- see module docstring

    try:
        with db.cursor() as cur:
            budget.check_and_reserve(
                cur, rep.id, rep.daily_credit_cap, float(worst_cost)
            )
            cur.execute(_INSERT_JOB_SQL, {
                "rep_id": rep.id,
                "norm_url": norm_url,
                "fields": requested,
                "input": json.dumps(input_payload or {}),
                "credits_reserved": float(worst_cost),
            })
            row = cur.fetchone()
        return dict(row)
    except psycopg2.errors.UniqueViolation as exc:
        constraint = getattr(getattr(exc, "diag", None), "constraint_name", None)
        if constraint != _INFLIGHT_INDEX and _INFLIGHT_INDEX not in str(exc):
            raise  # some other unique index -- not ours to translate
        holders = db.query(_HOLDER_SQL, {"norm_url": norm_url})
        holder = holders[0] if holders else {}
        # An empty holder means the live job finished in the race window
        # between our failed insert and this lookup; job_id=None tells the
        # route "retry now", which will simply succeed.
        raise InFlightConflict(
            holder.get("job_id"), holder.get("display_name")
        ) from None


def run_job_async(db: Any, job_id: str,
                  runner: Callable[[dict], tuple[dict, float]]) -> threading.Thread:
    """Start the worker thread for a queued job and return immediately.

    The thread is a daemon (a SIGTERM must not wait on provider calls;
    expire_stale() + budget.expire_refund() clean up anything cut off) and
    it acquires _JOB_SLOTS INSIDE itself: /enrich answers 202 the instant
    the row exists even when all MAX_CONCURRENT_JOBS slots are busy -- the
    job just waits in 'queued' until a slot frees.

    `runner(job_row) -> (result_payload, credits_billed)` does the actual
    waterfall. On success the job goes 'done' with the payload; on failure
    'failed' with a SAFE error text: str() of our own JobRunError only --
    any other exception type is reduced to its class name, because raw
    provider/library exception text can embed request URLs, keys, or PII
    and jobs.result is rep-visible.
    """
    thread = threading.Thread(
        target=_run_job,
        args=(db, job_id, runner),
        name=f"prospector-job-{job_id}",
        daemon=True,
    )
    thread.start()
    return thread


def _run_job(db: Any, job_id: str,
             runner: Callable[[dict], tuple[dict, float]]) -> None:
    """Thread body. The outer try/except is the never-die-silently rule: a
    worker thread has no caller to propagate to, so ANY escape lands in the
    log instead of vanishing into threading's default hook."""
    try:
        with _JOB_SLOTS:
            _run_job_slotted(db, job_id, runner)
    except Exception:
        logger.exception("job %s: worker thread crashed", job_id)


def _run_job_slotted(db: Any, job_id: str,
                     runner: Callable[[dict], tuple[dict, float]]) -> None:
    from . import budget  # lazy on purpose -- see module docstring

    with db.cursor() as cur:
        cur.execute(_MARK_RUNNING_SQL, {"job_id": job_id})
        row = cur.fetchone()
    if row is None:
        # Not 'queued' anymore: expire_stale() beat us to it (or the id is
        # bogus). Nothing was run, nothing billed -- the expiry path
        # already handled the refund.
        logger.warning(
            "job %s: no longer 'queued' when the worker got a slot -- skipping",
            job_id,
        )
        return

    job_row = dict(row)
    try:
        result_payload, credits_billed = runner(job_row)
    except Exception as exc:
        if isinstance(exc, JobRunError):
            billed = exc.billed
            message = str(exc)  # our own type: message is ours, safe to show
        else:
            billed = 0.0
            message = f"internal: {type(exc).__name__}"  # class name ONLY
        logger.warning(
            "job %s: runner failed (billed=%s)", job_id, billed, exc_info=True
        )
        if not _finish(db, job_id, "failed", {"error": message}):
            logger.error(
                "job %s: failed AFTER being expired -- state/result left "
                "untouched, settling billed=%s so the spend stays on the "
                "books", job_id, billed,
            )
        budget.settle(db, job_id, billed)
        return

    finished = _finish(db, job_id, "done", result_payload)
    if not finished:
        # The job was expired mid-flight (expire_stale() fired while the
        # runner was still on the wire). NEVER overwrite state/result --
        # the row's terminal state is already written -- but the spend is
        # real, so settle it anyway: _SETTLE_SQL's GREATEST plus the
        # expired-but-billed exception in budget._SPENT_TODAY_SQL keep the
        # money visible and counted (from a hardening pass).
        logger.error(
            "job %s: finished AFTER being expired -- state/result left "
            "untouched, settling billed=%s so the spend stays on the books",
            job_id, credits_billed,
        )
    budget.settle(db, job_id, float(credits_billed))


def _finish(db: Any, job_id: str, state: str, result: dict) -> bool:
    """Write the terminal state; returns False when the guarded UPDATE
    matched no row (the job is no longer 'running' -- expired mid-flight)."""
    with db.cursor() as cur:
        cur.execute(_FINISH_SQL, {
            "job_id": job_id,
            "state": state,
            "result": json.dumps(result),
        })
        return cur.rowcount > 0


def get_job(db: Any, job_id: str) -> dict | None:
    """Read one job row, or None. Deliberately NOT rep-scoped: the 409
    flow hands rep B rep A's job id so both panels can poll one job --
    any authenticated rep may read any job. (Authentication itself is the
    route's problem; this function assumes the caller already passed it.)
    """
    try:
        rows = db.query(_GET_JOB_SQL, {"job_id": job_id})
    except psycopg2.errors.InvalidTextRepresentation:
        return None  # not a uuid at all -- same answer as "no such job"
    return dict(rows[0]) if rows else None


def expire_stale(db: Any) -> int:
    """Expire queued/running jobs older than STALE_AFTER_SECONDS and refund
    each one's UNBILLED reservation via budget.expire_refund(). Returns how
    many were expired.

    Called opportunistically by the routes (on enqueue and on result
    reads) rather than by a scheduler: the moments a stale lock or a stuck
    reservation actually hurts are exactly the moments a rep touches the
    queue, so piggybacking there keeps the system honest with zero extra
    moving parts. Staleness is computed by the database clock (see
    _EXPIRE_SQL) -- the app clock is never consulted.

    Refund scope is the no-refund-after-submit rule: expire_refund()
    releases the reserved hold only; anything a provider already billed
    stays spent (see the module docstring).
    """
    from . import budget  # lazy on purpose -- see module docstring

    with db.cursor() as cur:
        cur.execute(_EXPIRE_SQL, {"stale": STALE_AFTER_SECONDS})
        rows = cur.fetchall()

    expired = 0
    for row in rows:
        job_id = row["id"]
        expired += 1
        try:
            budget.expire_refund(db, job_id)
        except Exception:
            # The job IS expired (that committed above); a failed refund
            # only leaves the hold to age out with the daily cap reset, so
            # log it rather than un-expire anything.
            logger.warning("job %s: expire refund failed", job_id, exc_info=True)
    if expired:
        logger.info("expired %d stale job(s)", expired)
    return expired
