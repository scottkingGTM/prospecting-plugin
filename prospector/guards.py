"""Commit guards for the prospecting plugin: data in, verdict out.

Every function here is pure — no I/O, no HubSpot client. The writer calls
these before any commit and either refuses (blocking hold) or asks the rep
to confirm (non-blocking hold). Each guard encodes a specific failure mode
we hit while bulk-importing enriched contacts:

* **The job-changer weld.** Enrichment returns a person's CURRENT contact
  details, so someone who left the target account comes back carrying their
  new employer's email address. Comparing the email domain against the
  account domain is the only thing that catches it.

* **Auto-created junk companies.** A CRM setting that "automatically creates
  and associates companies" fires whenever a new contact's email domain
  doesn't match an existing company — so every sister-brand or off-domain
  address mints a junk company record. Holding back every row whose domain
  can't be checked keeps a whole import batch clean of auto-creates.

* **Benign alternate domains.** Many held domain mismatches turn out to be
  the same company on another corporate domain. So a mismatch is a HOLD for
  rep judgment, not an auto-discard — the rep can confirm the alternate
  domain.

* **Pattern-guessed addresses.** Some enrichment guesses an address at the
  WRONG company entirely — a namesake, a city government, a university — for
  someone who never worked there. Inference is for judgment, never identity
  — an 'inferred' email status is always a blocking hold, even when the
  domains happen to match.

* **"Business" emails are often personal.** A supposed business email is a
  personal address a large fraction of the time. Consumer-provider emails
  can never match a company and can't auto-create sensibly either — they
  belong in a personal-email property, not the work email field.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .filters import extract_domain
from .matching import norm_linkedin

# Consumer email providers (compared by registered domain, so a
# mail.comcast.net address is still consumer). An address at one of these
# can never identify an employer — a supposed "business email" turns out to
# be a personal address a large fraction of the time.
CONSUMER_EMAIL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "icloud.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "msn.com",
    "live.com",
    "me.com",
    "comcast.net",
    "att.net",
    "verizon.net",
})

# Public suffixes that occupy TWO labels, so the registered domain is the
# last THREE labels (kept small on purpose — the target universe is narrow;
# extend as real cases appear rather than vendoring a full public-suffix
# list).
_TWO_LABEL_PUBLIC_SUFFIXES: frozenset[str] = frozenset({
    "co.uk", "org.uk", "ac.uk",
    "com.au", "net.au", "org.au",
    "co.nz", "co.jp", "co.in",
    "com.br", "com.mx", "com.cn",
})

# The only phone property we write. By design we don't use mobilephone —
# everything goes to 'phone'.
ALLOWED_PHONE_PROPERTY = "phone"


@dataclass(frozen=True)
class GuardHold:
    """A guard verdict the writer must honor.

    blocking=True  -> commit must refuse outright.
    blocking=False -> commit may proceed only with an explicit rep
                      confirmation flag.
    """

    code: str          # machine-readable
    blocking: bool     # True = commit must refuse; False = needs rep confirmation flag
    message: str       # rep-facing plain language
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def _registered_domain(raw: str | None) -> str:
    """Collapse a host/URL to its registered domain, lowercased.

    careers.acmepest.com -> acmepest.com; https://www.acme.com/x -> acme.com.
    Subdomain-insensitive so a careers.acme.com email matches an acme.com
    company — a subdomain is not a different employer.
    """
    host = extract_domain(raw)
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _TWO_LABEL_PUBLIC_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _email_registered_domain(email: str) -> str:
    """Registered domain of an email address, '' if unparseable."""
    if "@" not in email:
        return ""
    return _registered_domain(email.rsplit("@", 1)[1])


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def email_domain_guard(
    email: str | None,
    company_domain: str | None,
    alternate_confirmed: bool,
) -> GuardHold | None:
    """The job-changer / auto-create-trap guard, one function.

    Failure history: an enriched contact arriving with their new employer's
    email (the job-changer weld), and junk companies the CRM auto-created
    from sister-brand domains. Because many real mismatches turn out to be
    the same company on an alternate corporate domain, a mismatch is a rep
    decision (discard, or confirm the alternate) rather than an auto-drop.

    An email whose domain can't be parsed at all is treated as a mismatch:
    it can never be verified against the company, and blocking is the safe
    side of that.
    """
    if email is None or not email.strip():
        # No email -> nothing to weld wrong, and HubSpot auto-create can't
        # fire without one. Committing email-less is allowed.
        return None

    email_dom = _email_registered_domain(email.strip())

    if email_dom in CONSUMER_EMAIL_DOMAINS:
        # Consumer provider: can never match a company, can't auto-create
        # sensibly. Non-blocking — the rep can keep it, but in the right
        # field (a supposed "business email" is personal a large share of
        # the time).
        return GuardHold(
            code="consumer_email",
            blocking=False,
            message=(
                f"{email_dom} is a personal email provider, so this address "
                "can't identify an employer. If you keep it, it belongs in a "
                "personal-email property — not the work email field."
            ),
            detail={"email_domain": email_dom},
        )

    company_dom = _registered_domain(company_domain)
    if not company_dom:
        # Email present but no company domain to check against — exactly
        # the population that mints junk companies when the CRM's auto-create
        # setting fires on every unmatched domain.
        return GuardHold(
            code="auto_create_trap",
            blocking=True,
            message=(
                "This contact has an email but no company domain to check it "
                "against. Committing would let HubSpot auto-create a company "
                "from the email domain (this exact setting minted 50 junk "
                "companies in July). Attach a company with a domain first."
            ),
            detail={"email_domain": email_dom, "company_domain": None},
        )

    if email_dom == company_dom:
        return None

    if alternate_confirmed:
        # Rep looked and ruled it a benign alternate corporate domain —
        # a large share of real mismatches are exactly that.
        return None

    return GuardHold(
        code="domain_mismatch",
        blocking=True,
        message=(
            f"The email domain ({email_dom}) does not match the company's "
            f"domain ({company_dom}). The person may have changed jobs, or "
            "the enrichment may have guessed the wrong company. Either "
            f"discard this contact, or confirm {email_dom} is an alternate "
            "domain of the same company."
        ),
        detail={"email_domain": email_dom, "company_domain": company_dom},
    )


def inferred_email_guard(email_status: str | None) -> GuardHold | None:
    """Block any email the provider only INFERRED.

    Pattern-guessing routines have invented plausible addresses at a city
    government and at a university for people who never worked there.
    Inference is for judgment, never identity — an inferred address is
    blocked regardless of whether its domain happens to match.
    """
    if email_status is None:
        return None
    if email_status.strip().lower() != "inferred":
        return None
    return GuardHold(
        code="inferred_email",
        blocking=True,
        message=(
            "This email address was inferred (pattern-guessed), not found "
            "or verified. Guessed addresses have landed on the wrong "
            "company entirely — including a city government and MIT — so "
            "we never commit an inferred email as someone's identity."
        ),
        detail={"email_status": email_status},
    )


def linkedin_link_guard(existing_url: str | None, new_url: str) -> GuardHold | None:
    """Guard the one-click hs_linkedin_url backfill.

    Overwriting a DIFFERENT existing profile link is the cross-source
    identity weld in miniature: the contact already points at a person, and
    a new, different URL most likely points at a different person.
    """
    existing_norm = norm_linkedin(existing_url)
    if not existing_norm:
        return None  # nothing linked yet -- safe to backfill
    new_norm = norm_linkedin(new_url)
    if existing_norm == new_norm:
        return None  # same profile, just spelled differently -- no-op is fine
    return GuardHold(
        code="linkedin_conflict",
        blocking=True,
        message=(
            "This contact already links a different LinkedIn profile "
            f"({existing_norm}, vs the new {new_norm}) — that usually means "
            "a different person. Not overwriting; resolve which profile is "
            "really theirs first."
        ),
        detail={"existing_url": existing_norm, "new_url": new_norm},
    )


def phone_field_guard(requested_property: str) -> GuardHold | None:
    """Only the 'phone' property is writable.

    By design we don't use mobilephone (or any other phone variant) — every
    number we write goes to 'phone'.
    """
    if requested_property == ALLOWED_PHONE_PROPERTY:
        return None
    return GuardHold(
        code="wrong_phone_property",
        blocking=True,
        message=(
            f"Phone numbers are only written to the '{ALLOWED_PHONE_PROPERTY}' "
            f"property — '{requested_property}' is not used (standing ruling: "
            "no mobilephone)."
        ),
        detail={"requested_property": requested_property},
    )


def collect_commit_holds(
    *,
    email: str | None,
    email_status: str | None,
    company_domain: str | None,
    alternate_confirmed: bool,
) -> list[GuardHold]:
    """Run the email guards in order and return every applicable hold.

    Inferred first: an inferred email is blocked regardless of domains — a
    guessed address can PASS a domain check at the right company, so the
    identity guard cannot hide behind the domain guard.
    """
    holds: list[GuardHold] = []
    hold = inferred_email_guard(email_status)
    if hold is not None:
        holds.append(hold)
    hold = email_domain_guard(email, company_domain, alternate_confirmed)
    if hold is not None:
        holds.append(hold)
    return holds
