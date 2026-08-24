"""Unit tests for prospector.owners — the simplified owner resolution.

resolve_owner now takes a single rep owner id: it returns that id with source
"rep" when it is non-empty, otherwise the triage sentinel. There is no DB,
territory map, or closed-lost logic left to stub.
"""

from __future__ import annotations

from prospector.owners import TRIAGE_SENTINEL, normalize_state, resolve_owner


# -- owner resolution ----------------------------------------------------------


def test_rep_owner_id_resolves_to_that_rep():
    assert resolve_owner("901") == ("901", "rep")


def test_missing_rep_owner_id_routes_to_triage():
    assert resolve_owner(None) == (TRIAGE_SENTINEL, "triage")


def test_empty_rep_owner_id_routes_to_triage():
    assert resolve_owner("") == (TRIAGE_SENTINEL, "triage")


def test_whitespace_only_rep_owner_id_routes_to_triage():
    assert resolve_owner("   ") == (TRIAGE_SENTINEL, "triage")


def test_rep_owner_id_is_stripped():
    assert resolve_owner("  42  ") == ("42", "rep")


def test_non_string_rep_owner_id_is_coerced():
    """Some APIs deliver owner ids as ints; the resolver stringifies them."""
    assert resolve_owner(67890) == ("67890", "rep")


# -- state normalization --------------------------------------------------------------


def test_normalize_state_variants():
    assert normalize_state("TX") == "TX"
    assert normalize_state("tx") == "TX"
    assert normalize_state(" Texas ") == "TX"
    assert normalize_state("district of columbia") == "DC"
    assert normalize_state("DC") == "DC"
    assert normalize_state("New  Mexico") == "NM"  # doubled whitespace
    assert normalize_state("XX") is None  # not a real USPS code
    assert normalize_state("Atlantis") is None
    assert normalize_state("") is None
    assert normalize_state(None) is None
