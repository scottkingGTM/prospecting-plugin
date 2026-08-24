"""EXTENSION_ORIGIN accepts a comma-separated origin list:
unpacked extensions carry per-machine ids, so the pre-Web-Store rollout
needs one origin per rep. Any listed origin passes CORS; others are 403'd
on POST exactly like before.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from prospector.auth import RepRegistry, hash_token
from prospector.server import build_server

TOKEN = "m" * 40
ORIGIN_A = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ORIGIN_B = "chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ORIGIN_EVIL = "chrome-extension://cccccccccccccccccccccccccccccccc"


class StubDb:
    def query(self, sql, params=None):
        if "FROM prospector.reps" in sql:
            return [{"id": 1, "email": "r@x.com", "display_name": "R",
                     "hubspot_owner_id": "1", "token_hash": hash_token(TOKEN),
                     "daily_credit_cap": 50, "daily_promote_cap": 25,
                     "daily_t1_cap": 3, "daily_research_cap": 3}]
        return []

    def execute(self, sql, params=None):
        return 1


class _Cfg:
    extension_origin = f"{ORIGIN_A}, {ORIGIN_B}"
    dry_run = True
    hubspot_token = ""
    hubspot_portal_id = ""
    fullenrich_api_key = ""
    host = "127.0.0.1"
    port = 0


@pytest.fixture()
def url():
    db = StubDb()
    server = build_server(_Cfg(), db, RepRegistry(db))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def _post(url, origin):
    req = urllib.request.Request(
        url + "/recognize", data=json.dumps({"url": "x"}).encode(),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {TOKEN}", "Origin": origin})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)


def test_both_listed_origins_pass_cors(url):
    for origin in (ORIGIN_A, ORIGIN_B):
        status, headers = _post(url, origin)
        assert status != 403
        assert headers.get("Access-Control-Allow-Origin") == origin


def test_unlisted_origin_still_403s(url):
    status, _ = _post(url, ORIGIN_EVIL)
    assert status == 403
