"""Owner resolution for the Prospecting Plugin.

Decides which HubSpot owner a newly-created contact/company is assigned to.

The rule here is deliberately simple and generic: a contact created by a rep
is assigned to THAT rep's HubSpot owner id. If the rep has no owner id on
file, ownership cannot be resolved and the record is routed to triage rather
than assigned silently.

    1. rep     -- the committing rep's own hubspot_owner_id, when they have one.
    2. triage  -- everything else lands unassigned, flagged for a human.

`TRIAGE_SENTINEL` makes "we could not resolve an owner" an explicit, queryable
outcome. A record is NEVER assigned to some default rep behind the scenes.

If your team routes ownership differently -- by territory, by round-robin, by
a lookup against your own CRM/data-warehouse -- this is the single place to
change it: return the owner id you want (and a short `source` label for the
audit note), or `TRIAGE_SENTINEL` when you cannot decide.

`normalize_state` is a small, dependency-free US-state normalizer used when
writing a new company's `state` property as a two-letter code.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Sentinel owner for anything we cannot confidently route. Never assign
# silently to a default rep -- unresolved records go to triage, where a human
# routes them and the gap stays visible.
TRIAGE_SENTINEL = "REVOPS_TRIAGE"

# Full US state name (uppercased) -> USPS code, including DC. Input like
# 'Texas' or 'district of columbia' normalizes through this; a 'TX' short-form
# input is validated against the code set instead.
_STATE_NAME_TO_CODE: dict[str, str] = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN",
    "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI",
    "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
}

_VALID_STATE_CODES = frozenset(_STATE_NAME_TO_CODE.values())


def normalize_state(state: str | None) -> str | None:
    """Normalize a raw state value to a two-letter USPS code, or None.

    Accepts either the code ('TX', 'tx') or the full name ('Texas',
    'district of columbia'). Anything unresolvable returns None -- the caller
    simply omits the state rather than guessing.
    """
    if not state:
        return None
    cleaned = " ".join(state.strip().upper().split())
    if not cleaned:
        return None
    if len(cleaned) == 2:
        return cleaned if cleaned in _VALID_STATE_CODES else None
    return _STATE_NAME_TO_CODE.get(cleaned)


def resolve_owner(rep_owner_id: str | None) -> tuple[str, str]:
    """Resolve the owning HubSpot owner id for a newly-created record.

    Returns (owner_id, source): the committing rep's own owner id with
    source "rep" when they have one, otherwise (TRIAGE_SENTINEL, "triage").

    This is the extension point for your own routing. Swap the body for a
    territory map, a round-robin, or a lookup against your CRM -- just return
    an owner id and a short source label, or TRIAGE_SENTINEL to leave it for
    a human.
    """
    if rep_owner_id and str(rep_owner_id).strip():
        return str(rep_owner_id).strip(), "rep"
    return TRIAGE_SENTINEL, "triage"
