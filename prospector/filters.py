# Adapted from a shared CRM-hygiene helper; keep behavior identical.
"""Filters: stage-substring exclusion, international detection, internal-label patterns.

All functions are pure — no side effects, no I/O. Easy to test.
"""
from __future__ import annotations

import re
from typing import Iterable

# Stage-like property names we check for an excluded substring across contacts/companies/deals
STAGE_PROPERTY_NAMES = {
    "lifecyclestage",
    "hs_lead_status",
    "dealstage",
    # Custom stage-like properties some CRMs use — extend here as discovered
    "lead_stage",
    "sales_stage",
    "pipeline_stage",
}


def has_excluded_stage(
    properties: dict[str, str | None],
    associated_deal_stages: Iterable[str] = (),
    exclude_substrings: Iterable[str] = (),
) -> bool:
    """Return True if any stage-like property contains an excluded substring."""
    excludes = [s.lower() for s in exclude_substrings if s]
    if not excludes:
        return False

    for prop_name, value in properties.items():
        if not value:
            continue
        if prop_name.lower() not in STAGE_PROPERTY_NAMES:
            continue
        v = str(value).lower()
        if any(sub in v for sub in excludes):
            return True

    for deal_stage in associated_deal_stages:
        if not deal_stage:
            continue
        v = str(deal_stage).lower()
        if any(sub in v for sub in excludes):
            return True

    return False


# ---------- Phone ----------

_PHONE_NON_DIGIT = re.compile(r"[^\d+]")


def normalize_phone(raw: str | None) -> str:
    """Strip whitespace and punctuation. Keep digits and leading +."""
    if not raw:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    # Keep + only if at position 0
    cleaned = _PHONE_NON_DIGIT.sub("", s)
    if s.startswith("+") and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


def is_international_phone(raw: str | None, domestic_country_codes: Iterable[str] = ("1",)) -> bool:
    """
    Determine if a phone is international.
    - Empty / missing phone is NOT international (just incomplete).
    - +1XXXXXXXXXX = domestic (US/CA)
    - 11 digits starting with 1 = domestic
    - 10 digits = treat as domestic US default
    - +<any other country code> = international
    - Anything else (long without + or unparseable) → assume international to be safe
    """
    normalized = normalize_phone(raw)
    if not normalized:
        return False  # no phone = not international (just missing data)

    domestic_codes = set(domestic_country_codes)

    if normalized.startswith("+"):
        # Extract country code (max 3 digits)
        digits = normalized[1:]
        # Try matching domestic codes by length, longest first
        for length in (3, 2, 1):
            if len(digits) >= length and digits[:length] in domestic_codes:
                return False
        return True

    # No +, just digits
    digits = normalized
    if len(digits) == 10:
        return False  # bare US format
    if len(digits) == 11 and digits[0] in domestic_codes:
        return False
    if len(digits) > 11:
        # Long number with no + — likely international with country code mashed in
        return True
    if len(digits) < 7:
        # Too short to be meaningful — treat as missing, not international
        return False
    # 7-10 digits with leading digit not in domestic codes (rare) — assume domestic
    return False


# ---------- Website / Domain ----------

_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?([^/\s?#]+)", re.IGNORECASE)


def extract_domain(raw: str | None) -> str:
    if not raw:
        return ""
    s = str(raw).strip().lower()
    if not s:
        return ""
    m = _DOMAIN_RE.match(s)
    if not m:
        return ""
    return m.group(1).rstrip(".")


def get_tld(domain: str) -> str:
    if not domain or "." not in domain:
        return ""
    # Handle multi-part TLDs by taking the last segment as the simple TLD
    # (we treat "co.uk" as .uk for the domestic check — international)
    return domain.rsplit(".", 1)[-1].lower()


def is_international_domain(raw: str | None, domestic_tlds: Iterable[str]) -> bool:
    """Empty domain = not international (just missing data)."""
    domain = extract_domain(raw)
    if not domain:
        return False
    tld = get_tld(domain)
    if not tld:
        return False
    return tld not in set(domestic_tlds)


# ---------- Country field ----------

def is_international_country(raw: str | None, domestic_values: Iterable[str]) -> bool:
    """Empty country = not international."""
    if not raw:
        return False
    v = str(raw).strip().lower()
    if not v:
        return False
    return v not in set(domestic_values)


# ---------- Combined ----------

def is_international_record(
    *,
    phone: str | None,
    website: str | None,
    domain: str | None,
    country: str | None,
    domestic_phone_country_codes: Iterable[str],
    domestic_tlds: Iterable[str],
    domestic_country_values: Iterable[str],
) -> bool:
    """A record is international if ANY signal indicates non-US/CA."""
    if is_international_phone(phone, domestic_phone_country_codes):
        return True
    # Check both website AND domain — companies often have one or the other
    if is_international_domain(website, domestic_tlds):
        return True
    if is_international_domain(domain, domestic_tlds):
        return True
    if is_international_country(country, domestic_country_values):
        return True
    return False


# ---------- Internal labels ----------

def is_internal_label(name: str | None, patterns: Iterable[str]) -> bool:
    """Check if a company name matches any internal franchise/segment label pattern."""
    if not name:
        return False
    s = str(name).strip()
    if not s:
        return False
    for pattern in patterns:
        try:
            if re.match(pattern, s):
                return True
        except re.error:
            continue
    return False
