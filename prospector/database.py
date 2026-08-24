"""Supabase (Postgres) access for prospecting_plugin.

psycopg2 + RealDictCursor, conventions carried over from an earlier
enrichment-waterfall database layer -- with ONE deliberate
departure: this server is THREADED (concurrent HTTP requests from the
extension), so instead of the waterfall's single long-lived connection it
uses a psycopg2 ThreadedConnectionPool. Every unit of work checks a
connection out, runs inside one transaction, and returns it; no request can
tangle another request's transaction state.

This module is I/O only: it hands back plain dict rows and otherwise knows
nothing about owners, providers, or waterfall logic.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

# Expected privilege per prospector-schema table, mirroring
# sql/02_create_role.sql exactly. Config tables (reps, providers,
# waterfalls) are SELECT-only — the service reads its guardrails, never
# rewrites them; the working tables get S/I/U; recognize_cache additionally
# needs DELETE (cache eviction is the only hard delete in the system).
# preflight() demands each listed privilege AND treats INSERT/UPDATE on a
# config table as fatal over-privilege.
_PROSPECTOR_TABLE_PRIVS: dict[str, tuple[str, ...]] = {
    "reps": ("SELECT",),
    "providers": ("SELECT",),
    "waterfalls": ("SELECT",),
    "jobs": ("SELECT", "INSERT", "UPDATE"),
    "attempts": ("SELECT", "INSERT", "UPDATE"),
    "events": ("SELECT", "INSERT", "UPDATE"),
    "recognize_cache": ("SELECT", "INSERT", "UPDATE", "DELETE"),
}

# The SELECT-only config tables above; INSERT or UPDATE on any of these is
# a deploy blocker (a leaked token must not be able to mint reps or raise
# its own spend caps).
_PROSPECTOR_CONFIG_TABLES = ("reps", "providers", "waterfalls")


def _advisory_key_to_bigint(key: str) -> int:
    """Hash an arbitrary string key to the signed 64-bit integer that
    pg_try_advisory_lock() wants. sha256 (not hashtext()) so the mapping is
    stable across Postgres versions and computable from Python for
    debugging: first 8 digest bytes, big-endian, two's-complement signed."""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class Database:
    """Thread-safe Postgres access on a connection pool.

    Unlike the waterfall's single-connection Database (a sequential batch
    job), this one backs a threaded HTTP server: `pool_size` concurrent
    checkouts, each transactional via cursor(). The pool is created with
    minconn == maxconn on purpose: psycopg2 CLOSES a returned connection
    whenever more than minconn are open, so an elastic pool would churn a
    fresh TCP+TLS+auth handshake on every concurrent request. Server worker
    threads may exceed the pool slots; a BoundedSemaphore gates getconn so
    excess threads queue (up to 10s) for a slot instead of erroring
    instantly. Advisory locks live on their own dedicated connections
    OUTSIDE the pool (see try_advisory_lock), because a session-level lock
    must not have its lifetime tangled with pooled checkouts.
    """

    _CHECKOUT_TIMEOUT_S = 10

    def __init__(self, dsn: str, pool_size: int = 6) -> None:
        self._dsn = dsn
        self._pool_size = pool_size
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            pool_size,  # minconn == maxconn: see class docstring
            pool_size,
            dsn,
            cursor_factory=psycopg2.extras.RealDictCursor,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
        # Gates getconn(): threads beyond pool_size queue here instead of
        # hitting the pool's instant PoolError.
        self._checkout = threading.BoundedSemaphore(pool_size)
        # key -> dedicated autocommit connection currently holding that lock.
        self._lock_conns: dict[str, psycopg2.extensions.connection] = {}
        # Guards _lock_conns: try/release_advisory_lock may be called from
        # multiple server threads.
        self._lock_conns_guard = threading.Lock()

    def close(self) -> None:
        with self._lock_conns_guard:
            held_keys = list(self._lock_conns)
        for key in held_keys:
            self.release_advisory_lock(key)
        try:
            self._pool.closeall()
        except Exception:
            pass

    # -- transactional unit of work ----------------------------------------

    @contextmanager
    def cursor(self) -> Iterator[psycopg2.extras.RealDictCursor]:
        """Check a connection out of the pool and yield a RealDictCursor on
        it. Commits when the block exits cleanly, rolls back on any
        exception, and always returns the connection to the pool. A
        connection that died mid-block is discarded instead of returned, so
        one dropped socket never poisons the pool.

        Checkout is gated by a semaphore sized to the pool: when every slot
        is busy, this blocks up to _CHECKOUT_TIMEOUT_S and then raises a
        clear RuntimeError instead of psycopg2's instant PoolError. The
        semaphore is released on EVERY path that returns the connection,
        exceptions included."""
        if not self._checkout.acquire(timeout=self._CHECKOUT_TIMEOUT_S):
            raise RuntimeError(
                f"connection pool exhausted ({self._pool_size} slots, "
                f"waited {self._CHECKOUT_TIMEOUT_S}s)"
            )
        try:
            conn = self._pool.getconn()
        except Exception:
            self._checkout.release()
            raise
        broken = False
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                broken = True
            raise
        finally:
            try:
                self._pool.putconn(conn, close=broken or conn.closed != 0)
            except Exception:
                logger.warning(
                    "failed to return connection to pool (broken=%s)",
                    broken, exc_info=True,
                )
            finally:
                self._checkout.release()

    def query(self, sql: str, params: Any = None) -> list[dict]:
        """Run one SELECT in its own transaction; return rows as dicts."""
        with self.cursor() as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def execute(self, sql: str, params: Any = None) -> int:
        """Run one statement in its own transaction; return its rowcount."""
        with self.cursor() as cur:
            cur.execute(sql, params)
            rowcount = cur.rowcount
        return rowcount if rowcount is not None and rowcount >= 0 else 0

    # -- advisory locks ------------------------------------------------------

    def try_advisory_lock(self, key: str) -> bool:
        """Acquire an advisory lock for `key` on its own DEDICATED
        connection, separate from the pool. That is deliberate (copied from
        the waterfall): a session-level advisory lock lives and dies with
        its connection, so holding it on a pooled connection would tangle
        the lock's lifetime with unrelated checkout/putconn logic. If this
        process crashes outright, Postgres drops the connection and the
        lock releases itself -- a wedged container can never lock out the
        next run.

        Returns False for "not acquired" WITHOUT distinguishing why: the
        lock is either held elsewhere (another session) OR already held by
        this very instance. Callers must treat False as "someone has it",
        never as "safe to retry immediately"."""
        with self._lock_conns_guard:
            if key in self._lock_conns:
                return False  # already held by this instance

        conn = psycopg2.connect(
            self._dsn,
            cursor_factory=psycopg2.extras.RealDictCursor,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s) AS locked;",
                    (_advisory_key_to_bigint(key),),
                )
                row = cur.fetchone()
                locked = bool(row["locked"]) if row else False
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise

        if not locked:
            try:
                conn.close()
            except Exception:
                pass
            return False

        with self._lock_conns_guard:
            self._lock_conns[key] = conn
        return True

    def release_advisory_lock(self, key: str) -> None:
        with self._lock_conns_guard:
            conn = self._lock_conns.pop(key, None)
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_unlock(%s);",
                    (_advisory_key_to_bigint(key),),
                )
        except Exception:
            logger.warning(
                "advisory unlock failed for key=%s (connection likely already dead)",
                key, exc_info=True,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # -- preflight ------------------------------------------------------------

    def preflight(self) -> tuple[list[str], list[str]]:
        """Sanity-check this role's live privileges and seed data before the
        server takes a single request. Returns (fatal, warnings): any fatal
        entry is a deploy blocker; warnings are logged and allowed through.

        Every check runs in its own transaction and its own try/except, so
        one failing check (e.g. a table that does not exist yet) reports
        itself without masking the checks after it.
        """
        fatal: list[str] = []
        warnings: list[str] = []

        def one_value(sql: str, params: Any = None) -> Any:
            rows = self.query(sql, params)
            return next(iter(rows[0].values())) if rows else None

        # -- who are we? (needed for the postgres dev exception below) -----
        current_user = "unknown"
        try:
            current_user = one_value("SELECT current_user AS u;")
        except Exception as exc:
            fatal.append(f"could not determine current_user ({type(exc).__name__}: {exc})")

        # -- schema USAGE: without it every table grant is unreachable -------
        try:
            ok = one_value(
                "SELECT has_schema_privilege(current_user, %s, 'USAGE') AS ok;",
                ("prospector",),
            )
            if not ok:
                fatal.append("no USAGE privilege on schema prospector")
        except Exception as exc:
            fatal.append(
                f"could not check USAGE on schema prospector ({type(exc).__name__}: {exc})"
            )

        # -- prospector schema: exactly the expected privileges --------------
        for table, privs in _PROSPECTOR_TABLE_PRIVS.items():
            qualified = f"prospector.{table}"
            for priv in privs:
                try:
                    ok = one_value(
                        "SELECT has_table_privilege(current_user, %s, %s) AS ok;",
                        (qualified, priv),
                    )
                    if not ok:
                        fatal.append(f"no {priv} privilege on {qualified}")
                except Exception as exc:
                    fatal.append(
                        f"could not check {priv} on {qualified} ({type(exc).__name__}: {exc})"
                    )
                    break  # table itself is broken/missing; move to next table

        # -- NEGATIVE checks: config tables must be read-only -----------------
        # INSERT or UPDATE on reps/providers/waterfalls means a leaked service
        # token could mint reps or raise its own spend caps. Over-privilege
        # here is a deploy blocker, with a postgres-dev-role exception (the
        # superuser trips every privilege check by definition).
        for table in _PROSPECTOR_CONFIG_TABLES:
            qualified = f"prospector.{table}"
            for priv in ("INSERT", "UPDATE"):
                try:
                    has = one_value(
                        "SELECT has_table_privilege(current_user, %s, %s) AS ok;",
                        (qualified, priv),
                    )
                    if has:
                        msg = (
                            f"role '{current_user}' has {priv} on {qualified} -- "
                            "config tables must be SELECT-only for this role "
                            "(see sql/02_create_role.sql)"
                        )
                        if current_user == "postgres":
                            warnings.append(
                                msg + " [postgres dev role -- fine locally, "
                                "must NOT be true of the production role]"
                            )
                        else:
                            fatal.append(msg)
                except Exception as exc:
                    fatal.append(
                        f"could not check {priv} on {qualified} ({type(exc).__name__}: {exc})"
                    )
                    break

        # -- real-access probes: privileges can lie, a SELECT cannot ----------
        # has_table_privilege() does not see missing schema USAGE or an RLS
        # policy gap (a trap that has bitten us on another table before).
        # `SELECT ... LIMIT 0` exercises the whole path -- USAGE + grant +
        # RLS -- without reading a row. Table names come from our own tuple,
        # never from input, so the f-string interpolation is safe.
        for table in _PROSPECTOR_TABLE_PRIVS:
            try:
                self.query(f"SELECT 1 FROM prospector.{table} LIMIT 0;")
            except Exception as exc:
                fatal.append(
                    f"cannot actually SELECT from prospector.{table} "
                    f"({type(exc).__name__}: {exc}) -- privilege grants may "
                    "exist but access is broken (schema USAGE / RLS / missing table)"
                )

        # -- seed-data sanity -------------------------------------------------
        try:
            active_reps = int(
                one_value("SELECT count(*) AS n FROM prospector.reps WHERE active;") or 0
            )
            if active_reps == 0:
                warnings.append("no active reps -- all requests will 401")
        except Exception as exc:
            warnings.append(
                f"could not count active prospector.reps ({type(exc).__name__}: {exc})"
            )

        return fatal, warnings
