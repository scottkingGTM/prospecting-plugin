"""Route-layer tests for POST /resolve and POST /commit — the routes are
thin maps over resolve.py / writer.py (both heavily tested); these pin the
HTTP contract: shapes, error mapping, auth, and the hubspot_url add-on.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from prospector.auth import RepRegistry, hash_token
from prospector.server import build_server
from prospector import writer as writer_mod

TOKEN = "r" * 40


class StubDb:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def query(self, sql: str, params=None) -> list[dict]:
        if "FROM prospector.reps" in sql:
            return [{
                "id": 1, "email": "rep@example.com", "display_name": "Rep",
                "hubspot_owner_id": "42", "token_hash": hash_token(TOKEN),
                "daily_credit_cap": 50, "daily_promote_cap": 25,
                "daily_t1_cap": 3, "daily_research_cap": 3,
            }]
        return []

    def execute(self, sql: str, params=None) -> int:
        if "INSERT INTO prospector.events" in sql:
            self.events.append((sql, params))
        return 1


class StubHubSpot:
    """Only what /resolve touches."""

    def __init__(self) -> None:
        self.slug_calls: list[str] = []

    def find_contact_by_linkedin(self, norm_url):
        return None

    def find_contacts_by_emails(self, emails):
        return [{"id": "77", "firstname": "Dana", "lastname": "Ops",
                 "email": emails[0], "matched_email": emails[0],
                 "jobtitle": "VP Ops", "hs_linkedin_url": "",
                 "hubspot_owner_id": "42"}]

    def find_contacts_by_name(self, first, last):
        return []

    def find_companies_by_domain(self, domain):
        return [{"id": "900", "name": "Acme Pest", "domain": domain,
                 "state": "TX", "hs_ideal_customer_profile": "tier_2",
                 "hs_is_target_account": "true",
                 "linkedin_company_page": ""}]

    def find_company_by_linkedin_slug(self, slug):
        self.slug_calls.append(slug)
        return []

    def search_companies_fuzzy(self, token):
        return []

    def contact_hubspot_url(self, cid):
        return f"https://app.hubspot.com/contacts/1/record/0-1/{cid}"

    def company_hubspot_url(self, cid):
        return f"https://app.hubspot.com/contacts/1/record/0-2/{cid}"


class _Cfg:
    extension_origin = ""
    dry_run = True
    hubspot_token = ""
    hubspot_portal_id = ""
    fullenrich_api_key = ""
    host = "127.0.0.1"
    port = 0


@pytest.fixture()
def rig():
    db = StubDb()
    server = build_server(_Cfg(), db, RepRegistry(db))
    server.hubspot_client = StubHubSpot()
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}", server, db
    server.shutdown()
    server.server_close()


def _post(url, path, body, token=TOKEN):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_resolve_shape_and_hubspot_urls(rig):
    url, server, db = rig
    status, body = _post(url, "/resolve", {
        "contact": {"first_name": "Dana", "last_name": "Ops",
                    "email": "dana@acmepest.com"},
        "company": {"name": "Acme Pest", "domain": "acmepest.com",
                    "state": "Texas"},
    })
    assert status == 200
    assert body["contact_matches"][0]["matched_on"] == "email"
    assert body["contact_matches"][0]["hubspot_url"].endswith("/0-1/77")
    assert body["company_matches"][0]["hubspot_url"].endswith("/0-2/900")
    assert body["company_matches"][0]["preferred"] is True
    # resolve is logged
    assert any("'resolve'" in p[0] or "resolve" in str(p[1]) for p in db.events)


def test_resolve_extracts_bare_slug_from_full_company_url(rig):
    """The panel sends the FULL company-page URL; the route must hand the
    slug chain the bare /company/<slug> slug or the exact-slug post-filter
    matches nothing."""
    url, server, db = rig
    status, _ = _post(url, "/resolve", {
        "contact": {},
        "company": {
            "linkedin_url": "https://www.linkedin.com/company/acme-corp/",
        },
    })
    assert status == 200
    assert server.hubspot_client.slug_calls == ["acme-corp"]


def test_resolve_passes_bare_slug_input_through(rig):
    url, server, db = rig
    status, _ = _post(url, "/resolve", {
        "contact": {},
        "company": {"linkedin_slug": "acme-corp"},
    })
    assert status == 200
    assert server.hubspot_client.slug_calls == ["acme-corp"]


def test_resolve_input_validation(rig):
    url, server, db = rig
    status, body = _post(url, "/resolve", {"contact": {"email": "x" * 300},
                                           "company": {}})
    assert (status, body["error"]) == (400, "input_invalid")


def test_resolve_requires_auth(rig):
    url, server, db = rig
    status, _ = _post(url, "/resolve", {"contact": {}, "company": {}},
                      token=None)
    assert status == 401


def test_commit_maps_rejection_to_http(rig, monkeypatch):
    url, server, db = rig

    def _boom(db_, hubspot, rep, cfg, body):
        raise writer_mod.CommitRejected(422, "inferred_email",
                                        {"message": "blocked"})
    monkeypatch.setattr(writer_mod, "commit", _boom)
    status, body = _post(url, "/commit", {"confirm": True})
    assert (status, body["error"]) == (422, "inferred_email")
    assert body["message"] == "blocked"


def test_commit_confirm_false_returns_plan_under_preview_key(rig):
    """The pinned wire contract for confirm:false is {"preview": {<plan>}}
    -- the panel renders data.preview, so anything but the plan dict under
    that key regresses the panel to "(empty preview)". Runs the REAL writer
    end to end."""
    url, server, db = rig
    # The rig's stub returns a hit for every domain, which would 409 as
    # company_appeared -- this preview is for a genuinely new company.
    server.hubspot_client.find_companies_by_domain = lambda domain: []

    status, body = _post(url, "/commit", {
        "idempotency_key": "flow-key-1",
        "confirm": False,
        "contact": {"first_name": "Dana", "last_name": "Ops"},
        "company": {"new": {"name": "Acme Pest", "domain": "acmepest.com",
                            "state": "Texas"}},
    })

    assert status == 200
    plan = body["preview"]
    assert isinstance(plan, dict)  # never the old boolean
    assert "would" not in body
    assert plan["contact_props"]["firstname"] == "Dana"
    # F3: the preview names the company that would be created.
    assert plan["company_new"]["name"] == "Acme Pest"
    assert plan["company_new"]["state"] == "TX"
    assert isinstance(body["holds"], list)
    assert body["dry_run"] is True  # the rig's cfg is DRY_RUN
    # Previews write nothing -- no events rows.
    assert db.events == []


def test_commit_passthrough_success(rig, monkeypatch):
    url, server, db = rig
    monkeypatch.setattr(
        writer_mod, "commit",
        lambda db_, hubspot, rep, cfg, body: {"dry_run": True,
                                              "would": {"contact_props": {}}})
    status, body = _post(url, "/commit", {"confirm": True})
    assert status == 200
    assert body["dry_run"] is True
