"""Post-enrichment, pre-commit duplicate resolution.

This is the LIVE dedupe check that runs after enrichment and immediately
before a commit is offered to the rep. Rule zero applies: it reads HubSpot
only, never a downstream reporting mirror — a nightly-synced mirror can't
see contacts written earlier the same day, and the live check catches
duplicates a mirror-backed check would miss.

Both resolvers return CANDIDATES for a human, never decisions:
  * 'exact' confidence (linkedin/email/domain/slug) means the record IS the
    same entity as far as the identifier goes;
  * 'possible' confidence (name+company, fuzzy name) means "needs your
    eyes" — a merge pass found many same-name clusters were legitimately
    different companies (franchises, trade-name collisions), so nothing here
    ever auto-merges or auto-picks off a fuzzy signal.

All chains run even when an exact match exists: 'Create new' must always be
a deliberate click past every visible candidate, so the rep sees the
near-matches too. The single documented exception is in resolve_contact —
when the LinkedIn URL matched a contact AND the email chain matched the
same contact, the name+company search is skipped: two independent exact
identifiers already agree on one person, and a third, weaker net can only
re-find that person or drag in namesakes.

The HubSpot client is injected, never imported (a sibling owns hubspot.py);
this module codes strictly against the agreed interface:
find_contact_by_linkedin, find_contacts_by_emails, find_contacts_by_name,
find_companies_by_domain, find_company_by_linkedin_slug,
search_companies_fuzzy. Client exceptions are deliberately NOT caught —
the routes map errors, and a swallowed failure here would present a
duplicate as net-new.
"""

from __future__ import annotations

import re
from typing import Any

from .filters import extract_domain
from .matching import levenshtein, norm_company_name, norm_linkedin
from .nicknames import names_agree

# Strongest wins when one record is found by several chains: an identifier
# match beats a name heuristic, and a URL (a real primary key) beats an
# address (one person can hold two).
_CONTACT_STRENGTH = {"linkedin": 0, "email": 1, "name_company": 2}
_COMPANY_STRENGTH = {"domain": 0, "linkedin": 1, "fuzzy_name": 2}

# Fuzzy company-name gate, matching the shared merge tooling (Levenshtein
# ≤ 3 on norm_company_name).
_FUZZY_MAX_DISTANCE = 3

# Host labels that mark a company record keyed on an email/careers
# subdomain rather than the real website. The lesson: HubSpot keeps the
# primary's domain on merge, so suggesting a careers.<company>.com record as
# preferred would bake the junk domain into the surviving record. Such
# records are flagged and never preferred.
_SUBDOMAIN_LABELS = frozenset({"careers", "mail", "jobs", "mail2", "smtp", "webmail"})


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _dedupe(cards: list[dict], strength: dict[str, int], id_key: str) -> list[dict]:
    """Collapse one record found by several chains to its strongest card.

    Keeps first-seen order within a strength band (dict preserves insertion,
    sorted() is stable), so the output reads strongest-first and otherwise
    in chain order.
    """
    best: dict[str, dict] = {}
    for card in cards:
        key = card[id_key]
        held = best.get(key)
        if held is None or strength[card["matched_on"]] < strength[held["matched_on"]]:
            best[key] = card
    return sorted(best.values(), key=lambda c: strength[c["matched_on"]])


def _full_name(record: dict) -> str:
    return " ".join(
        part for part in (record.get("firstname"), record.get("lastname")) if part
    ).strip()


def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1] if "@" in email else ""


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def resolve_contact(
    hubspot: Any,
    *,
    linkedin_url: str = "",
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    company_name: str = "",
) -> dict:
    """Post-enrichment, pre-commit contact duplicate check.

    Chain, strongest first:
      1. normalized hs_linkedin_url (matching.norm_linkedin) — widely
         populated in the portal, a real primary key;
      2. exact email — LIVE against HubSpot, and via the client it includes
         hs_additional_emails (the one-person-two-addresses pattern: one
         person, two addresses on one record);
      3. name + company — 'possible match', NEVER auto-merge. This is the
         only net that catches what slips through email dedupe: nickname
         variants (William/Billy Smith, via names_agree) and same-person
         different-address pairs (m.brown@ vs mbrown@, or one person on two
         spellings of the same company domain). Company agreement comes from
         norm_company_name equality OR the candidate's email domain (see
         _company_evidence).

    All chains with inputs run even after an exact hit — the rep must see
    near-matches before 'Create new'. Sole exception: when the LinkedIn URL
    matched AND the email chain matched the same contact, the name+company
    search is skipped (two independent exact identifiers already agree; the
    weaker net could only re-find that contact or pull in namesakes).

    Returns {"matches": [...]} — cards deduped by hs_contact_id keeping the
    strongest matched_on (linkedin > email > name_company). Client
    exceptions propagate untouched.
    """
    cards: list[dict] = []

    norm_url = norm_linkedin(linkedin_url)
    linkedin_match = None
    if norm_url:
        linkedin_match = hubspot.find_contact_by_linkedin(norm_url)
        if linkedin_match:
            cards.append(_contact_card(linkedin_match, "linkedin", "exact"))

    email_norm = (email or "").strip().lower()
    email_match_ids: set[str] = set()
    if email_norm:
        for contact in hubspot.find_contacts_by_emails([email_norm]) or []:
            email_match_ids.add(str(contact["id"]))
            cards.append(_contact_card(contact, "email", "exact"))

    # The documented exception (module docstring): linkedin and email both
    # matched, and they agree on ONE contact — skip the name+company net.
    skip_name_chain = (
        linkedin_match is not None
        and bool(email_match_ids)
        and email_match_ids == {str(linkedin_match["id"])}
    )

    # The name chain needs a name AND some company evidence to test against
    # (a stated company name, or the enriched email's domain); without
    # either, no candidate could ever qualify, so the search is not made.
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    if (
        not skip_name_chain
        and first
        and last
        and ((company_name or "").strip() or email_norm)
    ):
        enriched_name = f"{first} {last}"
        for candidate in hubspot.find_contacts_by_name(first, last) or []:
            if not names_agree(_full_name(candidate), enriched_name):
                continue
            if not _company_evidence(candidate, company_name, email_norm):
                continue
            cards.append(_contact_card(candidate, "name_company", "possible"))

    return {"matches": _dedupe(cards, _CONTACT_STRENGTH, "hs_contact_id")}


def _contact_card(contact: dict, matched_on: str, confidence: str) -> dict:
    return {
        "hs_contact_id": str(contact["id"]),
        "matched_on": matched_on,
        "confidence": confidence,
        "name": _full_name(contact) or contact.get("email") or f"id:{contact['id']}",
        "email": contact.get("email"),
        "jobtitle": contact.get("jobtitle"),
        "hs_linkedin_url": contact.get("hs_linkedin_url"),
        "hubspot_owner_id": contact.get("hubspot_owner_id"),
    }


def _company_evidence(candidate: dict, company_name: str, email_norm: str) -> bool:
    """Does this same-named candidate plausibly sit at the same company?

    Three independent signals, any one suffices:
      1. norm_company_name equality on the stored company field
         (Billy Smith @ "Acme Pest Control LLC" vs enriched "Acme Pest
         Control" — suffix-stripped equal);
      2. the candidate's email domain equals the enriched email's domain
         (m.brown@acme vs mbrown@acme — exact-email missed the different
         locals, the shared domain is the company tell);
      3. the candidate's email domain spells the company name (e.g.
         sam@acme-pest.com vs sam@acmepest.com — different domains for
         one company; both squeeze to "acmepest").
    """
    candidate_email = (candidate.get("email") or "").strip().lower()
    candidate_domain = _email_domain(candidate_email)
    wanted_norm = norm_company_name(company_name)

    if wanted_norm and norm_company_name(candidate.get("company")) == wanted_norm:
        return True
    if (
        email_norm
        and candidate_domain
        and _email_domain(email_norm) == candidate_domain
    ):
        return True
    if wanted_norm and candidate_domain:
        squeezed = re.sub(r"[^a-z0-9]", "", wanted_norm)
        with_tld = re.sub(r"[^a-z0-9]", "", candidate_domain)
        sans_tld = re.sub(r"[^a-z0-9]", "", candidate_domain.rsplit(".", 1)[0])
        if squeezed and squeezed in (with_tld, sans_tld):
            return True
    return False


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

def resolve_company(
    hubspot: Any,
    *,
    domain: str = "",
    linkedin_slug: str = "",
    name: str = "",
    state: str = "",
) -> dict:
    """Company duplicate check: registered domain → LinkedIn
    company slug → fuzzy name + state. All chains with inputs run — the rep
    sees every candidate before 'Create new'.

    Rules earned by the merge history:
      * Multi-record on one domain is a LIVE CONDITION, not an edge case
        (we routinely see one company twice — one tiered, one not). ALL are
        returned; preferred = the tiered one (hs_ideal_customer_profile
        set), and when more than one is tiered, the target-account one, else
        the first; and flags.merge_candidate goes up when one domain holds
        >1 record.
      * Fuzzy name matches are CANDIDATES ONLY — same name ≠ same company
        (many same-name clusters turn out to be legitimately separate; e.g.
        a franchise brand in two different states is two businesses). Gate:
        levenshtein on norm_company_name ≤ 3, and when BOTH sides carry a
        state the states must match to even be listed. matched_on=
        'fuzzy_name', confidence='possible', never preferred solely from
        fuzzy.
      * A careers/mail/jobs/mail2/smtp/webmail subdomain record can never
        be the preferred suggestion (the survivor lesson: HubSpot keeps the
        primary's domain on merge). Flagged subdomain_record=True and
        excluded from preferred selection — even when tiered.

    Fuzzy fetch strategy: the first token of norm_company_name longer than
    3 chars (else the first token) goes to search_companies_fuzzy (name
    CONTAINS_TOKEN recall net); correctness is enforced locally with the
    levenshtein + state gates above.

    Returns {"matches": [...], "flags": {"merge_candidate", "fuzzy_only"}};
    each match carries hs_company_id, name, domain, state, icp_tier,
    is_target, matched_on, confidence, preferred, subdomain_record. Client
    exceptions propagate untouched.
    """
    cards: list[dict] = []

    norm_domain = extract_domain(domain)
    domain_matches: list[dict] = []
    if norm_domain:
        domain_matches = hubspot.find_companies_by_domain(norm_domain) or []
        for company in domain_matches:
            cards.append(_company_match_card(company, "domain", "exact"))

    slug = (linkedin_slug or "").strip().lower()
    if slug:
        for company in hubspot.find_company_by_linkedin_slug(slug) or []:
            cards.append(_company_match_card(company, "linkedin", "exact"))

    wanted_norm = norm_company_name(name)
    if wanted_norm:
        # Normalize BOTH sides to two-letter codes: the book holds 'TX' and
        # 'Texas' interchangeably, so raw string equality would
        # wrongly gate out same-state candidates. Unresolvable state text
        # normalizes to None and skips the gate rather than blocking.
        from .owners import normalize_state
        wanted_state = normalize_state(state) or ""
        for company in hubspot.search_companies_fuzzy(_fuzzy_token(wanted_norm)) or []:
            candidate_norm = norm_company_name(company.get("name"))
            if not candidate_norm:
                continue
            distance = levenshtein(
                wanted_norm, candidate_norm, max_dist=_FUZZY_MAX_DISTANCE
            )
            if distance > _FUZZY_MAX_DISTANCE:
                continue
            candidate_state = normalize_state(company.get("state")) or ""
            if wanted_state and candidate_state and wanted_state != candidate_state:
                # Both sides carry a state and they disagree: the
                # same-name-different-state rule — a shared brand name in two
                # states is two legitimately different companies, so a
                # cross-state fuzzy hit isn't even listed.
                continue
            cards.append(_company_match_card(company, "fuzzy_name", "possible"))

    matches = _dedupe(cards, _COMPANY_STRENGTH, "hs_company_id")

    preferred_id = _pick_preferred(matches)
    for match in matches:
        match["preferred"] = match["hs_company_id"] == preferred_id

    return {
        "matches": matches,
        "flags": {
            "merge_candidate": len(domain_matches) > 1,
            "fuzzy_only": bool(matches)
            and all(m["matched_on"] == "fuzzy_name" for m in matches),
        },
    }


def _fuzzy_token(wanted_norm: str) -> str:
    """First token of the normalized name longer than 3 chars, else the
    first token — short leading tokens ('a', 'the', 'ace') would flood the
    CONTAINS_TOKEN recall net with unrelated names."""
    tokens = wanted_norm.split()
    return next((t for t in tokens if len(t) > 3), tokens[0])


def _company_match_card(company: dict, matched_on: str, confidence: str) -> dict:
    return {
        "hs_company_id": str(company["id"]),
        "name": company.get("name"),
        "domain": company.get("domain"),
        "state": company.get("state"),
        "icp_tier": company.get("hs_ideal_customer_profile") or None,
        "is_target": _is_true(company.get("hs_is_target_account")),
        "matched_on": matched_on,
        "confidence": confidence,
        "preferred": False,  # decided after dedup, across all chains
        "subdomain_record": _is_subdomain_record(company.get("domain")),
    }


def _is_true(value: Any) -> bool:
    """HubSpot booleans arrive as real bools or as 'true'/'false' strings
    depending on the endpoint — accept both spellings of true."""
    return value is True or str(value).strip().lower() == "true"


def _is_subdomain_record(raw_domain: str | None) -> bool:
    """True when the record's domain is a careers/mail-style subdomain
    (e.g. careers.<company>.com) rather than a registrable website domain."""
    labels = [label for label in extract_domain(raw_domain).split(".") if label]
    return len(labels) >= 3 and labels[0] in _SUBDOMAIN_LABELS


def _pick_preferred(matches: list[dict]) -> str | None:
    """The one match the panel should suggest, or None.

    Eligible = exact matches (domain/linkedin) that are not subdomain
    records — fuzzy hits are never preferred (candidates only), and the
    subdomain rule bars subdomain records even when tiered. Among eligible:
    the tiered one wins; if several are tiered, the target-account one,
    else the first; with no tiered record, the first eligible.
    """
    eligible = [
        m
        for m in matches
        if m["matched_on"] != "fuzzy_name" and not m["subdomain_record"]
    ]
    if not eligible:
        return None
    tiered = [m for m in eligible if m["icp_tier"]]
    if tiered:
        targeted = [m for m in tiered if m["is_target"]]
        return (targeted[0] if targeted else tiered[0])["hs_company_id"]
    return eligible[0]["hs_company_id"]
