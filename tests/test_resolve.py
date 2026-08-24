"""Unit tests for prospector.resolve — the pre-commit duplicate check.

No live network: the HubSpot client is a call-counting stub built against
the pinned interface (find_contact_by_linkedin, find_contacts_by_emails,
find_contacts_by_name, find_companies_by_domain,
find_company_by_linkedin_slug, search_companies_fuzzy). Fixtures are named
for the case each rule defends against: a nickname match, the
m.brown@/mbrown@ and domain-spelling catches, a company's twin records, a
cross-state name collision, and a careers-subdomain survivor.
"""

from __future__ import annotations

from collections import Counter

import pytest

from prospector.resolve import resolve_company, resolve_contact


# ---------------------------------------------------------------------------
# Stub client
# ---------------------------------------------------------------------------


class StubHubSpot:
    """Call-counting stub for the pinned dedupe interface."""

    def __init__(
        self,
        by_linkedin=None,
        by_emails=None,
        by_name=None,
        by_domain=None,
        by_slug=None,
        fuzzy=None,
    ):
        self.by_linkedin = by_linkedin
        self.by_emails = by_emails or []
        self.by_name = by_name or []
        self.by_domain = by_domain or []
        self.by_slug = by_slug or []
        self.fuzzy = fuzzy or []
        self.calls: Counter = Counter()

    def find_contact_by_linkedin(self, norm_url):
        self.calls["find_contact_by_linkedin"] += 1
        self.last_linkedin_lookup = norm_url
        return dict(self.by_linkedin) if self.by_linkedin else None

    def find_contacts_by_emails(self, emails):
        self.calls["find_contacts_by_emails"] += 1
        self.last_email_lookup = list(emails)
        return [dict(c) for c in self.by_emails]

    def find_contacts_by_name(self, first, last):
        self.calls["find_contacts_by_name"] += 1
        self.last_name_lookup = (first, last)
        return [dict(c) for c in self.by_name]

    def find_companies_by_domain(self, domain):
        self.calls["find_companies_by_domain"] += 1
        self.last_domain_lookup = domain
        return [dict(c) for c in self.by_domain]

    def find_company_by_linkedin_slug(self, slug):
        self.calls["find_company_by_linkedin_slug"] += 1
        self.last_slug_lookup = slug
        return [dict(c) for c in self.by_slug]

    def search_companies_fuzzy(self, name_token):
        self.calls["search_companies_fuzzy"] += 1
        self.last_fuzzy_token = name_token
        return [dict(c) for c in self.fuzzy]


class ExplodingHubSpot(StubHubSpot):
    """Every touched method raises — proves resolve swallows nothing."""

    def find_contact_by_linkedin(self, norm_url):
        raise RuntimeError("hubspot 500")

    def find_companies_by_domain(self, domain):
        raise RuntimeError("hubspot 500")


# ---------------------------------------------------------------------------
# Contact fixtures
# ---------------------------------------------------------------------------

BILLY = {
    "id": "101",
    "firstname": "Billy",
    "lastname": "Smith",
    "email": "billy@acmepest.com",
    "jobtitle": "COO",
    "hs_linkedin_url": "linkedin.com/in/billy-smith",
    "hubspot_owner_id": "9",
    "company": "Acme Pest Control LLC",
}

MORGAN = {
    "id": "202",
    "firstname": "Morgan",
    "lastname": "Brown",
    "email": "m.brown@acmepest.com",
    "jobtitle": "VP Customer Service",
    "hs_linkedin_url": "",
    "hubspot_owner_id": "9",
    "company": "",
}

SAM = {
    "id": "303",
    "firstname": "Sam",
    "lastname": "Rivera",
    "email": "sam@apex.solar",
    "jobtitle": "Director of Ops",
    "hs_linkedin_url": "",
    "hubspot_owner_id": "12",
    "company": "",
}


# ---------------------------------------------------------------------------
# resolve_contact
# ---------------------------------------------------------------------------


def test_linkedin_and_email_same_contact_dedupes_and_skips_name_chain():
    """LinkedIn exact + email exact agreeing on ONE contact: one match,
    matched_on='linkedin' (the strongest chain wins the dedupe), and the
    name+company search is skipped — the documented exception."""
    hub = StubHubSpot(
        by_linkedin=BILLY,
        by_emails=[{**BILLY, "matched_email": "billy@acmepest.com"}],
        by_name=[BILLY],  # would only re-find him; must never be asked
    )
    result = resolve_contact(
        hub,
        linkedin_url="https://www.linkedin.com/in/billy-smith/",
        email="billy@acmepest.com",
        first_name="Billy",
        last_name="Smith",
        company_name="Acme Pest Control",
    )
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["hs_contact_id"] == "101"
    assert match["matched_on"] == "linkedin"
    assert match["confidence"] == "exact"
    assert hub.calls["find_contact_by_linkedin"] == 1
    assert hub.calls["find_contacts_by_emails"] == 1
    assert hub.calls["find_contacts_by_name"] == 0
    # The URL was normalized before the lookup.
    assert hub.last_linkedin_lookup == "linkedin.com/in/billy-smith"


def test_linkedin_match_without_email_still_runs_name_chain():
    """The skip is ONLY for linkedin+email agreement: with no email input
    the rep must still see the name+company near-matches before 'Create
    new'."""
    other = {**MORGAN, "id": "999", "firstname": "Billy", "lastname": "Smith",
             "email": "b.smith@acmepest.com", "company": "Acme Pest Control"}
    hub = StubHubSpot(by_linkedin=BILLY, by_name=[other])
    result = resolve_contact(
        hub,
        linkedin_url="linkedin.com/in/billy-smith",
        first_name="William",
        last_name="Smith",
        company_name="Acme Pest Control",
    )
    assert hub.calls["find_contacts_by_name"] == 1
    matched_on = {m["hs_contact_id"]: m["matched_on"] for m in result["matches"]}
    assert matched_on == {"101": "linkedin", "999": "name_company"}


def test_email_match_via_additional_emails():
    """An email-repair pattern: one person, two addresses. The client
    matched on hs_additional_emails (matched_email differs from the primary
    email) — resolve reports it as an exact email match."""
    hub = StubHubSpot(
        by_emails=[{
            "id": "404",
            "firstname": "Will",
            "lastname": "Smith",
            "email": "will.smith@acmepest.com",  # primary
            "jobtitle": "GM",
            "hs_linkedin_url": "",
            "hubspot_owner_id": "9",
            "matched_email": "wsmith@acmepest.com",  # the additional one
        }]
    )
    result = resolve_contact(hub, email="WSmith@AcmePest.com")
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["matched_on"] == "email"
    assert match["confidence"] == "exact"
    assert match["email"] == "will.smith@acmepest.com"
    # Email was normalized (stripped/lowercased) before the search.
    assert hub.last_email_lookup == ["wsmith@acmepest.com"]


def test_billy_william_nickname_possible_match():
    """Billy Smith stored, William Smith enriched, same normalized company:
    names_agree bridges the nickname group, company matches on
    norm_company_name equality (suffix-stripped) — surfaced as a possible
    match, never an exact one."""
    hub = StubHubSpot(by_name=[BILLY])
    result = resolve_contact(
        hub,
        first_name="William",
        last_name="Smith",
        company_name="Acme Pest Control",
    )
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["hs_contact_id"] == "101"
    assert match["matched_on"] == "name_company"
    assert match["confidence"] == "possible"
    assert match["name"] == "Billy Smith"


def test_mbrown_different_local_same_domain_surfaced():
    """m.brown@ stored vs mbrown@ enriched. The exact-email chain
    misses (different locals), but name+company catches it — the shared
    email domain is the company evidence."""
    hub = StubHubSpot(by_emails=[], by_name=[MORGAN])
    result = resolve_contact(
        hub,
        email="mbrown@acmepest.com",
        first_name="Morgan",
        last_name="Brown",
    )
    assert hub.calls["find_contacts_by_emails"] == 1
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["hs_contact_id"] == "202"
    assert match["matched_on"] == "name_company"
    assert match["confidence"] == "possible"


def test_domain_spells_company_name():
    """sam@apex.solar stored vs sam@apexsolar.com enriched — two
    spellings of one company's domain. Email chain misses, and even the
    domains differ; the candidate's domain squeezing to the company name is
    the evidence that lists it."""
    hub = StubHubSpot(by_emails=[], by_name=[SAM])
    result = resolve_contact(
        hub,
        email="sam@apexsolar.com",
        first_name="Sam",
        last_name="Rivera",
        company_name="Apex Solar",
    )
    assert len(result["matches"]) == 1
    assert result["matches"][0]["hs_contact_id"] == "303"
    assert result["matches"][0]["matched_on"] == "name_company"


def test_name_candidate_without_company_evidence_dropped():
    """A namesake at a different company is NOT listed: names_agree passes
    but no company evidence does — the franchise/namesake protection."""
    stranger = {**BILLY, "id": "777", "email": "billy@otherco.com",
                "company": "Other Services Inc"}
    hub = StubHubSpot(by_name=[stranger])
    result = resolve_contact(
        hub,
        first_name="Billy",
        last_name="Smith",
        company_name="Acme Pest Control",
    )
    assert result["matches"] == []


def test_resolve_contact_no_inputs_no_calls():
    hub = StubHubSpot(by_linkedin=BILLY, by_emails=[BILLY], by_name=[BILLY])
    result = resolve_contact(hub)
    assert result == {"matches": []}
    assert sum(hub.calls.values()) == 0


def test_resolve_contact_client_errors_propagate():
    """Swallow nothing: the routes map errors, and a swallowed failure
    would present a duplicate as net-new."""
    with pytest.raises(RuntimeError, match="hubspot 500"):
        resolve_contact(ExplodingHubSpot(), linkedin_url="linkedin.com/in/x-y")


# ---------------------------------------------------------------------------
# Company fixtures
# ---------------------------------------------------------------------------

ACME_PLUMBING_TIERLESS = {
    "id": "51",
    "name": "Acme Plumbing",
    "domain": "acmeplumbing.com",
    "state": "TX",
    "hs_ideal_customer_profile": "",
    "hs_is_target_account": "false",
    "linkedin_company_page": "",
}

ACME_PLUMBING_TIERED = {
    "id": "52",
    "name": "Acme Plumbing",
    "domain": "acmeplumbing.com",
    "state": "TX",
    "hs_ideal_customer_profile": "tier_2",
    "hs_is_target_account": "true",
    "linkedin_company_page": "https://www.linkedin.com/company/acme-plumbing/",
}


# ---------------------------------------------------------------------------
# resolve_company
# ---------------------------------------------------------------------------


def test_two_records_one_domain():
    """The live condition (multiple companies share one domain): both
    records returned, the tiered one preferred, and the merge candidate
    flagged for hygiene."""
    hub = StubHubSpot(by_domain=[ACME_PLUMBING_TIERLESS, ACME_PLUMBING_TIERED])
    result = resolve_company(hub, domain="https://www.acmeplumbing.com/")
    assert len(result["matches"]) == 2
    preferred = {m["hs_company_id"]: m["preferred"] for m in result["matches"]}
    assert preferred == {"51": False, "52": True}
    assert all(m["matched_on"] == "domain" for m in result["matches"])
    assert all(m["confidence"] == "exact" for m in result["matches"])
    assert result["flags"]["merge_candidate"] is True
    assert result["flags"]["fuzzy_only"] is False
    # Input was normalized to the bare registered domain.
    assert hub.last_domain_lookup == "acmeplumbing.com"


def test_both_tiered_prefers_target_account():
    """When BOTH records on the domain carry a tier, the target-account one
    is preferred."""
    tiered_not_target = {**ACME_PLUMBING_TIERLESS,
                         "hs_ideal_customer_profile": "tier_1",
                         "hs_is_target_account": "false"}
    hub = StubHubSpot(by_domain=[tiered_not_target, ACME_PLUMBING_TIERED])
    result = resolve_company(hub, domain="acmeplumbing.com")
    preferred = [m for m in result["matches"] if m["preferred"]]
    assert [m["hs_company_id"] for m in preferred] == ["52"]


def test_slug_chain_dedupes_against_domain_chain():
    """A record found by domain AND slug appears once, keeping the stronger
    matched_on='domain'; a slug-only sibling is listed as an exact linkedin
    match."""
    slug_only = {**ACME_PLUMBING_TIERLESS, "id": "53", "domain": "acmeplumbing.net"}
    hub = StubHubSpot(
        by_domain=[ACME_PLUMBING_TIERED],
        by_slug=[ACME_PLUMBING_TIERED, slug_only],
    )
    result = resolve_company(
        hub,
        domain="acmeplumbing.com",
        linkedin_slug="acme-plumbing",
    )
    matched_on = {m["hs_company_id"]: m["matched_on"] for m in result["matches"]}
    assert matched_on == {"52": "domain", "53": "linkedin"}
    # One record on the domain: not a merge candidate.
    assert result["flags"]["merge_candidate"] is False


def test_landscaping_state_gate_excludes_cross_state():
    """Globex Landscaping NJ vs CA were legitimately different companies:
    a fuzzy name hit whose state disagrees with the requested state is not
    even listed."""
    nj = {
        "id": "61",
        "name": "Globex Landscaping",
        "domain": "globexlandscapingnj.com",
        "state": "NJ",
        "hs_ideal_customer_profile": "tier_2",
        "hs_is_target_account": "false",
    }
    hub = StubHubSpot(fuzzy=[nj])
    result = resolve_company(hub, name="Globex Landscaping", state="CA")
    assert result["matches"] == []
    assert result["flags"]["fuzzy_only"] is False
    # Fuzzy token: first token of the normalized name longer than 3 chars.
    assert hub.last_fuzzy_token == "globex"


def test_fuzzy_distance_gate_and_flags():
    """Levenshtein ≤3 same-state hits list as 'possible' and never become
    preferred (fuzzy_only flag up); distance 4 is excluded; a candidate
    with NO state on file is still listed (the gate needs both sides)."""
    close = {
        "id": "62",
        "name": "Acme Pests",  # distance 1 from "acme pest"
        "domain": "acmepests.com",
        "state": "TX",
        "hs_ideal_customer_profile": "tier_1",
        "hs_is_target_account": "true",
    }
    far = {
        "id": "63",
        "name": "Acme Pestival",  # distance 4 — over the gate
        "domain": "acmepestival.com",
        "state": "TX",
        "hs_ideal_customer_profile": "",
        "hs_is_target_account": "false",
    }
    stateless = {
        "id": "64",
        "name": "Acme Pest Co",  # normalizes to "acme pest" — distance 0
        "domain": "",
        "state": "",
        "hs_ideal_customer_profile": "",
        "hs_is_target_account": "false",
    }
    hub = StubHubSpot(fuzzy=[close, far, stateless])
    result = resolve_company(hub, name="Acme Pest", state="TX")
    ids = {m["hs_company_id"] for m in result["matches"]}
    assert ids == {"62", "64"}
    for match in result["matches"]:
        assert match["matched_on"] == "fuzzy_name"
        assert match["confidence"] == "possible"
        # Never preferred solely from fuzzy — even the tiered target one.
        assert match["preferred"] is False
    assert result["flags"]["fuzzy_only"] is True
    assert result["flags"]["merge_candidate"] is False


def test_all_chains_run_even_with_exact_domain_hit():
    """'Create new' must be a deliberate click past visible candidates: an
    exact domain hit does not stop the fuzzy chain from surfacing a
    near-name record."""
    lookalike = {
        "id": "65",
        "name": "Acme Pests",
        "domain": "acmepests.net",
        "state": "TX",
        "hs_ideal_customer_profile": "",
        "hs_is_target_account": "false",
    }
    exact = {
        "id": "66",
        "name": "Acme Pest",
        "domain": "acmepest.com",
        "state": "TX",
        "hs_ideal_customer_profile": "tier_2",
        "hs_is_target_account": "false",
    }
    hub = StubHubSpot(by_domain=[exact], fuzzy=[lookalike])
    result = resolve_company(hub, domain="acmepest.com", name="Acme Pest", state="TX")
    assert hub.calls["search_companies_fuzzy"] == 1
    matched_on = {m["hs_company_id"]: m["matched_on"] for m in result["matches"]}
    assert matched_on == {"66": "domain", "65": "fuzzy_name"}
    preferred = {m["hs_company_id"]: m["preferred"] for m in result["matches"]}
    assert preferred == {"66": True, "65": False}
    assert result["flags"]["fuzzy_only"] is False


def test_subdomain_record_never_preferred():
    """The careers-subdomain survivor lesson: a careers.* record is flagged
    and can never be the preferred suggestion, even when it is the tiered
    one."""
    careers = {
        "id": "71",
        "name": "Treecorp",
        "domain": "careers.treecorp.com",
        "state": "NY",
        "hs_ideal_customer_profile": "tier_1",
        "hs_is_target_account": "true",
    }
    real = {
        "id": "72",
        "name": "Treecorp",
        "domain": "treecorp.com",
        "state": "NY",
        "hs_ideal_customer_profile": "",
        "hs_is_target_account": "false",
    }
    hub = StubHubSpot(by_domain=[careers, real])
    result = resolve_company(hub, domain="treecorp.com")
    by_id = {m["hs_company_id"]: m for m in result["matches"]}
    assert by_id["71"]["subdomain_record"] is True
    assert by_id["71"]["preferred"] is False
    assert by_id["72"]["subdomain_record"] is False
    assert by_id["72"]["preferred"] is True
    assert result["flags"]["merge_candidate"] is True


def test_only_subdomain_record_means_no_preferred():
    """When the ONLY exact match is a subdomain record, nothing is
    preferred — suggesting it would bake the junk domain into a merge."""
    careers = {
        "id": "71",
        "name": "Treecorp",
        "domain": "careers.treecorp.com",
        "state": "NY",
        "hs_ideal_customer_profile": "tier_1",
        "hs_is_target_account": "true",
    }
    hub = StubHubSpot(by_domain=[careers])
    result = resolve_company(hub, domain="careers.treecorp.com")
    assert len(result["matches"]) == 1
    assert result["matches"][0]["subdomain_record"] is True
    assert result["matches"][0]["preferred"] is False


def test_resolve_company_no_inputs_no_calls():
    hub = StubHubSpot(by_domain=[ACME_PLUMBING_TIERED], fuzzy=[ACME_PLUMBING_TIERED])
    result = resolve_company(hub)
    assert result["matches"] == []
    assert result["flags"] == {"merge_candidate": False, "fuzzy_only": False}
    assert sum(hub.calls.values()) == 0


def test_resolve_company_client_errors_propagate():
    with pytest.raises(RuntimeError, match="hubspot 500"):
        resolve_company(ExplodingHubSpot(), domain="acmepest.com")
