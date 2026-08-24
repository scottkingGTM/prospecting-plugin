"""Pure unit tests for prospector.database and the config helpers it leans
on -- NO live DB, no psycopg2 connection is ever opened.

Covers:
  * _advisory_key_to_bigint -- deterministic, in signed-64-bit range,
    high-bit digests don't raise, distinct keys differ
  * _ensure_sslmode (config) -- URL with/without params, keyword-DSN branch
  * MaskedLogFilter (config) -- masks a secret in msg and in args
  * cursor() semaphore -- released on every exception path (stub pool)
"""

from __future__ import annotations

import logging
import threading

import pytest

from prospector.config import MaskedLogFilter, _ensure_sslmode
from prospector.database import Database, _advisory_key_to_bigint


# -- _advisory_key_to_bigint ---------------------------------------------------


def test_advisory_key_is_deterministic():
    assert _advisory_key_to_bigint("job:abc") == _advisory_key_to_bigint("job:abc")


def test_advisory_key_stays_in_signed_64_bit_range():
    for key in ("", "a", "job:abc", "x" * 10_000, "unicode-éè字"):
        value = _advisory_key_to_bigint(key)
        assert -(2**63) <= value < 2**63


def test_advisory_key_high_bit_digest_does_not_raise():
    """A digest whose first byte has the high bit set maps to a NEGATIVE
    bigint (two's complement) -- it must come back as a valid signed value,
    not raise or overflow. Find such a key deterministically."""
    import hashlib

    key = next(
        f"key{i}"
        for i in range(1000)
        if hashlib.sha256(f"key{i}".encode()).digest()[0] & 0x80
    )
    value = _advisory_key_to_bigint(key)
    assert value < 0
    assert -(2**63) <= value < 2**63


def test_advisory_key_distinct_keys_differ():
    assert _advisory_key_to_bigint("lock:a") != _advisory_key_to_bigint("lock:b")


# -- _ensure_sslmode -------------------------------------------------------------


def test_ensure_sslmode_url_without_params_appends_query():
    assert (
        _ensure_sslmode("postgresql://user:pw@host:5432/db")
        == "postgresql://user:pw@host:5432/db?sslmode=require"
    )


def test_ensure_sslmode_url_with_params_appends_ampersand():
    assert (
        _ensure_sslmode("postgresql://user:pw@host:5432/db?application_name=x")
        == "postgresql://user:pw@host:5432/db?application_name=x&sslmode=require"
    )


def test_ensure_sslmode_existing_mode_untouched():
    url = "postgresql://user:pw@host:5432/db?sslmode=verify-full"
    assert _ensure_sslmode(url) == url


def test_ensure_sslmode_keyword_dsn_branch():
    assert (
        _ensure_sslmode("host=localhost user=me dbname=db ")
        == "host=localhost user=me dbname=db sslmode=require"
    )


# -- MaskedLogFilter ---------------------------------------------------------------


def _filtered_message(record: logging.LogRecord, secrets: tuple[str, ...]) -> str:
    filt = MaskedLogFilter(secrets)
    assert filt.filter(record) is True  # never drops records, only rewrites
    return record.getMessage()


def test_masked_filter_blanks_secret_in_msg():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1,
        "connecting with token s3cr3t-token-value", (), None,
    )
    out = _filtered_message(record, ("s3cr3t-token-value",))
    assert "s3cr3t-token-value" not in out
    assert "***REDACTED***" in out


def test_masked_filter_blanks_secret_in_args():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1,
        "connecting with token %s", ("s3cr3t-token-value",), None,
    )
    out = _filtered_message(record, ("s3cr3t-token-value",))
    assert "s3cr3t-token-value" not in out
    assert "***REDACTED***" in out


# -- cursor() semaphore release ------------------------------------------------------


class _StubCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        pass


class _StubConn:
    closed = 0

    def cursor(self):
        return _StubCursor()

    def commit(self):
        pass

    def rollback(self):
        pass


class _StubPool:
    """Stands in for ThreadedConnectionPool: hands out one stub connection
    and records putconn calls so the tests can assert on the return path."""

    def __init__(self, getconn_error: Exception | None = None) -> None:
        self.getconn_error = getconn_error
        self.putconn_calls: list[tuple] = []

    def getconn(self):
        if self.getconn_error is not None:
            raise self.getconn_error
        return _StubConn()

    def putconn(self, conn, close=False):
        self.putconn_calls.append((conn, close))


def _make_db(pool: _StubPool, slots: int = 1) -> Database:
    """Build a Database around a stub pool WITHOUT running __init__ (which
    would open real sockets)."""
    db = Database.__new__(Database)
    db._dsn = "stub"
    db._pool = pool
    db._pool_size = slots
    db._checkout = threading.BoundedSemaphore(slots)
    db._lock_conns = {}
    db._lock_conns_guard = threading.Lock()
    return db


def _slot_is_free(db: Database) -> bool:
    """True if the single semaphore slot can be re-acquired -- i.e. every
    prior cursor() released it. BoundedSemaphore would raise on
    over-release, so acquire+release here also proves no double-release."""
    acquired = db._checkout.acquire(blocking=False)
    if acquired:
        db._checkout.release()
    return acquired


def test_semaphore_released_after_clean_block():
    pool = _StubPool()
    db = _make_db(pool)
    with db.cursor() as cur:
        cur.execute("SELECT 1")
    assert _slot_is_free(db)
    assert len(pool.putconn_calls) == 1


def test_semaphore_released_when_block_raises():
    pool = _StubPool()
    db = _make_db(pool)
    with pytest.raises(RuntimeError, match="boom"):
        with db.cursor():
            raise RuntimeError("boom")
    assert _slot_is_free(db)
    # The connection still went back to the pool on the exception path.
    assert len(pool.putconn_calls) == 1


def test_semaphore_released_when_getconn_raises():
    pool = _StubPool(getconn_error=ConnectionError("pool is down"))
    db = _make_db(pool)
    with pytest.raises(ConnectionError):
        with db.cursor():
            pass  # pragma: no cover - never reached
    assert _slot_is_free(db)
    assert pool.putconn_calls == []  # nothing was checked out


def test_exhausted_pool_raises_clear_error(monkeypatch):
    """With the only slot held, cursor() must raise the explicit
    pool-exhausted RuntimeError (timeout shrunk so the test stays fast)."""
    pool = _StubPool()
    db = _make_db(pool, slots=1)
    monkeypatch.setattr(Database, "_CHECKOUT_TIMEOUT_S", 0.05)
    assert db._checkout.acquire(blocking=False)  # hold the only slot
    try:
        with pytest.raises(RuntimeError, match="connection pool exhausted"):
            with db.cursor():
                pass  # pragma: no cover - never reached
    finally:
        db._checkout.release()
