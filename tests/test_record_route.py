"""Integration tests for POST /record — the read-only record-detail
endpoint. Real server on an ephemeral port, stub Database, stub HubSpot
client injected via server.hubspot_client. No live calls.

Response contract pinned here:

  type=company -> {record, owner_name, contacts, hubspot_url}
  type=contact -> {record, owner_name, company (mini-card or null),
                   hubspot_url, company_hubspot_url}
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from prospector.auth import RepRegistry, hash_token
from prospector.server import build_server

TOKEN = "r" * 40

COMPANY_DETAIL = {
    "id": "9001",
    "name": "Acme Pest Control",
    "domain": "acmepest.com",
    "industry": "Pest Control",
    "numberofemployees": "250",
    "city": "Austin",
    "state": "TX",
    "lifecyclestage": "lead",
    "hs_ideal_customer_profile": "tier_2",
    "hs_is_target_account": "true",
    "hubspot_owner_id": "42",
    "createdate": "2024-02-01T00:00:00Z",
    "hs_lastmodifieddate": "2026-08-10T00:00:00Z",
    "description": "Pest control across Texas",
    "phone": "+1 512 555 0100",
    "website": "https://acmepest.com",
}

CONTACT_DETAIL = {
    "id": "501",
    "firstname": "Jane",
    "lastname": "Doe",
    "jobtitle": "VP of Operations",
    "email": "jane@acmepest.com",
    "hs_additional_emails": None,
    "phone": "+1 512 555 0101",
    "hs_linkedin_url": "linkedin.com/in/jane-doe",
    "lifecyclestage": "lead",
    "hubspot_owner_id": "42",
    "associatedcompanyid": "9001",
    "createdate": "2025-01-01T00:00:00Z",
    "lastmodifieddate": "2026-08-01T00:00:00Z",
    "notes_last_contacted": "2026-07-15T00:00:00Z",
    "num_associated_deals": "1",
}

COMPANY_CONTACTS = [
    {"id": "501", "firstname": "Jane", "lastname": "Doe",
     "jobtitle": "VP of Operations", "email": "jane@acmepest.com",
     "hs_linkedin_url": "linkedin.com/in/jane-doe", "lifecyclestage": "lead"},
    {"id": "502", "firstname": "Bob", "lastname": "Ops",
     "jobtitle": "Call Center Manager", "email": "bob@acmepest.com",
     "hs_linkedin_url": None, "lifecyclestage": "subscriber"},
]

ACCOUNT_ROW = {
    "hs_company_id": "9001",
    "name": "Acme Pest Control",
    "domain": "acmepest.com",
    "icp_tier": "Tier 2",
    "is_target_account": True,
    "lifecycle_stage": "lead",
    "owner_id": "42",
    "abm_stage": "S1",
    "employee_count": 250,
    "num_open_deals": 0,
    "total_deal_value": 0,
    "won_deals": 0,
    "lost_deals": 0,
    "last_deal_close_date": None,
    "is_current_client": False,
    "closed_lost_phase": None,
    "closed_lost_cooldown_days": None,
    "trend_cooling_off": False,
    "buying_committee_coverage": 40,
    "buying_committee_tiers": None,
    "coverage_components": {"ops": 1},
    "intl_has_intl_employees": False,
    "intl_overseas_confidence": None,
    "num_contacts": 3,
    "engaged_contacts_count": 1,
    "last_touch_at": "2026-08-10T00:00:00+00:00",
    "refreshed_at": "2026-08-18T07:00:00+00:00",
}


class StubDb:
    """Just enough Database for RepRegistry + the account view + events."""

    def __init__(self, account_rows=None) -> None:
        self.account_rows = account_rows if account_rows is not None else []
        self.events: list[tuple] = []

    def query(self, sql: str, params=None) -> list[dict]:
        if "token_hash" in sql or "daily_credit_cap" in sql:
            return [{
                "id": 1, "email": "rep@example.com", "display_name": "Rep",
                "hubspot_owner_id": "42", "token_hash": hash_token(TOKEN),
                "daily_credit_cap": 50, "daily_promote_cap": 25,
                "daily_t1_cap": 3, "daily_research_cap": 3,
            }]
        if "v_account_360_mcp" in sql:
            return [dict(r) for r in self.account_rows]
        if "prospector.reps" in sql:
            # recognize._owner_display_name's display-name lookup.
            return [{"display_name": "Nick AE"}]
        return []

    def execute(self, sql: str, params=None) -> int:
        if "INSERT INTO prospector.events" in sql:
            self.events.append(params)
            return 1
        return 0


class StubHubSpot:
    """Stub for exactly the client surface /record consumes."""

    def __init__(self) -> None:
        self.companies = {"9001": dict(COMPANY_DETAIL)}
        self.contacts = {"501": dict(CONTACT_DETAIL)}
        self.company_contacts = {"9001": [dict(c) for c in COMPANY_CONTACTS]}
        self.calls: list[tuple] = []

    def get_company(self, company_id):
        self.calls.append(("get_company", company_id))
        company = self.companies.get(str(company_id))
        return dict(company) if company else None

    def get_contact_detail(self, contact_id):
        self.calls.append(("get_contact_detail", contact_id))
        contact = self.contacts.get(str(contact_id))
        return dict(contact) if contact else None

    def get_company_contacts(self, company_id, limit=10):
        self.calls.append(("get_company_contacts", company_id, limit))
        return [dict(c) for c in
                self.company_contacts.get(str(company_id), [])][:limit]

    def get_owner(self, owner_id):
        self.calls.append(("get_owner", owner_id))
        if str(owner_id) == "42":
            return {"id": "42", "firstName": "Nick", "lastName": "AE",
                    "email": "nick@example.com"}
        return None

    def contact_hubspot_url(self, contact_id):
        return f"https://app.hubspot.com/contacts/777/record/0-1/{contact_id}"

    def company_hubspot_url(self, company_id):
        return f"https://app.hubspot.com/contacts/777/record/0-2/{company_id}"


class _Cfg:
    extension_origin = ""
    dry_run = True
    hubspot_token = ""       # deliberately empty: injection must win
    hubspot_portal_id = ""
    host = "127.0.0.1"
    port = 0


def make_server(account_rows=None):
    db = StubDb(account_rows=account_rows)
    registry = RepRegistry(db)
    server = build_server(_Cfg(), db, registry)
    server.hubspot_client = StubHubSpot()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return f"http://{host}:{port}", server, db


@pytest.fixture()
def server_url():
    url, server, db = make_server(account_rows=[dict(ACCOUNT_ROW)])
    yield url, server, db
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
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


# -- validation -----------------------------------------------------------------


def test_record_requires_auth(server_url):
    url, server, db = server_url
    status, body = _post(url, "/record",
                         {"type": "company", "id": "9001"}, token=None)
    assert status == 401


@pytest.mark.parametrize("bad_body", [
    {},                                        # both missing
    {"type": "deal", "id": "9001"},            # unknown type
    {"type": "Company", "id": "9001"},         # case matters
    {"type": "company"},                       # id missing
    {"type": "company", "id": 9001},           # id must be a string
    {"type": "company", "id": ""},             # empty
    {"type": "company", "id": "12a4"},         # non-digit
    {"type": "company", "id": "9001; DROP"},   # junk
    {"type": "company", "id": "9" * 21},       # too long
    {"type": "company", "id": "¹²"},  # unicode superscript digits
    {"type": "contact", "id": "../501"},       # path traversal shape
])
def test_record_rejects_invalid_input(server_url, bad_body):
    url, server, db = server_url
    status, body = _post(url, "/record", bad_body)
    assert status == 400
    assert body["error"] == "input_invalid"
    # Nothing hit HubSpot and nothing was logged.
    assert server.hubspot_client.calls == []
    assert db.events == []


def test_record_503_when_hubspot_unconfigured(server_url):
    url, server, db = server_url
    server.hubspot_client = None  # remove injection; cfg token is empty
    status, body = _post(url, "/record", {"type": "company", "id": "9001"})
    assert status == 503
    assert body["error"] == "hubspot_not_configured"


def test_record_unknown_company_is_404(server_url):
    url, server, db = server_url
    status, body = _post(url, "/record", {"type": "company", "id": "424242"})
    assert status == 404
    assert body["error"] == "record_not_found"
    assert db.events == []  # no record, no record_view event


def test_record_unknown_contact_is_404(server_url):
    url, server, db = server_url
    status, body = _post(url, "/record", {"type": "contact", "id": "424242"})
    assert status == 404
    assert body["error"] == "record_not_found"


# -- company --------------------------------------------------------------------


def test_record_company_happy_path_shape(server_url):
    url, server, db = server_url
    status, body = _post(url, "/record", {"type": "company", "id": "9001"})
    assert status == 200
    assert set(body) == {"record", "owner_name", "contacts", "hubspot_url"}
    record = body["record"]
    assert record["id"] == "9001"
    assert record["name"] == "Acme Pest Control"
    assert record["numberofemployees"] == "250"
    assert record["city"] == "Austin"
    assert body["owner_name"] == "Nick AE"
    assert body["hubspot_url"].endswith("/record/0-2/9001")
    # Contacts roster: flattened one-line rows, in association order.
    assert [c["id"] for c in body["contacts"]] == ["501", "502"]
    assert body["contacts"][0]["jobtitle"] == "VP of Operations"
    # No account context in the payload any more.
    assert "account" not in body


def test_record_company_without_contacts_gets_empty_list(server_url):
    url, server, db = server_url
    server.hubspot_client.company_contacts = {}  # association miss -> []
    status, body = _post(url, "/record", {"type": "company", "id": "9001"})
    assert status == 200
    assert body["contacts"] == []


def test_record_company_logs_record_view_event(server_url):
    url, server, db = server_url
    _post(url, "/record", {"type": "company", "id": "9001"})
    assert len(db.events) == 1
    rep_id, action, status_val, target, cost, dry_run = db.events[0]
    assert action == "record_view"
    assert status_val == "done"
    assert json.loads(target) == {"type": "company", "id": "9001"}


def test_record_event_insert_failure_never_fails_the_read(server_url):
    url, server, db = server_url

    def boom(sql, params=None):
        raise RuntimeError("db down")

    db.execute = boom
    status, body = _post(url, "/record", {"type": "company", "id": "9001"})
    assert status == 200
    assert body["record"]["id"] == "9001"


# -- contact --------------------------------------------------------------------


def test_record_contact_happy_path_with_company_mini_card(server_url):
    url, server, db = server_url
    status, body = _post(url, "/record", {"type": "contact", "id": "501"})
    assert status == 200
    assert set(body) == {"record", "owner_name", "company", "hubspot_url",
                         "company_hubspot_url"}
    record = body["record"]
    assert record["id"] == "501"
    assert record["email"] == "jane@acmepest.com"
    assert record["num_associated_deals"] == "1"
    assert body["owner_name"] == "Nick AE"
    assert body["hubspot_url"].endswith("/record/0-1/501")
    # Mini-card only: name/domain/tier/target, nothing else.
    assert body["company"] == {
        "id": "9001",
        "name": "Acme Pest Control",
        "domain": "acmepest.com",
        "tier": "tier_2",
        "is_target_account": "true",
    }
    assert body["company_hubspot_url"].endswith("/record/0-2/9001")
    # Event logged with the contact identity.
    assert json.loads(db.events[0][3]) == {"type": "contact", "id": "501"}


def test_record_contact_without_company_association(server_url):
    url, server, db = server_url
    server.hubspot_client.contacts["501"]["associatedcompanyid"] = None
    status, body = _post(url, "/record", {"type": "contact", "id": "501"})
    assert status == 200
    assert body["company"] is None
    assert body["company_hubspot_url"] is None
    # No pointless company fetch.
    assert ("get_company", "9001") not in server.hubspot_client.calls


def test_record_contact_with_merged_away_company_id(server_url):
    """associatedcompanyid points at a company that 404s (merged away):
    the contact still renders, just without the mini-card."""
    url, server, db = server_url
    server.hubspot_client.contacts["501"]["associatedcompanyid"] = "31337"
    status, body = _post(url, "/record", {"type": "contact", "id": "501"})
    assert status == 200
    assert body["record"]["id"] == "501"
    assert body["company"] is None
    assert body["company_hubspot_url"] is None


def test_record_owner_falls_back_to_raw_id_when_owner_lookup_misses(server_url):
    url, server, db = server_url
    server.hubspot_client.companies["9001"]["hubspot_owner_id"] = "999"
    status, body = _post(url, "/record", {"type": "company", "id": "9001"})
    assert status == 200
    assert body["owner_name"] == "999"  # never hidden, never invented
