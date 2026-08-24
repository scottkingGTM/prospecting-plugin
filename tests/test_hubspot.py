"""Unit tests for prospector.hubspot — mocked transport only, no network.

Every test drives the client through httpx.MockTransport, so the full retry
matrix (429 + Retry-After, 5xx exhaustion, 401 fail-fast) runs without a
single live HubSpot call. The token-hygiene tests are the ones that matter
most: the Authorization header value must never surface in an exception
message or a log line, no matter which failure path produced it.
"""

from __future__ import annotations

import json as jsonlib
import logging

import httpx
import pytest

from prospector.hubspot import (
    COMPANY_CONTACT_LIST_PROPERTIES,
    COMPANY_DETAIL_PROPERTIES,
    CONTACT_DETAIL_PROPERTIES,
    CONTACT_SEARCH_PROPERTIES,
    HubSpotClient,
    HubSpotError,
    MAX_ATTEMPTS,
    NAME_SEARCH_PROPERTIES,
)
from prospector.matching import norm_linkedin

TOKEN = "test-bearer-token-not-a-real-secret"


def make_client(handler, *, portal_id: str = ""):
    """Client wired to a MockTransport, with sleeps recorded not slept."""
    sleeps: list[float] = []
    client = HubSpotClient(
        TOKEN,
        transport=httpx.MockTransport(handler),
        portal_id=portal_id,
        sleep=sleeps.append,
    )
    return client, sleeps


def search_response(results: list[dict], total: int | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"total": total if total is not None else len(results),
                   "results": results})


def contact_record(record_id: str, **props) -> dict:
    return {"id": record_id, "properties": props}


# -- LinkedIn contact search --------------------------------------------------


def test_linkedin_search_sends_canonical_and_stored_variants():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = jsonlib.loads(request.content)
        return search_response([])

    client, _ = make_client(handler)
    client.find_contact_by_linkedin("linkedin.com/in/jane-doe")

    assert captured["path"] == "/crm/v3/objects/contacts/search"
    groups = captured["body"]["filterGroups"]
    # One variant per group so HubSpot ORs them; every filter is EQ on
    # hs_linkedin_url.
    for group in groups:
        (filt,) = group["filters"]
        assert filt["propertyName"] == "hs_linkedin_url"
        assert filt["operator"] == "EQ"
    values = {group["filters"][0]["value"] for group in groups}
    assert values == {
        "linkedin.com/in/jane-doe",
        "linkedin.com/in/jane-doe/",
        "https://www.linkedin.com/in/jane-doe",
        "https://www.linkedin.com/in/jane-doe/",
        "https://linkedin.com/in/jane-doe",
    }
    # HubSpot caps a search at 5 filterGroups -- the variant list must
    # never grow past it.
    assert len(groups) <= 5
    assert captured["body"]["properties"] == CONTACT_SEARCH_PROPERTIES


def test_linkedin_search_normalizes_input_before_building_variants():
    """A messy as-scraped URL collapses to the same five variants — the
    canonical form is norm_linkedin's, not a second normalizer's."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = jsonlib.loads(request.content)
        return search_response([])

    client, _ = make_client(handler)
    client.find_contact_by_linkedin("HTTPS://WWW.LinkedIn.com/in/Jane-Doe/?src=x")

    values = {g["filters"][0]["value"] for g in captured["body"]["filterGroups"]}
    canonical = norm_linkedin("HTTPS://WWW.LinkedIn.com/in/Jane-Doe/?src=x")
    assert canonical == "linkedin.com/in/jane-doe"
    assert canonical in values
    assert len(values) == 5


def test_linkedin_search_zero_results_returns_none():
    client, _ = make_client(lambda request: search_response([]))
    assert client.find_contact_by_linkedin("linkedin.com/in/nobody") is None


def test_linkedin_search_one_result_is_flattened():
    record = contact_record(
        "1001", firstname="Jane", lastname="Doe", email="jane@acme.com",
        hs_linkedin_url="https://www.linkedin.com/in/jane-doe")

    client, _ = make_client(lambda request: search_response([record]))
    found = client.find_contact_by_linkedin("linkedin.com/in/jane-doe")

    assert found == {
        "id": "1001",
        "firstname": "Jane",
        "lastname": "Doe",
        "email": "jane@acme.com",
        "hs_linkedin_url": "https://www.linkedin.com/in/jane-doe",
    }
    assert "_multiple_matches" not in found


def test_linkedin_search_multiple_results_flags_count():
    records = [contact_record("1001", firstname="Jane"),
               contact_record("2002", firstname="Jane")]

    client, _ = make_client(lambda request: search_response(records))
    found = client.find_contact_by_linkedin("linkedin.com/in/jane-doe")

    assert found["id"] == "1001"  # first match wins...
    assert found["_multiple_matches"] == 2  # ...but the dupe is surfaced


# -- name search (possible matches) --------------------------------------------


def test_name_search_body_is_one_anded_filter_group():
    """ONE filterGroup, firstname AND lastname CONTAINS_TOKEN -- both name
    parts must hit the same contact, and token matching (not EQ) tolerates
    middle names and credentials stored in the name fields."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = jsonlib.loads(request.content)
        return search_response([])

    client, _ = make_client(handler)
    client.find_contacts_by_name("billy", "smith")

    assert captured["path"] == "/crm/v3/objects/contacts/search"
    (group,) = captured["body"]["filterGroups"]  # exactly one group = AND
    assert group["filters"] == [
        {"propertyName": "firstname", "operator": "CONTAINS_TOKEN",
         "value": "billy"},
        {"propertyName": "lastname", "operator": "CONTAINS_TOKEN",
         "value": "smith"},
    ]
    assert captured["body"]["properties"] == NAME_SEARCH_PROPERTIES
    assert captured["body"]["limit"] == 10


def test_name_search_zero_results_returns_empty_list():
    client, _ = make_client(lambda request: search_response([]))
    assert client.find_contacts_by_name("nobody", "nowhere") == []


def test_name_search_two_results_are_flattened_in_order():
    records = [
        contact_record("7001", firstname="Billy", lastname="Smith",
                       email="billy@acme.com", jobtitle="VP Ops"),
        contact_record("7002", firstname="Bill", lastname="Smith",
                       email="bill@other.com"),
    ]
    client, _ = make_client(lambda request: search_response(records))
    found = client.find_contacts_by_name("bill", "smith")
    assert [c["id"] for c in found] == ["7001", "7002"]
    assert found[0]["jobtitle"] == "VP Ops"
    assert found[1]["email"] == "bill@other.com"


def test_name_search_blank_names_short_circuit():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made for a blank name")

    client, _ = make_client(handler)
    assert client.find_contacts_by_name("", "smith") == []
    assert client.find_contacts_by_name("billy", "  ") == []


# -- company searches -----------------------------------------------------------


def test_domain_search_returns_all_matches_in_order():
    records = [
        {"id": "11", "properties": {"name": "Acme HVAC", "domain": "acme.com"}},
        {"id": "22", "properties": {"name": "Acme HVAC (old)", "domain": "acme.com"}},
        {"id": "33", "properties": {"name": "Acme Corporate", "domain": "acme.com"}},
    ]

    client, _ = make_client(lambda request: search_response(records))
    found = client.find_companies_by_domain("acme.com")

    assert [c["id"] for c in found] == ["11", "22", "33"]
    assert found[0]["name"] == "Acme HVAC"


def test_domain_search_normalizes_to_registered_domain():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = jsonlib.loads(request.content)
        return search_response([])

    client, _ = make_client(handler)
    client.find_companies_by_domain("https://WWW.Acme.com/about")

    (group,) = captured["body"]["filterGroups"]
    (filt,) = group["filters"]
    assert filt == {"propertyName": "domain", "operator": "EQ",
                    "value": "acme.com"}


def test_domain_search_empty_domain_short_circuits():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made for an empty domain")

    client, _ = make_client(handler)
    assert client.find_companies_by_domain("") == []


def company_record(record_id: str, page: str | None) -> dict:
    props: dict = {"name": f"Company {record_id}"}
    if page is not None:
        props["linkedin_company_page"] = page
    return {"id": record_id, "properties": props}


def test_company_slug_search_uses_contains_token():
    captured: dict = {}
    record = company_record(
        "44", "https://www.linkedin.com/company/acme-hvac")

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = jsonlib.loads(request.content)
        return search_response([record])

    client, _ = make_client(handler)
    found = client.find_company_by_linkedin_slug("acme-hvac")

    assert captured["path"] == "/crm/v3/objects/companies/search"
    (group,) = captured["body"]["filterGroups"]
    (filt,) = group["filters"]
    assert filt == {"propertyName": "linkedin_company_page",
                    "operator": "CONTAINS_TOKEN", "value": "acme-hvac"}
    assert [c["id"] for c in found] == ["44"]


def test_company_slug_search_post_filters_to_exact_slug():
    """CONTAINS_TOKEN tokenizes on non-alphanumerics, so a search for
    'acme' also returns 'acme-corp' -- the exact-slug post-filter is the
    correctness gate that keeps only the true match."""
    records = [
        company_record("1", "https://www.linkedin.com/company/acme-corp"),
        company_record("2", "https://www.linkedin.com/company/acme"),
    ]
    client, _ = make_client(lambda request: search_response(records))
    found = client.find_company_by_linkedin_slug("acme")
    assert [c["id"] for c in found] == ["2"]


def test_company_slug_search_drops_records_without_company_page():
    """A record with no (or an empty) linkedin_company_page only matched
    via token noise -- it is dropped, never returned as a company match."""
    records = [
        company_record("3", None),
        company_record("4", ""),
    ]
    client, _ = make_client(lambda request: search_response(records))
    assert client.find_company_by_linkedin_slug("acme") == []


def test_company_slug_post_filter_is_case_and_trailing_slash_insensitive():
    records = [company_record(
        "5", "HTTPS://WWW.LinkedIn.com/company/Acme-HVAC/?trk=nav")]
    client, _ = make_client(lambda request: search_response(records))
    found = client.find_company_by_linkedin_slug("ACME-HVAC")
    assert [c["id"] for c in found] == ["5"]


# -- retry / backoff ---------------------------------------------------------------


def test_429_honors_retry_after_then_succeeds():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"},
                                  json={"message": "rate limited"})
        return search_response([contact_record("1001", firstname="Jane")])

    client, sleeps = make_client(handler)
    found = client.find_contact_by_linkedin("linkedin.com/in/jane-doe")

    assert found["id"] == "1001"
    assert len(calls) == 2
    assert sleeps == [3.0]  # Retry-After wins over exponential backoff


def test_retry_after_sleep_is_capped_at_30s():
    """A pathological Retry-After (an hour) must not park the request
    thread -- the honored sleep is capped at 30s."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3600"},
                                  json={"message": "rate limited"})
        return search_response([contact_record("1001", firstname="Jane")])

    client, sleeps = make_client(handler)
    found = client.find_contact_by_linkedin("linkedin.com/in/jane-doe")

    assert found["id"] == "1001"
    assert sleeps == [30.0]


def test_500_retries_max_attempts_then_raises():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"message": "boom"})

    client, sleeps = make_client(handler)
    with pytest.raises(HubSpotError) as excinfo:
        client.find_contact_by_linkedin("linkedin.com/in/jane-doe")

    assert len(calls) == MAX_ATTEMPTS
    assert len(sleeps) == MAX_ATTEMPTS - 1
    assert excinfo.value.status_code == 500
    assert "500" in str(excinfo.value)


def test_401_fails_fast_with_scopes_message():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"message": "unauthorized"})

    client, sleeps = make_client(handler)
    with pytest.raises(HubSpotError) as excinfo:
        client.find_contact_by_linkedin("linkedin.com/in/jane-doe")

    assert len(calls) == 1  # never retried
    assert sleeps == []
    assert excinfo.value.status_code == 401
    assert "check HUBSPOT_TOKEN scopes" in str(excinfo.value)


# -- token hygiene ----------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 500])
def test_token_never_appears_in_exception_messages(status, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        # Prove the header went out (auth works)...
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(status, json={"message": "nope"})

    client, _ = make_client(handler)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(HubSpotError) as excinfo:
            client.find_contact_by_linkedin("linkedin.com/in/jane-doe")

    # ...but it never comes back in the error or the logs.
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in repr(excinfo.value)
    assert TOKEN not in caplog.text


def test_token_never_appears_on_transport_failure(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client, _ = make_client(handler)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(HubSpotError) as excinfo:
            client.find_contact_by_linkedin("linkedin.com/in/jane-doe")

    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in caplog.text
    # `raise ... from None`: the httpx exception (which carries the request,
    # headers included) must not ride along as the cause.
    assert excinfo.value.__cause__ is None


# -- owners --------------------------------------------------------------------


def test_get_owner_returns_flattened_owner():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/crm/v3/owners/987"
        return httpx.Response(200, json={
            "id": "987", "firstName": "Nick", "lastName": "Rep",
            "email": "nick@example.com", "userId": 555})

    client, _ = make_client(handler)
    assert client.get_owner("987") == {
        "id": "987", "firstName": "Nick", "lastName": "Rep",
        "email": "nick@example.com"}


def test_get_owner_404_returns_none():
    client, _ = make_client(
        lambda request: httpx.Response(404, json={"message": "not found"}))
    assert client.get_owner("999") is None


def test_get_owner_other_errors_still_raise():
    client, _ = make_client(
        lambda request: httpx.Response(400, json={"message": "bad id"}))
    with pytest.raises(HubSpotError) as excinfo:
        client.get_owner("not-an-id")
    assert excinfo.value.status_code == 400


# -- record URL helpers ------------------------------------------------------------


def test_record_urls_with_portal_id():
    client, _ = make_client(lambda request: search_response([]),
                            portal_id="1234567")
    assert (client.contact_hubspot_url("1001")
            == "https://app.hubspot.com/contacts/1234567/record/0-1/1001")
    assert (client.company_hubspot_url("22")
            == "https://app.hubspot.com/contacts/1234567/record/0-2/22")


def test_record_urls_without_portal_id_return_none():
    client, _ = make_client(lambda request: search_response([]))
    assert client.contact_hubspot_url("1001") is None
    assert client.company_hubspot_url("22") is None


# -- record detail reads (the /record endpoint) --------------------------------


def test_get_company_flattens_and_requests_detail_properties():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={
            "id": "9001",
            "properties": {"name": "Acme Pest Control",
                           "domain": "acmepest.com",
                           "numberofemployees": "250",
                           "createdate": "2026-08-01T00:00:00Z"}})

    client, _ = make_client(handler)
    company = client.get_company("9001")

    assert captured["method"] == "GET"
    assert captured["path"] == "/crm/v3/objects/companies/9001"
    requested = captured["params"]["properties"].split(",")
    assert requested == COMPANY_DETAIL_PROPERTIES
    assert company == {"id": "9001", "name": "Acme Pest Control",
                       "domain": "acmepest.com", "numberofemployees": "250",
                       "createdate": "2026-08-01T00:00:00Z"}


def test_get_company_404_returns_none():
    client, _ = make_client(
        lambda request: httpx.Response(404, json={"status": "error"}))
    assert client.get_company("999") is None


def test_get_company_other_errors_still_raise():
    client, _ = make_client(
        lambda request: httpx.Response(400, json={"message": "bad id"}))
    with pytest.raises(HubSpotError) as excinfo:
        client.get_company("not-an-id")
    assert excinfo.value.status_code == 400


def test_get_contact_detail_flattens_and_requests_detail_properties():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={
            "id": "501",
            "properties": {"firstname": "Jane", "lastname": "Doe",
                           "associatedcompanyid": "9001"}})

    client, _ = make_client(handler)
    contact = client.get_contact_detail("501")

    assert captured["path"] == "/crm/v3/objects/contacts/501"
    requested = captured["params"]["properties"].split(",")
    assert requested == CONTACT_DETAIL_PROPERTIES
    assert contact == {"id": "501", "firstname": "Jane", "lastname": "Doe",
                       "associatedcompanyid": "9001"}


def test_get_contact_detail_404_returns_none():
    client, _ = make_client(
        lambda request: httpx.Response(404, json={"status": "error"}))
    assert client.get_contact_detail("999") is None


def test_get_company_contacts_association_page_then_batch_read():
    """v4 association list first, then ONE contacts batch read carrying the
    listed ids and the roster property set."""
    captured: dict = {"paths": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["paths"].append(request.url.path)
        if request.url.path.endswith("/associations/contacts"):
            captured["assoc_params"] = dict(request.url.params)
            return httpx.Response(200, json={"results": [
                {"toObjectId": 501}, {"toObjectId": 502}]})
        assert request.url.path == "/crm/v3/objects/contacts/batch/read"
        captured["batch_body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"results": [
            {"id": "501", "properties": {"firstname": "Jane",
                                         "jobtitle": "VP Ops"}},
            {"id": "502", "properties": {"firstname": "Bob"}},
        ]})

    client, _ = make_client(handler)
    contacts = client.get_company_contacts("9001")

    assert captured["paths"][0] == (
        "/crm/v4/objects/companies/9001/associations/contacts")
    assert captured["assoc_params"]["limit"] == "10"
    assert captured["batch_body"]["inputs"] == [{"id": "501"}, {"id": "502"}]
    assert (captured["batch_body"]["properties"]
            == COMPANY_CONTACT_LIST_PROPERTIES)
    assert [c["id"] for c in contacts] == ["501", "502"]
    assert contacts[0]["jobtitle"] == "VP Ops"


def test_get_company_contacts_caps_and_dedupes_association_rows():
    """The batch read never carries more than `limit` ids, and duplicate
    association rows (a contact associated twice via labels) collapse."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/associations/contacts"):
            rows = [{"toObjectId": 501}, {"toObjectId": 501},
                    {"toObjectId": 502}, {"toObjectId": 503}]
            return httpx.Response(200, json={"results": rows})
        captured["batch_body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"results": [
            {"id": "501", "properties": {}}, {"id": "502", "properties": {}}]})

    client, _ = make_client(handler)
    client.get_company_contacts("9001", limit=2)
    assert captured["batch_body"]["inputs"] == [{"id": "501"}, {"id": "502"}]


def test_get_company_contacts_association_404_is_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/associations/contacts")
        return httpx.Response(404, json={"status": "error"})

    client, _ = make_client(handler)
    assert client.get_company_contacts("999") == []


def test_get_company_contacts_no_associations_skips_batch_read():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    client, _ = make_client(handler)
    assert client.get_company_contacts("9001") == []
    assert paths == ["/crm/v4/objects/companies/9001/associations/contacts"]


# -- write-pipeline additions -------------------------------------------------
#
# These methods exist only for prospector.writer's guarded pipeline. The
# tests below pin the wire shapes writer.py depends on: the 5-filterGroup
# email search, echo-matching (op_clay lesson: never trust result order),
# the merged-away-id semantics of batch read, and the closed-lost heuristic.


def test_find_contacts_by_emails_fills_the_filtergroup_cap_exactly():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v3/objects/contacts/search"
        captured.append(jsonlib.loads(request.content))
        return search_response([])

    client, _ = make_client(handler)
    emails = [f"user{i}@acme.com" for i in range(6)]
    client.find_contacts_by_emails(emails)

    # 6 emails -> chunks of 4 + 2 (the 5-filterGroup cap binds at 1 IN
    # group + 4 CONTAINS_TOKEN groups).
    assert len(captured) == 2
    first = captured[0]["filterGroups"]
    assert len(first) == 5
    (in_filter,) = first[0]["filters"]
    assert in_filter["propertyName"] == "email"
    assert in_filter["operator"] == "IN"
    assert in_filter["values"] == emails[:4]
    for group, email in zip(first[1:], emails[:4]):
        (filt,) = group["filters"]
        assert filt["propertyName"] == "hs_additional_emails"
        assert filt["operator"] == "CONTAINS_TOKEN"
        assert filt["value"] == email
    assert len(captured[1]["filterGroups"]) == 3  # IN + 2 token groups


def test_find_contacts_by_emails_attributes_matched_email():
    records = [
        contact_record("1", email="jane@acme.com"),
        contact_record("2", email="j.doe@other.com",
                       hs_additional_emails="old@x.com;bob@acme.com"),
    ]
    client, _ = make_client(lambda request: search_response(records))
    found = client.find_contacts_by_emails(["Jane@Acme.com", "bob@acme.com"])

    by_id = {c["id"]: c for c in found}
    # Primary-email hit attributes to the primary...
    assert by_id["1"]["matched_email"] == "jane@acme.com"
    # ...a secondary-address hit attributes through hs_additional_emails.
    assert by_id["2"]["matched_email"] == "bob@acme.com"


def test_find_contacts_by_emails_empty_input_sends_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    client, _ = make_client(handler)
    assert client.find_contacts_by_emails([]) == []
    assert client.find_contacts_by_emails(["", "   "]) == []


def test_create_contact_echo_matches_never_position():
    """op_clay lesson: even a rogue multi-result response must be resolved
    by the echoed email, not by taking results[0]."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v3/objects/contacts/batch/create"
        body = jsonlib.loads(request.content)
        assert len(body["inputs"]) == 1
        return httpx.Response(201, json={"results": [
            {"id": "9", "properties": {"email": "other@x.com"}},
            {"id": "10", "properties": {"email": "jane@acme.com",
                                        "firstname": "Jane"}},
        ]})

    client, _ = make_client(handler)
    created = client.create_contact({"email": "Jane@acme.com",
                                     "firstname": "Jane"})
    assert created["id"] == "10"
    assert created["firstname"] == "Jane"


def test_create_contact_inline_association_when_company_id_given():
    """With a company_id, the create payload itself carries the
    HUBSPOT_DEFINED contact->company primary association (279) -- the
    contact is born associated, leaving the portal's company-auto-create
    setting no gap to mint a junk company."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = jsonlib.loads(request.content)
        return httpx.Response(201, json={"results": [
            {"id": "10", "properties": {"email": "jane@acme.com"}}]})

    client, _ = make_client(handler)
    created = client.create_contact({"email": "jane@acme.com"},
                                    company_id="555")

    assert created["id"] == "10"
    (one,) = captured["body"]["inputs"]
    assert one["associations"] == [{
        "to": {"id": "555"},
        "types": [{"associationCategory": "HUBSPOT_DEFINED",
                   "associationTypeId": 279}],
    }]


def test_create_contact_without_company_id_carries_no_associations():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = jsonlib.loads(request.content)
        return httpx.Response(201, json={"results": [
            {"id": "10", "properties": {"email": "jane@acme.com"}}]})

    client, _ = make_client(handler)
    client.create_contact({"email": "jane@acme.com"})
    (one,) = captured["body"]["inputs"]
    assert "associations" not in one


def test_create_contact_without_echo_match_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"results": [
            {"id": "9", "properties": {"email": "other@x.com"}}]})

    client, _ = make_client(handler)
    with pytest.raises(HubSpotError):
        client.create_contact({"email": "jane@acme.com"})


def test_create_company_echo_matches_on_domain():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v3/objects/companies/batch/create"
        return httpx.Response(201, json={"results": [
            {"id": "70", "properties": {"domain": "someone-else.com"}},
            {"id": "77", "properties": {"domain": "frosthvac.com",
                                        "name": "Frost HVAC"}},
        ]})

    client, _ = make_client(handler)
    created = client.create_company({"name": "Frost HVAC",
                                     "domain": "frosthvac.com"})
    assert created["id"] == "77"


def test_update_contact_patches_the_record():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={
            "id": "888",
            "properties": {"hs_linkedin_url": "linkedin.com/in/x"}})

    client, _ = make_client(handler)
    updated = client.update_contact("888",
                                    {"hs_linkedin_url": "linkedin.com/in/x"})

    assert captured["method"] == "PATCH"
    assert captured["path"] == "/crm/v3/objects/contacts/888"
    assert captured["body"] == {
        "properties": {"hs_linkedin_url": "linkedin.com/in/x"}}
    assert updated == {"id": "888", "hs_linkedin_url": "linkedin.com/in/x"}


def test_update_company_patches_the_record():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={
            "id": "555",
            "properties": {"hs_ideal_customer_profile": "tier_2"}})

    client, _ = make_client(handler)
    client.update_company("555", {"hs_ideal_customer_profile": "tier_2"})
    assert captured["method"] == "PATCH"
    assert captured["path"] == "/crm/v3/objects/companies/555"


def test_associate_contact_company_uses_v4_default_endpoint():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={})

    client, _ = make_client(handler)
    assert client.associate_contact_company("888", "555") is None
    assert captured["method"] == "PUT"
    assert captured["path"] == (
        "/crm/v4/objects/contacts/888/associations/default/companies/555")


def test_companies_batch_read_absent_id_means_stale_or_merged():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v3/objects/companies/batch/read"
        body = jsonlib.loads(request.content)
        assert body["inputs"] == [{"id": "555"}, {"id": "666"}]
        # HubSpot silently omits ids that were deleted or merged away.
        return httpx.Response(200, json={"results": [
            {"id": "555", "properties": {"name": "Acme"}}]})

    client, _ = make_client(handler)
    found = client.companies_batch_read(["555", "666"])
    assert set(found) == {"555"}
    assert found["555"]["name"] == "Acme"


def test_create_note_plain_text_body_and_both_associations():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = jsonlib.loads(request.content)
        return httpx.Response(201, json={"id": "note-77"})

    client, _ = make_client(handler)
    note_id = client.create_note("plain text body\nline two", "888", "555")

    assert note_id == "note-77"
    assert captured["path"] == "/crm/v3/objects/notes"
    props = captured["body"]["properties"]
    assert props["hs_note_body"] == "plain text body\nline two"
    assert isinstance(props["hs_timestamp"], int)
    pairs = [(a["to"]["id"], a["types"][0]["associationTypeId"])
             for a in captured["body"]["associations"]]
    assert pairs == [("888", 202), ("555", 190)]


def test_create_note_contact_only_when_no_company():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = jsonlib.loads(request.content)
        return httpx.Response(201, json={"id": "note-78"})

    client, _ = make_client(handler)
    client.create_note("body", "888")
    assert len(captured["body"]["associations"]) == 1


def test_get_contact_404_returns_none():
    client, _ = make_client(
        lambda request: httpx.Response(404, json={"status": "error"}))
    assert client.get_contact("999") is None


def test_get_contact_flattens_and_requests_properties():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={
            "id": "888", "properties": {"hs_linkedin_url": ""}})

    client, _ = make_client(handler)
    contact = client.get_contact("888")
    assert contact == {"id": "888", "hs_linkedin_url": ""}
    assert "hs_linkedin_url" in captured["params"]["properties"]
