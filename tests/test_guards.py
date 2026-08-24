"""Tests for prospector.guards — known failure modes as fixtures.

Each case encodes a failure mode the guards defend against: a contact who
changed jobs coming back with their new employer's email domain, junk
companies auto-created from a bare email domain, benign alternate-domain
mismatches (sister brands), invented/inferred email addresses that happen
to look plausible, and the fact that a supposed "business email" is often
a personal address.
"""

from __future__ import annotations

import pytest

from prospector.guards import (
    ALLOWED_PHONE_PROPERTY,
    CONSUMER_EMAIL_DOMAINS,
    GuardHold,
    collect_commit_holds,
    email_domain_guard,
    inferred_email_guard,
    linkedin_link_guard,
    phone_field_guard,
)


# ---------------------------------------------------------------------------
# email_domain_guard — the job-changer / auto-create-trap guard
# ---------------------------------------------------------------------------

def test_job_changer_mismatch_blocks():
    """FAILURE MODE: the job-changer — an enrichment returns a contact on
    file at one company but with their NEW employer's email domain.
    Unconfirmed mismatch must be a blocking hold naming both domains."""
    hold = email_domain_guard("jane@globex.com", "initech.com", alternate_confirmed=False)
    assert isinstance(hold, GuardHold)
    assert hold.code == "domain_mismatch"
    assert hold.blocking is True
    assert hold.detail == {"email_domain": "globex.com", "company_domain": "initech.com"}
    # Rep-facing message must name BOTH domains and both choices.
    assert "globex.com" in hold.message
    assert "initech.com" in hold.message
    assert "discard" in hold.message.lower()
    assert "alternate" in hold.message.lower()


def test_confirmed_sister_brand_passes():
    """FAILURE MODE: a northwind.com email on a northwindgroup.com account —
    a benign sister brand that would mint a junk company when unguarded.
    Most real mismatches are benign alternates, so a rep confirmation
    clears the hold."""
    assert email_domain_guard(
        "sam@northwind.com", "northwindgroup.com", alternate_confirmed=True
    ) is None


def test_unconfirmed_sister_brand_still_holds():
    """Same sister-brand pair WITHOUT the rep's confirmation stays held —
    the guard can't tell an alternate domain from a job-changer."""
    hold = email_domain_guard(
        "sam@northwind.com", "northwindgroup.com", alternate_confirmed=False
    )
    assert hold is not None and hold.code == "domain_mismatch" and hold.blocking


def test_registered_domain_collapse_matches():
    """Subdomain-insensitivity: a careers.acmepest.com email vs an
    acmepest.com company is the SAME employer, not a mismatch."""
    assert email_domain_guard(
        "hr@careers.acmepest.com", "acmepest.com", alternate_confirmed=False
    ) is None


def test_company_domain_url_forms_match():
    """extract_domain handles protocol/www/path on the company side."""
    assert email_domain_guard(
        "jane@acme.com", "https://www.acme.com/about", alternate_confirmed=False
    ) is None


def test_no_email_is_allowed():
    """Committing without an email is allowed — HubSpot auto-create can't
    fire without one."""
    assert email_domain_guard(None, "acme.com", alternate_confirmed=False) is None
    assert email_domain_guard("", "acme.com", alternate_confirmed=False) is None
    assert email_domain_guard("   ", None, alternate_confirmed=False) is None


@pytest.mark.parametrize("company_domain", [None, "", "   "])
def test_email_without_company_domain_is_the_auto_create_trap(company_domain):
    """FAILURE MODE: junk companies auto-created — HubSpot's 'automatically
    create and associate companies' setting fires whenever a new contact's
    email domain matches no existing company. Email + no company domain is
    exactly that population: blocking."""
    hold = email_domain_guard("jane@northwind.com", company_domain, alternate_confirmed=False)
    assert hold is not None
    assert hold.code == "auto_create_trap"
    assert hold.blocking is True
    assert hold.detail["email_domain"] == "northwind.com"


def test_auto_create_trap_not_cleared_by_confirmation():
    """alternate_confirmed only clears a MISMATCH between two known domains;
    with no company domain there's nothing to have confirmed."""
    hold = email_domain_guard("jane@northwind.com", None, alternate_confirmed=True)
    assert hold is not None and hold.code == "auto_create_trap"


def test_consumer_email_is_a_nonblocking_hold():
    """FAILURE MODE: a supposed 'business email' is often a PERSONAL
    address. gmail.com can never match a company and can't auto-create
    sensibly — non-blocking hold advising the personal-email property."""
    hold = email_domain_guard("bob@gmail.com", "acme.com", alternate_confirmed=False)
    assert hold is not None
    assert hold.code == "consumer_email"
    assert hold.blocking is False
    assert "personal" in hold.message.lower()


def test_consumer_email_beats_the_auto_create_trap():
    """A gmail address with NO company domain is still consumer_email, not
    auto_create_trap — the right fix is the personal-email property, not
    attaching a company."""
    hold = email_domain_guard("bob@gmail.com", None, alternate_confirmed=False)
    assert hold is not None and hold.code == "consumer_email"


@pytest.mark.parametrize("email", [
    "a@yahoo.com",
    "a@outlook.com",
    "a@hotmail.com",
    "a@icloud.com",
    "a@aol.com",
    "a@proton.me",
    "a@msn.com",
    "a@live.com",
    "a@me.com",
    "a@comcast.net",
    "a@att.net",
    "a@verizon.net",
    "a@mail.comcast.net",  # subdomain of a consumer provider is still consumer
])
def test_consumer_domain_roster(email):
    hold = email_domain_guard(email, "acme.com", alternate_confirmed=False)
    assert hold is not None and hold.code == "consumer_email" and not hold.blocking


def test_consumer_frozenset_is_module_level():
    assert isinstance(CONSUMER_EMAIL_DOMAINS, frozenset)
    assert "gmail.com" in CONSUMER_EMAIL_DOMAINS


def test_malformed_email_is_held_not_passed():
    """An email with no parseable domain can never be verified against the
    company — blocked as a mismatch rather than waved through."""
    hold = email_domain_guard("not-an-email", "acme.com", alternate_confirmed=False)
    assert hold is not None and hold.blocking


# ---------------------------------------------------------------------------
# inferred_email_guard — inference is for judgment, never identity
# ---------------------------------------------------------------------------

def test_inferred_email_blocks():
    """FAILURE MODE: an enrichment routine INVENTED plausible-looking
    addresses (e.g. a company mapped to an unrelated city-government or
    university domain). Inferred status is always a blocking hold, even
    when domains would match."""
    hold = inferred_email_guard("inferred")
    assert hold is not None
    assert hold.code == "inferred_email"
    assert hold.blocking is True


def test_inferred_status_is_case_insensitive():
    hold = inferred_email_guard("  Inferred ")
    assert hold is not None and hold.code == "inferred_email"


@pytest.mark.parametrize("status", [None, "", "verified", "found", "valid"])
def test_non_inferred_statuses_pass(status):
    assert inferred_email_guard(status) is None


# ---------------------------------------------------------------------------
# linkedin_link_guard — the one-click hs_linkedin_url backfill
# ---------------------------------------------------------------------------

def test_linkedin_backfill_onto_empty_is_safe():
    assert linkedin_link_guard(None, "https://www.linkedin.com/in/jane-doe") is None
    assert linkedin_link_guard("", "https://www.linkedin.com/in/jane-doe") is None


def test_linkedin_same_profile_different_spelling_is_noop():
    """www + trailing slash vs bare spelling of the SAME slug — norm_linkedin
    collapses them, so re-linking is a harmless no-op."""
    assert linkedin_link_guard(
        "https://www.linkedin.com/in/jane-doe/",
        "linkedin.com/in/jane-doe",
    ) is None


def test_linkedin_different_slug_conflicts():
    """A different existing slug likely means a DIFFERENT PERSON — the
    cross-source identity weld in miniature. Blocking."""
    hold = linkedin_link_guard(
        "https://www.linkedin.com/in/jane-doe",
        "https://www.linkedin.com/in/jane-doe-8a41b2",
    )
    assert hold is not None
    assert hold.code == "linkedin_conflict"
    assert hold.blocking is True
    assert hold.detail["existing_url"] == "linkedin.com/in/jane-doe"
    assert hold.detail["new_url"] == "linkedin.com/in/jane-doe-8a41b2"


# ---------------------------------------------------------------------------
# phone_field_guard — standing rule: no mobilephone
# ---------------------------------------------------------------------------

def test_mobilephone_is_refused():
    hold = phone_field_guard("mobilephone")
    assert hold is not None
    assert hold.code == "wrong_phone_property"
    assert hold.blocking is True
    assert hold.detail == {"requested_property": "mobilephone"}


def test_phone_is_the_only_allowed_property():
    assert ALLOWED_PHONE_PROPERTY == "phone"
    assert phone_field_guard("phone") is None
    assert phone_field_guard("hs_whatsapp_phone_number") is not None


# ---------------------------------------------------------------------------
# collect_commit_holds — ordering and aggregation
# ---------------------------------------------------------------------------

def test_collect_inferred_beats_domain_checks():
    """An inferred email is blocked even when its domain MATCHES the company
    (an invented address can pass a domain check at the right account) — so
    the inferred hold must come first and cannot be hidden by a clean domain
    verdict."""
    holds = collect_commit_holds(
        email="jane@acme.com",
        email_status="inferred",
        company_domain="acme.com",
        alternate_confirmed=False,
    )
    assert [h.code for h in holds] == ["inferred_email"]
    assert holds[0].blocking


def test_collect_returns_all_applicable_holds_inferred_first():
    holds = collect_commit_holds(
        email="jane@globex.com",
        email_status="inferred",
        company_domain="initech.com",
        alternate_confirmed=False,
    )
    assert [h.code for h in holds] == ["inferred_email", "domain_mismatch"]


def test_collect_clean_commit_has_no_holds():
    holds = collect_commit_holds(
        email="jane@careers.acme.com",
        email_status="verified",
        company_domain="https://www.acme.com",
        alternate_confirmed=False,
    )
    assert holds == []


def test_collect_no_email_no_holds():
    holds = collect_commit_holds(
        email=None,
        email_status=None,
        company_domain=None,
        alternate_confirmed=False,
    )
    assert holds == []
