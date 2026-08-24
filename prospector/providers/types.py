"""Shared value types for enrichment providers.

Every provider adapter -- the FullEnrich adapter today, additional adapters
later -- speaks these types and nothing else, so the waterfall and the
/enrich route never
see a vendor payload shape. The vendor's raw response is preserved verbatim
in ContactResult.meta["raw_response"] for the attempts audit table, and
NOWHERE else: to_payload() (what the extension receives) deliberately
excludes it, because a raw vendor payload can carry fields we never vetted
for display (internal ids, other candidates, billing internals).

These interfaces are pinned -- other modules are coded against them
concurrently. Do not rename fields.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# The three enrichable fields, in canonical order. Matches the CHECK
# constraint on prospector.waterfalls.field exactly (sql/01_create_schema.sql)
# -- a field name that isn't in this tuple can't have a waterfall either.
FIELDS = ("work_email", "mobile", "personal_email")


@dataclass(frozen=True)
class EmailHit:
    address: str
    type: str  # 'work' | 'personal'
    status: str  # 'verified' | 'risky' | 'unknown' | 'inferred'
    provider: str
    cost_credits: float


@dataclass(frozen=True)
class PhoneHit:
    number: str
    type: str  # 'mobile' | 'direct' | 'hq'
    status: str
    dnc_flag: bool | None  # None = vendor did not say, NOT "not on DNC"
    provider: str
    cost_credits: float


@dataclass(frozen=True)
class EnrichInput:
    """Identity of the person to enrich.

    linkedin_url must already be in the canonical norm_linkedin form
    (prospector.matching) -- and NEVER a Sales Nav URL: Sales Nav URLs are
    unenrichable (by design) and must have been rejected at
    /recognize. Adapters keep a last-line defensive check anyway.
    """

    linkedin_url: str = ""
    first_name: str = ""
    last_name: str = ""
    company_domain: str = ""
    company_name: str = ""


@dataclass
class ContactResult:
    """What one provider returned for one person.

    meta carries {provider_id, request_id, latency_ms, raw_response}:
    request_id is the vendor's job/enrichment id (the handle for support
    tickets and billing disputes), raw_response the full vendor payload.
    raw_response goes to prospector.attempts for audit -- it is NOT part of
    to_payload(), which is what travels to the extension.
    """

    emails: list[EmailHit]
    phones: list[PhoneHit]
    profile: dict
    company: dict
    meta: dict

    def to_payload(self) -> dict:
        """JSON-safe dict for the extension. raw_response is excluded on
        purpose (see class docstring); everything else is plain
        dicts/lists/scalars."""
        return {
            "emails": [asdict(e) for e in self.emails],
            "phones": [asdict(p) for p in self.phones],
            "profile": self.profile,
            "company": self.company,
            "meta": {k: v for k, v in self.meta.items() if k != "raw_response"},
        }
