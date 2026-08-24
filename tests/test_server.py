"""The HTTP scaffold (prospector/server.py + prospector/auth.py).

These tests stand the REAL server up on an ephemeral port and talk to it
over a socket, because the things worth testing here are exactly what a
unit test of a handler method would fake away: the auth header, the 0.5s
failure penalty, the CORS headers, and the status codes.

The Database is a stub (no psycopg2 connection is ever opened): the
RepRegistry only needs .query() to return roster rows, so the stub hands
back canned dicts. Config is built in code -- this test never loads .env,
and every token below is synthetic.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time

import pytest

from prospector import server
from prospector.auth import RepRegistry, hash_token
from prospector.config import AppConfig

ORIGIN = "chrome-extension://abcdefghijklmnop"
BAD_ORIGIN = "https://evil.example.com"

# Synthetic tokens for this process only.
TOKEN_TJ = "test-token-tj-" + "0" * 24
TOKEN_INACTIVE = "test-token-inactive-" + "1" * 24  # NOT in the roster rows
TOKEN_WRONG = "test-token-wrong-" + "2" * 24

REP_ROWS = [
    {
        "id": 1,
        "email": "tj@example.com",
        "display_name": "TJ",
        "hubspot_owner_id": "111",
        "token_hash": hash_token(TOKEN_TJ),
        "daily_credit_cap": 50,
        "daily_promote_cap": 5,
        "daily_t1_cap": 3,
        "daily_research_cap": 10,
    },
]


class StubDatabase:
    """Only what RepRegistry touches: query(). No pool, no sockets."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.queries = 0

    def query(self, sql: str, params=None) -> list[dict]:
        self.queries += 1
        return [dict(row) for row in self.rows]


def _config(**overrides) -> AppConfig:
    kwargs = dict(
        database_url="postgresql://stub/stub?sslmode=require",
        extension_origin=ORIGIN,
        dry_run=True,
        host="127.0.0.1",
        port=0,  # ephemeral
    )
    kwargs.update(overrides)
    return AppConfig(**kwargs)


@pytest.fixture
def live_server():
    db = StubDatabase(REP_ROWS)
    registry = RepRegistry(db)
    httpd = server.build_server(_config(), db, registry)

    # A test-only POST route, injected through the route table exactly the
    # way a later phase would add one -- exercises Origin enforcement and
    # the body cap on a state-changing request.
    def _echo(request, rep, body):
        request.send_json(200, {"echo": body, "rep": rep.display_name})

    httpd.routes[("POST", "/echo")] = _echo

    # poll_interval keeps shutdown() from costing half a second per test
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        yield {"port": httpd.server_address[1], "httpd": httpd, "db": db}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(port, method, path, *, token=None, origin=None, body=None,
             raw_body=None, extra_headers=None):
    """(status, parsed_json, headers). Uses http.client so Content-Length
    is exact and no exception is raised for 4xx."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = dict(extra_headers or {})
    payload = raw_body
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    try:
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            parsed = {"_raw": raw.decode("utf-8", "replace")}
        return response.status, parsed, dict(response.getheaders())
    finally:
        conn.close()


def _header(headers: dict, name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


# -- /healthz -------------------------------------------------------------


def test_healthz_is_public_and_says_nothing_else(live_server):
    status, payload, _ = _request(live_server["port"], "GET", "/healthz")
    assert status == 200
    assert payload == {"ok": True}  # liveness ONLY: no version, no counts


def test_healthz_with_a_token_in_the_query_is_400(live_server):
    # The secret-in-query rejection runs BEFORE the public /healthz
    # shortcut: a credential pasted onto ANY path is already burned, and
    # /healthz must not be the one URL where that gets a friendly 200.
    status, payload, _ = _request(live_server["port"], "GET", "/healthz?token=x")
    assert status == 400
    assert "URL" in payload["error"]


# -- auth -------------------------------------------------------------------


def test_status_without_token_is_401_after_the_penalty(live_server):
    started = time.monotonic()
    status, payload, headers = _request(live_server["port"], "GET", "/status")
    elapsed = time.monotonic() - started
    assert status == 401
    assert payload == {"error": "unauthorized"}
    assert elapsed >= 0.4  # the 0.5s failure penalty, minus timer slack
    assert _header(headers, "WWW-Authenticate") is None  # nothing to probe


def test_wrong_bearer_is_401_after_the_penalty(live_server):
    started = time.monotonic()
    status, _, headers = _request(live_server["port"], "GET", "/status", token=TOKEN_WRONG)
    elapsed = time.monotonic() - started
    assert status == 401
    assert elapsed >= 0.4
    assert _header(headers, "WWW-Authenticate") is None


def test_token_of_a_rep_not_in_the_roster_is_401(live_server):
    # A perfectly well-formed token whose hash is not in the active-rep
    # cache (e.g. the rep was deactivated) must be indistinguishable from
    # a wrong token.
    status, payload, _ = _request(
        live_server["port"], "GET", "/status", token=TOKEN_INACTIVE
    )
    assert status == 401
    assert payload == {"error": "unauthorized"}


def test_status_with_a_valid_bearer(live_server):
    status, payload, _ = _request(live_server["port"], "GET", "/status", token=TOKEN_TJ)
    assert status == 200
    # Phase 2 added spend fields; assert the stable core exactly and the
    # spend fields by presence (their values need a jobs table).
    assert payload["rep"] == "TJ"
    assert payload["dry_run"] is True
    assert payload["caps"] == {"credit": 50, "promote": 5, "t1": 3}
    assert payload["version"] == server.VERSION
    assert "phase" not in payload
    assert "workspace_balance" in payload and "spent_today" in payload


def test_a_token_in_the_query_string_is_400_even_with_a_valid_bearer(live_server):
    status, payload, _ = _request(
        live_server["port"], "GET", f"/status?token={TOKEN_TJ}", token=TOKEN_TJ
    )
    assert status == 400
    assert "URL" in payload["error"]


def test_a_query_string_secret_never_appears_in_the_logs(live_server, caplog):
    # Per-rep tokens are unknown to the process in plaintext, so the
    # MaskedLogFilter can NEVER redact one from a request line -- the only
    # defense is that log_request/log_message log the bare path. Prove it:
    # the marker below must not show up in ANY record, on any logger.
    marker = "zq7SECRETMARKER7qz"
    with caplog.at_level(logging.INFO):
        _request(
            live_server["port"], "GET", f"/status?token={marker}", token=TOKEN_TJ
        )
        _request(live_server["port"], "GET", "/status", token=TOKEN_TJ)
    assert caplog.records  # the requests WERE logged...
    for record in caplog.records:
        assert marker not in record.getMessage()  # ...but never the query string


# -- CORS ---------------------------------------------------------------------


def test_options_preflight_with_the_pinned_origin(live_server):
    status, _, headers = _request(
        live_server["port"], "OPTIONS", "/status", origin=ORIGIN
    )
    assert status == 204
    assert _header(headers, "Access-Control-Allow-Origin") == ORIGIN
    assert _header(headers, "Access-Control-Allow-Methods") == "GET, POST, OPTIONS"
    assert _header(headers, "Access-Control-Allow-Headers") == "Authorization, Content-Type"
    assert _header(headers, "Access-Control-Max-Age") is not None


def test_options_preflight_with_a_mismatched_origin_is_204_without_cors(live_server):
    # A mismatched Origin still gets a 204 -- just WITHOUT the allow
    # headers, which is the browser's cue to block.
    status, _, headers = _request(
        live_server["port"], "OPTIONS", "/status", origin=BAD_ORIGIN
    )
    assert status == 204
    assert _header(headers, "Access-Control-Allow-Origin") is None
    assert _header(headers, "Access-Control-Allow-Methods") is None


def test_get_with_a_mismatched_origin_still_reaches_auth(live_server):
    # Origin enforcement is for state-changing requests only: a GET with
    # a foreign Origin is starved of CORS headers (so a browser can't
    # read it) but the request itself still runs auth like any other.
    status, payload, _ = _request(
        live_server["port"], "GET", "/status", origin=BAD_ORIGIN
    )
    assert status == 401
    assert payload == {"error": "unauthorized"}

    status, payload, headers = _request(
        live_server["port"], "GET", "/status", token=TOKEN_TJ, origin=BAD_ORIGIN
    )
    assert status == 200
    assert payload["rep"] == "TJ"
    assert _header(headers, "Access-Control-Allow-Origin") is None


def test_post_with_a_mismatched_origin_is_403_before_auth(live_server):
    # Even a VALID token does not get past the origin check: the 403 fires
    # before auth, so a hostile page can't even burn the failure delay.
    status, payload, headers = _request(
        live_server["port"], "POST", "/echo",
        token=TOKEN_TJ, origin=BAD_ORIGIN, body={"hello": 1},
    )
    assert status == 403
    assert payload == {"error": "origin not allowed"}
    assert _header(headers, "Access-Control-Allow-Origin") is None


def test_post_with_the_pinned_origin_succeeds_and_echoes_cors(live_server):
    status, payload, headers = _request(
        live_server["port"], "POST", "/echo",
        token=TOKEN_TJ, origin=ORIGIN, body={"hello": 1},
    )
    assert status == 200
    assert payload == {"echo": {"hello": 1}, "rep": "TJ"}
    # Every response whose Origin matches carries Allow-Origin, not just
    # the preflight -- or the extension could never read the body.
    assert _header(headers, "Access-Control-Allow-Origin") == ORIGIN


def test_post_with_no_origin_at_all_reaches_auth(live_server):
    # curl / server-to-server / tests send no Origin header; CORS only
    # ever constrains browsers, so these go straight through to auth.
    status, payload, _ = _request(
        live_server["port"], "POST", "/echo", token=TOKEN_TJ, body={"n": 2}
    )
    assert status == 200
    assert payload["echo"] == {"n": 2}


# -- request body ---------------------------------------------------------------


def test_body_over_16kb_is_413(live_server):
    status, payload, _ = _request(
        live_server["port"], "POST", "/echo",
        token=TOKEN_TJ, raw_body=b"x" * (16 * 1024 + 1),
    )
    assert status == 413
    assert "16384" in payload["error"]


def test_non_json_body_is_400(live_server):
    status, _, _ = _request(
        live_server["port"], "POST", "/echo", token=TOKEN_TJ, raw_body=b"not json"
    )
    assert status == 400


def test_transfer_encoding_is_refused(live_server):
    # A chunked body has no Content-Length, so the 16KB pre-read cap
    # cannot see it coming -- refused outright, and the connection closes
    # so the unread body cannot poison keep-alive.
    status, payload, headers = _request(
        live_server["port"], "POST", "/echo", token=TOKEN_TJ,
        extra_headers={"Transfer-Encoding": "chunked"},
    )
    assert status == 400
    assert payload == {"error": "transfer_encoding_unsupported"}
    assert (_header(headers, "Connection") or "").lower() == "close"


def test_post_without_content_length_is_400(live_server):
    # http.client adds Content-Length on its own, so drive putrequest/
    # putheader by hand to send a POST with neither Content-Length nor
    # body. Without the header the body would read as EMPTY -- and a
    # silently-empty body on a future spend endpoint means "all
    # defaults", which is exactly the wrong failure mode.
    conn = http.client.HTTPConnection("127.0.0.1", live_server["port"], timeout=10)
    try:
        conn.putrequest("POST", "/echo", skip_accept_encoding=True)
        conn.putheader("Authorization", f"Bearer {TOKEN_TJ}")
        conn.endheaders()  # deliberately NO Content-Length, no body
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 400
        assert payload == {"error": "content_length_required"}
        assert (response.getheader("Connection") or "").lower() == "close"
    finally:
        conn.close()


# -- routing --------------------------------------------------------------------


def test_unknown_path_with_valid_auth_is_404(live_server):
    status, payload, _ = _request(
        live_server["port"], "GET", "/nope", token=TOKEN_TJ
    )
    assert status == 404
    assert payload == {"error": "not found"}


def test_unknown_path_without_auth_is_401_not_404(live_server):
    # Anti-enumeration pin: auth runs BEFORE routing, so an
    # unauthenticated caller cannot map which paths exist by diffing
    # 401 against 404.
    status, payload, _ = _request(live_server["port"], "GET", "/nope")
    assert status == 401
    assert payload == {"error": "unauthorized"}


# -- RepRegistry TTL (no HTTP, injected clock) ------------------------------------


def test_registry_refreshes_on_ttl_not_before():
    clock = {"now": 0.0}
    db = StubDatabase([])  # roster starts empty
    registry = RepRegistry(db, ttl_seconds=60, clock=lambda: clock["now"])

    assert registry.authenticate(TOKEN_TJ) is None
    first_queries = db.queries

    # The rep is activated in the DB, but the cache is still warm: no
    # re-query, still unauthenticated.
    db.rows = REP_ROWS
    assert registry.authenticate(TOKEN_TJ) is None
    assert db.queries == first_queries

    # TTL lapses -> refresh picks the rep up.
    clock["now"] = 61.0
    rep = registry.authenticate(TOKEN_TJ)
    assert rep is not None and rep.display_name == "TJ"
    assert db.queries == first_queries + 1


def test_registry_never_authenticates_an_empty_token():
    # Even a poisoned roster row holding hash("") must not let a request
    # with no token through.
    rows = [dict(REP_ROWS[0], token_hash=hash_token(""))]
    registry = RepRegistry(StubDatabase(rows))
    assert registry.authenticate(None) is None
    assert registry.authenticate("") is None
