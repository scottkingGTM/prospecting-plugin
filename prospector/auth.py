"""Per-rep bearer-token auth for prospecting_plugin.

The server is multi-tenant in the smallest possible way: each rep has their
own bearer token, and the token IS the identity -- there are no usernames,
sessions, or cookies to get wrong. Two rules keep that safe:

  * Tokens are never stored. prospector.reps holds sha256 hex digests only
    (token_hash); this module hashes the presented token and compares
    digests. A database dump therefore leaks no credentials.
  * Comparison is constant-time ACROSS ALL REPS, not just per rep. The
    presented token is hashed once, then the digest is checked against
    EVERY cached rep's token_hash with secrets.compare_digest, with no
    early exit on a match. Response time does not vary with which rep
    matched, where they sit in the list, or whether anyone matched at all
    -- so timing tells an attacker nothing about the roster.

The registry caches the active-rep rows with a short TTL so auth does not
cost a database round-trip per request, while a deactivated rep still loses
access within a minute. If the DB is unreachable, a stale cache is served
for at most STALE_ROSTER_MAX_SECONDS past its load time, then auth fails
closed -- so an offboarded rep's token must die within TTL + 15 minutes
even during a DB outage. The clock is injectable so tests can move time
without sleeping.

NEVER log tokens or token hashes from this module -- a hash is enough to
grant access if the DB is writable, so it is treated as a secret too.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 60.0
# When a roster refresh fails, the stale cache may be served for at most
# this long past its load time; beyond it, auth fails closed. Trade-off: a
# flaky network must not lock every rep out, but an offboarded rep's token
# must still die within TTL + this window even during a DB outage.
STALE_ROSTER_MAX_SECONDS = 900.0  # 15 minutes

# Column order mirrors the prospector.reps schema; WHERE active means a
# deactivated rep simply vanishes from the cache at the next refresh --
# there is no separate "revoke" path to forget.
_REPS_SQL = """
    SELECT id, email, display_name, hubspot_owner_id, token_hash,
           daily_credit_cap, daily_promote_cap, daily_t1_cap
      FROM prospector.reps
     WHERE active;
"""


def hash_token(token: str) -> str:
    """sha256 hex digest of a token -- the ONLY form a token ever takes at
    rest. Used both by the provisioning script (to write token_hash) and by
    authenticate() (to compare)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Rep:
    """One authenticated rep. Frozen: a Rep is a snapshot of the roster row
    at cache-refresh time, handed to route handlers -- nothing downstream
    may mutate identity or caps."""

    id: Any
    email: str
    display_name: str
    hubspot_owner_id: str
    token_hash: str
    daily_credit_cap: int
    daily_promote_cap: int
    daily_t1_cap: int


class RepRegistry:
    """TTL-cached view of the active reps, keyed by nothing -- lookups are
    a full scan on purpose (see the module docstring on timing).

    `db` needs only a .query(sql) -> list[dict] method, so tests inject a
    stub instead of a psycopg2 pool. `clock` defaults to time.monotonic and
    is injectable so TTL behavior is testable without sleeping.
    """

    def __init__(
        self,
        db: Any,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: tuple[Rep, ...] | None = None
        self._loaded_at: float = float("-inf")

    def authenticate(self, bearer_token: str | None) -> Rep | None:
        """Return the Rep whose token_hash matches the presented token, or
        None. Constant-time across the roster: hash once, compare against
        every cached rep, keep looping after a match."""
        # Hash even a missing token so the "no header" path does the same
        # work as the "wrong token" path (uniform cost, nothing to time).
        presented = hash_token(bearer_token or "").encode("ascii")

        matched: Rep | None = None
        for rep in self._active_reps():
            stored = (rep.token_hash or "").encode("utf-8")
            if secrets.compare_digest(presented, stored):
                matched = rep  # deliberately no break -- see docstring

        # An empty/missing token must never authenticate, even if a broken
        # provisioning run wrote hash("") into a roster row.
        if not bearer_token:
            return None
        return matched

    # -- cache ----------------------------------------------------------------

    def _active_reps(self) -> tuple[Rep, ...]:
        """The cached roster, refreshed from the DB when the TTL lapses.
        The refresh happens under the lock, which serializes concurrent
        expiries into one query instead of a stampede. If the DB is down,
        the stale cache is served only while it is at most
        STALE_ROSTER_MAX_SECONDS old (a flaky network must not lock every
        rep out, but an offboarded rep's token must still die within
        TTL + 15 minutes even during a DB outage); beyond that window, or
        with no cache at all, auth fails closed."""
        with self._lock:
            now = self._clock()
            if self._cache is not None and (now - self._loaded_at) < self._ttl:
                return self._cache

            try:
                rows = self._db.query(_REPS_SQL)
            except Exception:  # noqa: BLE001 - DB down must not crash auth
                # No identifying detail here on purpose: never tokens,
                # never hashes, and the exception text is the DB's problem.
                # Every failed refresh attempt logs at ERROR so a long
                # outage stays loud in the logs.
                age = now - self._loaded_at
                if self._cache is not None and age <= STALE_ROSTER_MAX_SECONDS:
                    logger.exception(
                        "refreshing the rep roster failed; serving the stale "
                        "cache (age %.0fs of %.0fs allowed)",
                        age,
                        STALE_ROSTER_MAX_SECONDS,
                    )
                    return self._cache
                logger.exception(
                    "refreshing the rep roster failed and %s -- failing "
                    "closed: ALL auth will fail until the DB is back",
                    "no cache exists yet"
                    if self._cache is None
                    else f"the cache is older than {STALE_ROSTER_MAX_SECONDS:.0f}s",
                )
                return ()

            self._cache = _usable_roster(rows)
            self._loaded_at = now
            logger.info("rep roster refreshed: %d active rep(s)", len(self._cache))
            return self._cache


def _usable_roster(rows: list[dict]) -> tuple[Rep, ...]:
    """Rows -> Reps, minus every rep in any group that shares a token_hash.
    Two active rows with one token means every request from that token
    would misattribute identity and spend, so ALL sharers are excluded
    until re-provisioned -- fail loud, not silent misattribution. The
    ERROR names rep EMAILS only, never the hash (a hash is a secret, see
    the module docstring)."""
    reps = [_row_to_rep(row) for row in rows]
    by_hash: dict[str, list[Rep]] = {}
    for rep in reps:
        by_hash.setdefault(rep.token_hash, []).append(rep)

    for group in by_hash.values():
        if len(group) > 1:
            logger.error(
                "duplicate token_hash in prospector.reps shared by: %s -- "
                "excluding ALL of them from the roster until re-provisioned",
                ", ".join(sorted(rep.email for rep in group)),
            )

    return tuple(rep for rep in reps if len(by_hash[rep.token_hash]) == 1)


def _row_to_rep(row: dict) -> Rep:
    return Rep(
        id=row["id"],
        email=str(row.get("email") or ""),
        display_name=str(row.get("display_name") or ""),
        hubspot_owner_id=str(row.get("hubspot_owner_id") or ""),
        token_hash=str(row.get("token_hash") or ""),
        daily_credit_cap=int(row.get("daily_credit_cap") or 0),
        daily_promote_cap=int(row.get("daily_promote_cap") or 0),
        daily_t1_cap=int(row.get("daily_t1_cap") or 0),
    )
