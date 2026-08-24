"""Regression tests for the live-smoke failure:
FullEnrich rejected our first real request with error.enrichment.data.empty
because the datum carried empty names and a protocol-less LinkedIn URL --
and, it later turned out, v1-folklore key names (firstname/lastname) the
v2 API silently drops.
"""

from __future__ import annotations

from prospector.providers.fullenrich import FullEnrichAdapter
from prospector.providers.types import EnrichInput


def _datum(inp):
    return FullEnrichAdapter._datum(inp, ["contact.work_emails"])


def test_canonical_protocol_less_url_becomes_real_url():
    d = _datum(EnrichInput(linkedin_url="linkedin.com/in/sample-person"))
    assert d["linkedin_url"] == "https://www.linkedin.com/in/sample-person"


def test_full_https_url_passes_through_untouched():
    d = _datum(EnrichInput(linkedin_url="https://www.linkedin.com/in/sample-person"))
    assert d["linkedin_url"] == "https://www.linkedin.com/in/sample-person"


def test_names_ride_along_when_present():
    d = _datum(EnrichInput(linkedin_url="linkedin.com/in/sample-person",
                           first_name="Chris", last_name="Oland",
                           company_domain="oland.com"))
    # v2 snake_case name keys -- 'firstname'/'lastname' was the v1 folklore
    # the live API silently dropped.
    assert (d["first_name"], d["last_name"], d["domain"]) == (
        "Chris", "Oland", "oland.com")
    assert "firstname" not in d
    assert "lastname" not in d
