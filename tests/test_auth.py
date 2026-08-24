"""RepRegistry roster behavior (prospector/auth.py): the stale-cache
window, the fail-closed paths, and duplicate-token detection.

No HTTP here -- the registry is driven directly with an injected clock and
a stub db, so an outage is one flag flip and fifteen minutes is one
assignment, not a sleep. Every token below is synthetic.
"""

from __future__ import annotations

import logging

from prospector.auth import STALE_ROSTER_MAX_SECONDS, RepRegistry, hash_token

TOKEN_TJ = "test-token-tj-" + "3" * 24
TOKEN_NICK = "test-token-nick-" + "4" * 24
TOKEN_WILL = "test-token-will-" + "5" * 24


def _row(rep_id: int, email: str, token: str, **overrides) -> dict:
    row = {
        "id": rep_id,
        "email": email,
        "display_name": email.split("@")[0].title(),
        "hubspot_owner_id": str(100 + rep_id),
        "token_hash": hash_token(token),
        "daily_credit_cap": 50,
        "daily_promote_cap": 5,
        "daily_t1_cap": 3,
        "daily_research_cap": 10,
    }
    row.update(overrides)
    return row


class FlakyDatabase:
    """query() returns canned rows until .down is set, then raises --
    simulating a DB outage without a psycopg2 pool anywhere near it."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.down = False
        self.queries = 0

    def query(self, sql: str, params=None) -> list[dict]:
        self.queries += 1
        if self.down:
            raise ConnectionError("synthetic outage")
        return [dict(row) for row in self.rows]


def _registry(db: FlakyDatabase, clock: dict) -> RepRegistry:
    return RepRegistry(db, ttl_seconds=60, clock=lambda: clock["now"])


# -- the stale-roster window --------------------------------------------------


def test_stale_cache_within_the_window_still_serves():
    clock = {"now": 0.0}
    db = FlakyDatabase([_row(1, "tj@example.com", TOKEN_TJ)])
    registry = _registry(db, clock)
    assert registry.authenticate(TOKEN_TJ) is not None  # cache loaded at t=0

    # TTL lapses, the refresh fails, but the cache is only 61s old: a
    # flaky network must not lock every rep out.
    db.down = True
    clock["now"] = 61.0
    assert registry.authenticate(TOKEN_TJ) is not None


def test_stale_cache_beyond_the_window_fails_closed(caplog):
    clock = {"now": 0.0}
    db = FlakyDatabase([_row(1, "tj@example.com", TOKEN_TJ)])
    registry = _registry(db, clock)
    assert registry.authenticate(TOKEN_TJ) is not None

    # Cache is now older than STALE_ROSTER_MAX_SECONDS and the DB is
    # still down: an offboarded rep's token must die within TTL+15min
    # even during an outage, so auth fails closed -- and EVERY refresh
    # attempt logs an ERROR, not just the first.
    db.down = True
    clock["now"] = STALE_ROSTER_MAX_SECONDS + 1.0
    with caplog.at_level(logging.ERROR, logger="prospector.auth"):
        assert registry.authenticate(TOKEN_TJ) is None
        assert registry.authenticate(TOKEN_TJ) is None
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 2


def test_empty_roster_fails_closed():
    registry = _registry(FlakyDatabase([]), {"now": 0.0})
    assert registry.authenticate(TOKEN_TJ) is None


# -- duplicate token_hash -------------------------------------------------------


def test_duplicate_token_hash_excludes_both_and_spares_the_rest(caplog):
    rows = [
        _row(1, "tj@example.com", TOKEN_TJ),
        _row(2, "nick@example.com", TOKEN_TJ),  # same token as TJ: provisioning bug
        _row(3, "will@example.com", TOKEN_WILL),
    ]
    registry = _registry(FlakyDatabase(rows), {"now": 0.0})
    with caplog.at_level(logging.ERROR, logger="prospector.auth"):
        assert registry.authenticate(TOKEN_TJ) is None       # BOTH sharers out
        assert registry.authenticate(TOKEN_WILL) is not None  # bystander unaffected

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        "tj@example.com" in msg and "nick@example.com" in msg for msg in errors
    ), "the ERROR must name the rep emails so the fix is obvious"
    shared_hash = hash_token(TOKEN_TJ)
    assert all(shared_hash not in msg for msg in errors)  # emails, NEVER the hash


# -- deactivation ---------------------------------------------------------------


def test_deactivated_rep_disappears_after_the_ttl_refresh():
    clock = {"now": 0.0}
    db = FlakyDatabase([_row(1, "tj@example.com", TOKEN_TJ)])
    registry = _registry(db, clock)
    assert registry.authenticate(TOKEN_TJ) is not None

    # Deactivated in the DB (WHERE active drops the row), but the cache
    # is still warm: still in.
    db.rows = []
    clock["now"] = 30.0
    assert registry.authenticate(TOKEN_TJ) is not None

    # TTL lapses -> the next refresh forgets the rep.
    clock["now"] = 61.0
    assert registry.authenticate(TOKEN_TJ) is None
