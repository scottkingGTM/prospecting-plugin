"""Integration tests for POST /recognize — the real server on an ephemeral
port, a stub Database (with a working recognize_cache emulation), and a stub
HubSpot client injected via the server.hubspot_client attribute.
"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from prospector.auth import RepRegistry, hash_token
from prospector.server import build_server

TOKEN = "t" * 40


class StubDb:
    """Just enough Database for RepRegistry + the recognize cache + events."""

    def __init__(self) -> None:
        self.cache: dict[str, dict] = {}
        self.events: list[tuple] = []

    def query(self, sql: str, params=None) -> list[dict]:
        if "FROM prospector.reps" in sql:
            return [{
                "id": 1, "email": "rep@example.com", "display_name": "Rep",
                "hubspot_owner_id": "42", "token_hash": hash_token(TOKEN),
                "daily_credit_cap": 50, "daily_promote_cap": 25,
                "daily_t1_cap": 3, "daily_research_cap": 3,
            }]
        if "FROM prospector.recognize_cache" in sql:
            row = self.cache.get(params["key"])
            if not row:
                return []
            # psycopg2 decodes jsonb to a dict; the stub stores the JSON
            # string the app sent, so decode here to match the real driver.
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            return [{"payload": payload, "cache_age_s": 0}]
        if "v_account_360_mcp" in sql:
            return []  # pending_sync path
        return []

    def execute(self, sql: str, params=None) -> int:
        if "INSERT INTO prospector.recognize_cache" in sql:
            self.cache[params["key"]] = {"payload": params["payload"]}
            return 1
        if "INSERT INTO prospector.events" in sql:
            self.events.append(params)
            return 1
        return 0


class StubHubSpot:
    def __init__(self) -> None:
        self.calls = 0

    def find_contact_by_linkedin(self, norm_url: str):
        self.calls += 1
        return {
            "id": "301", "firstname": "Dana", "lastname": "Ops",
            "jobtitle": "VP Operations", "email": "dana@acme.com",
            "hubspot_owner_id": "42", "lifecyclestage": "lead",
            "lastmodifieddate": "2026-08-01T00:00:00Z",
            "associatedcompanyid": None,
        }

    def find_companies_by_domain(self, domain: str):
        self.calls += 1
        return []

    def find_company_by_linkedin_slug(self, slug: str):
        self.calls += 1
        return []

    def get_owner(self, owner_id: str):
        return {"id": owner_id, "firstName": "Nick", "lastName": "AE",
                "email": "nick@example.com"}

    def contact_hubspot_url(self, contact_id: str):
        return None

    def company_hubspot_url(self, company_id: str):
        return None


class _Cfg:
    extension_origin = ""
    dry_run = True
    hubspot_token = ""       # deliberately empty: injection must win
    hubspot_portal_id = ""
    host = "127.0.0.1"
    port = 0


@pytest.fixture()
def server_url():
    db = StubDb()
    registry = RepRegistry(db)
    server = build_server(_Cfg(), db, registry)
    server.hubspot_client = StubHubSpot()
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}", server, db
    server.shutdown()
    server.server_close()


def _post(url: str, path: str, body: dict, token: str | None = TOKEN):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code, json.loads(e.read().decode())


import urllib.error  # noqa: E402  (used in _post's except clause)


def test_recognize_profile_green(server_url):
    url, server, db = server_url
    status, body = _post(url, "/recognize",
                         {"surface": "linkedin_profile",
                          "url": "https://www.linkedin.com/in/dana-ops/"})
    assert status == 200
    assert body["verdict"] == "green"
    assert body["contact"]["name"].startswith("Dana")
    # one recognize event logged for the uncached lookup
    assert len(db.events) == 1


def test_recognize_cached_second_call_skips_hubspot(server_url):
    url, server, db = server_url
    _post(url, "/recognize", {"url": "https://www.linkedin.com/in/dana-ops/"})
    calls_after_first = server.hubspot_client.calls
    status, body = _post(url, "/recognize",
                         {"url": "https://m.linkedin.com/in/dana-ops"})
    assert status == 200
    assert body["cached"] is True
    assert server.hubspot_client.calls == calls_after_first  # m. collapsed, served from cache
    assert len(db.events) == 1  # cached hits are not re-logged


def test_recognize_idle_surfaces_no_auth_to_hubspot(server_url):
    url, server, db = server_url
    for u in ("https://www.linkedin.com/feed/", "https://www.google.com/search?q=x"):
        status, body = _post(url, "/recognize", {"url": u})
        assert status == 200
        assert body["verdict"] == "idle"
    assert server.hubspot_client.calls == 0


def test_recognize_sales_nav_unsupported(server_url):
    url, server, db = server_url
    status, body = _post(url, "/recognize",
                         {"url": "https://www.linkedin.com/sales/lead/ACwAAA,NAME,x"})
    assert status == 200
    assert body["verdict"] == "unsupported_surface"
    assert server.hubspot_client.calls == 0


def test_recognize_requires_url(server_url):
    url, server, db = server_url
    status, body = _post(url, "/recognize", {})
    assert status == 400
    assert body["error"] == "url_required"


def test_recognize_requires_auth(server_url):
    url, server, db = server_url
    status, body = _post(url, "/recognize",
                         {"url": "https://www.linkedin.com/in/x"}, token=None)
    assert status == 401


def test_recognize_page_title_flows_through_to_name_hint(server_url):
    """The tab title reaches recognize() and comes back as name_hint --
    the fix for the mangled-slug autofill case."""
    url, server, db = server_url
    status, body = _post(url, "/recognize", {
        "url": "https://www.linkedin.com/in/dana-ops/",
        "page_title": "Dana Ops - VP Operations at Acme | LinkedIn",
    })
    assert status == 200
    assert body["name_hint"] == {
        "full_name": "Dana Ops", "first_name": "Dana", "last_name": "Ops",
    }


def test_recognize_invalid_page_title_is_ignored_silently(server_url):
    """Non-string or oversized page_title never fails the lookup -- it is
    dropped and the response simply carries no name_hint."""
    url, server, db = server_url
    for bad_title in (123, ["x"], {"t": 1}, "x" * 301):
        status, body = _post(url, "/recognize", {
            "url": "https://www.linkedin.com/in/dana-ops/",
            "page_title": bad_title,
            "force_refresh": True,  # each try does a real lookup
        })
        assert status == 200
        assert body["verdict"] == "green"
        assert "name_hint" not in body


def test_recognize_cached_hit_gains_hint_from_a_later_title(server_url):
    """First call caches hint-less (title not sent); the second call, with
    a title, is still served from cache -- no new HubSpot call -- but now
    carries the name_hint."""
    url, server, db = server_url
    _post(url, "/recognize", {"url": "https://www.linkedin.com/in/dana-ops/"})
    calls_after_first = server.hubspot_client.calls
    status, body = _post(url, "/recognize", {
        "url": "https://www.linkedin.com/in/dana-ops/",
        "page_title": "Dana Ops | LinkedIn",
    })
    assert status == 200
    assert body["cached"] is True
    assert body["name_hint"]["first_name"] == "Dana"
    assert server.hubspot_client.calls == calls_after_first


def test_recognize_503_when_hubspot_unconfigured(server_url):
    url, server, db = server_url
    server.hubspot_client = None  # remove injection; cfg token is empty
    status, body = _post(url, "/recognize",
                         {"url": "https://www.linkedin.com/in/someone"})
    assert status == 503
    assert body["error"] == "hubspot_not_configured"
    # No env var name in the detail -- it names the capability, not the config.
    assert body["detail"] == "HubSpot is not configured on this deployment"
