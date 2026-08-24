"""Tests for prospector.matching (vendored from an internal hygiene module).

The upstream module shipped without tests, so these were written fresh against
the vendored copy. They pin the normalization behavior the prospector's dedupe
depends on, plus real-world cases (LinkedIn URL spellings, company-suffix
stripping) that have bitten in production elsewhere.
"""

from __future__ import annotations

import pytest

from prospector.matching import (
    dedupe_pairs_by_tier,
    email_domain,
    email_local,
    find_company_duplicates,
    find_contact_duplicates,
    levenshtein,
    norm_company_name,
    norm_email,
    norm_linkedin,
    norm_name,
)
from prospector.filters import extract_domain, normalize_phone


# ---------------------------------------------------------------------------
# norm_email / norm_name / email parts
# ---------------------------------------------------------------------------

def test_norm_email():
    assert norm_email("  Jane.Doe@Example.COM ") == "jane.doe@example.com"
    assert norm_email("") == ""
    assert norm_email(None) == ""


def test_norm_name_collapses_whitespace_and_case():
    assert norm_name("  Jane   DOE ") == "jane doe"
    assert norm_name(None) == ""


def test_email_local_and_domain():
    assert email_local("jane@acme.com") == "jane"
    assert email_domain("jane@acme.com") == "acme.com"
    assert email_local("not-an-email") == ""
    assert email_domain("not-an-email") == ""


# ---------------------------------------------------------------------------
# norm_linkedin — incident-derived spelling equivalences
# ---------------------------------------------------------------------------

CANONICAL_LI = "linkedin.com/in/jane-doe"


@pytest.mark.parametrize("raw", [
    "https://www.linkedin.com/in/jane-doe",
    "https://www.linkedin.com/in/jane-doe/",           # trailing slash
    "https://www.linkedin.com/in/jane-doe?utm_source=share&x=1",  # query junk
    "https://www.linkedin.com/in/jane-doe#section",    # fragment junk
    "HTTPS://WWW.LINKEDIN.COM/IN/JANE-DOE",            # uppercase slug
    "www.linkedin.com/in/jane-doe",                    # missing https
    "linkedin.com/in/jane-doe",                        # bare
    "http://linkedin.com/in/jane-doe/",                # http, no www
])
def test_norm_linkedin_equivalences(raw):
    assert norm_linkedin(raw) == CANONICAL_LI


def test_norm_linkedin_mobile_host_is_not_collapsed():
    """Documented behavior: only a leading www. is stripped, so an m.linkedin
    URL keeps its host and does NOT collapse to the canonical form. Kept as a
    behavior pin — fixing this belongs upstream in the source module first."""
    assert norm_linkedin("https://m.linkedin.com/in/jane-doe") == "m.linkedin.com/in/jane-doe"
    assert norm_linkedin("https://m.linkedin.com/in/jane-doe") != CANONICAL_LI


def test_norm_linkedin_empty():
    assert norm_linkedin("") == ""
    assert norm_linkedin(None) == ""


# ---------------------------------------------------------------------------
# norm_company_name — suffix stripping
# ---------------------------------------------------------------------------

def test_norm_company_name_strips_llc():
    assert norm_company_name("Acme Pest Control LLC") == norm_company_name("Acme Pest Control")
    assert norm_company_name("Acme Pest Control LLC") == "acme pest control"


def test_norm_company_name_strips_inc_dot():
    assert (norm_company_name("Acme Plumbing Inc.")
            == norm_company_name("Acme Plumbing"))
    assert norm_company_name("Acme Plumbing Inc.") == "acme plumbing"


def test_norm_company_name_strips_stacked_suffixes_and_punctuation():
    assert norm_company_name("Acme Holdings, LLC") == "acme"
    assert norm_company_name("Ace & Sons Co.") == "ace sons"


def test_norm_company_name_empty():
    assert norm_company_name(None) == ""
    assert norm_company_name("") == ""


# ---------------------------------------------------------------------------
# levenshtein
# ---------------------------------------------------------------------------

def test_levenshtein_basics():
    assert levenshtein("kitten", "sitting") == 3
    assert levenshtein("same", "same") == 0
    assert levenshtein("", "abc") == 3
    assert levenshtein("abc", "") == 3


def test_levenshtein_max_dist_cutoff():
    # Exceeding max_dist short-circuits to max_dist + 1
    assert levenshtein("abcdef", "uvwxyz", max_dist=2) == 3
    # Length-difference shortcut alone can trigger the cutoff
    assert levenshtein("a", "abcdefgh", max_dist=3) == 4
    # Within the cap, exact distance comes back
    assert levenshtein("kitten", "sitting", max_dist=3) == 3


# ---------------------------------------------------------------------------
# Filters used by matching
# ---------------------------------------------------------------------------

def test_extract_domain():
    assert extract_domain("https://www.acme.com/path?x=1") == "acme.com"
    assert extract_domain("ACME.COM") == "acme.com"
    assert extract_domain(None) == ""


def test_normalize_phone():
    assert normalize_phone("(801) 555-1234") == "8015551234"
    assert normalize_phone("+1 801-555-1234") == "+18015551234"
    assert normalize_phone(None) == ""


# ---------------------------------------------------------------------------
# Record-shaped duplicate detection
# ---------------------------------------------------------------------------

def _contact(cid, **props):
    return {"id": cid, "properties": props}


def _company(cid, **props):
    return {"id": cid, "properties": props}


def test_find_contact_duplicates_exact_email():
    contacts = [
        _contact("1", email="jane@acme.com", firstname="Jane", lastname="Doe"),
        _contact("2", email="Jane@Acme.com", firstname="J", lastname="Doe"),
        _contact("3", email="other@acme.com", firstname="Bob", lastname="Smith"),
    ]
    pairs = find_contact_duplicates(contacts)
    email_pairs = [p for p in pairs if p["signal"] == "exact_email"]
    assert len(email_pairs) == 1
    p = email_pairs[0]
    assert p["tier"] == "high"
    assert {p["contact_a_id"], p["contact_b_id"]} == {"1", "2"}


def test_find_contact_duplicates_linkedin_spelling_variants():
    contacts = [
        _contact("1", hs_linkedin_url="https://www.linkedin.com/in/jane-doe/",
                 firstname="Jane", lastname="Doe"),
        _contact("2", hs_linkedin_url="linkedin.com/in/jane-doe?utm=x",
                 firstname="Jane", lastname="Doe"),
    ]
    pairs = find_contact_duplicates(contacts)
    assert any(p["signal"] == "linkedin_url" and p["tier"] == "high" for p in pairs)


def test_find_company_duplicates_same_domain():
    companies = [
        _company("10", name="Acme Pest Control LLC", domain="acme.com"),
        _company("11", name="Acme Pest Control", website="https://www.acme.com/"),
    ]
    pairs = find_company_duplicates(companies)
    assert any(p["signal"] == "exact_domain" and p["tier"] == "high" for p in pairs)


def test_find_company_duplicates_same_name_missing_domain():
    companies = [
        _company("10", name="Acme Plumbing Inc."),
        _company("11", name="Acme Plumbing", domain="acmeplumbing.com"),
    ]
    pairs = find_company_duplicates(companies)
    assert any(p["signal"] == "same_name" and p["tier"] == "medium" for p in pairs)


def test_dedupe_pairs_by_tier_keeps_highest():
    pairs = [
        {"tier": "medium", "signal": "same_name", "score": 500,
         "company_a_id": "1", "company_b_id": "2"},
        {"tier": "high", "signal": "exact_domain", "score": 1000,
         "company_a_id": "1", "company_b_id": "2"},
    ]
    out = dedupe_pairs_by_tier(pairs, ("company_a_id", "company_b_id"))
    assert len(out) == 1
    assert out[0]["tier"] == "high"
