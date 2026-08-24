"""Tests for prospector.nicknames (vendored from a shared name-matching helper).

The name-agreement cases are ported from a shared name-matching helper's test suite —
several are regression tests transcribed from real CRM records — plus
incident-derived cases requested for the prospector.
"""

from __future__ import annotations

from prospector.nicknames import (
    names_agree,
    normalize_name,
    strip_trailing_credentials,
)


# ---------------------------------------------------------------------------
# strip_trailing_credentials
# ---------------------------------------------------------------------------

def test_strip_trailing_credentials_mba():
    assert strip_trailing_credentials("Jane Doe, MBA") == "Jane Doe"


def test_strip_trailing_credentials_fmp():
    # Real record: a BluSky VP filed as "Colby Wrathall, FMP"
    assert strip_trailing_credentials("Colby Wrathall, FMP") == "Colby Wrathall"


def test_strip_trailing_credentials_keeps_last_first_order():
    # "Wrathall, Colby" is a reversed name, not a credential — must survive.
    assert strip_trailing_credentials("Wrathall, Colby") == "Wrathall, Colby"


def test_strip_trailing_credentials_no_comma_passthrough():
    assert strip_trailing_credentials("Jane Doe") == "Jane Doe"
    assert strip_trailing_credentials(None) == ""
    assert strip_trailing_credentials("") == ""


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------

def test_normalize_name_accents_and_credentials():
    assert normalize_name("José García") == "jose garcia"
    assert normalize_name("Alex Mortensen, CPA") == "alex mortensen"
    assert normalize_name("Bobby Smith Jr.") == "bobby smith"
    assert normalize_name(None) == ""


# ---------------------------------------------------------------------------
# names_agree — ported from the shared helper's tests
# ---------------------------------------------------------------------------

def test_names_agree_on_credential_suffixes():
    assert names_agree("Alex Mortensen", "Alex Mortensen, CPA")
    assert names_agree("Alyson Cagle, CPTD", "Alyson Cagle")


def test_names_agree_on_nickname():
    assert names_agree("Mike McHugh", "Michael McHugh")
    assert names_agree("Dave Smith", "David Smith")


def test_names_reject_different_first_names_same_surname():
    """The live CRM landmine: Michael Sorensen vs Merri Sorensen — same
    surname, different human. Must never agree."""
    assert not names_agree("Michael Sorensen", "Merri Sorensen")


def test_names_reject_different_surnames():
    assert not names_agree("Jane Doe", "Jane Roe")


def test_names_handle_accents_and_blanks():
    assert names_agree("José García", "Jose Garcia")
    assert not names_agree("", "Somebody")
    assert not names_agree(None, None)


# ---------------------------------------------------------------------------
# names_agree — incident-derived cases for the prospector
# ---------------------------------------------------------------------------

def test_names_agree_billy_william():
    assert names_agree("Billy Smith", "William Smith")


def test_names_agree_michael_mike():
    assert names_agree("Michael Johnson", "Mike Johnson")


def test_names_agree_bradford_brad():
    # Not in the nickname table — covered by the 3+ character prefix rule.
    assert names_agree("Bradford Jones", "Brad Jones")


def test_names_agree_is_symmetric_for_nicknames():
    assert names_agree("William Smith", "Billy Smith")
    assert names_agree("Brad Jones", "Bradford Jones")


def test_single_initials_never_weld_people():
    # The prefix rule requires 3+ chars on both sides.
    assert not names_agree("J Smith", "James Smith")
    assert not names_agree("Al Smith", "Albert Smith")  # 2 chars, not in table
