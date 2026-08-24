# Adapted from a shared CRM-hygiene helper; keep behavior identical.
"""Duplicate matching + scoring for contacts and companies.

Pure functions — no I/O. Takes lists of dicts (records), returns lists of pair dicts.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from .filters import extract_domain, normalize_phone


# ---------- Normalization helpers ----------

def norm_email(raw: str | None) -> str:
    if not raw:
        return ""
    return str(raw).strip().lower()


def norm_linkedin(raw: str | None) -> str:
    """Normalize LinkedIn URL: lowercase, strip trailing slash, strip query, strip www/https."""
    if not raw:
        return ""
    s = str(raw).strip().lower()
    # Strip protocol
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    # Strip query string and trailing slash
    s = s.split("?")[0].split("#")[0].rstrip("/")
    return s


def norm_name(raw: str | None) -> str:
    if not raw:
        return ""
    return re.sub(r"\s+", " ", str(raw).strip().lower())


COMPANY_SUFFIXES = [
    " inc.", " inc", " llc", " llc.", " l.l.c.", " co.", " co", " ltd.", " ltd",
    " corporation", " corp.", " corp", " company", " group", " plc", " gmbh",
    " holdings", " holding", ", inc", ", llc", " p.c.", " pllc",
]


def norm_company_name(raw: str | None) -> str:
    if not raw:
        return ""
    s = norm_name(raw)
    if not s:
        return ""
    # Strip common suffixes (greedily, multiple times)
    changed = True
    while changed:
        changed = False
        for suffix in COMPANY_SUFFIXES:
            if s.endswith(suffix):
                s = s[: -len(suffix)].rstrip(" ,.")
                changed = True
    # Strip punctuation
    s = re.sub(r"[^\w\s]", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def email_local(email: str) -> str:
    if "@" not in email:
        return ""
    return email.split("@", 1)[0]


def email_domain(email: str) -> str:
    if "@" not in email:
        return ""
    return email.split("@", 1)[1]


# ---------- Levenshtein (small inline implementation) ----------

def levenshtein(a: str, b: str, max_dist: int | None = None) -> int:
    """Standard Levenshtein. Optional max_dist short-circuit returns max_dist+1 if exceeded."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if max_dist is not None and abs(len(a) - len(b)) > max_dist:
        return max_dist + 1

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        row_min = curr[0]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                prev[j] + 1,       # deletion
                curr[j - 1] + 1,   # insertion
                prev[j - 1] + cost  # substitution
            )
            if curr[j] < row_min:
                row_min = curr[j]
        prev = curr
        if max_dist is not None and row_min > max_dist:
            return max_dist + 1
    return prev[-1]


# ---------- Contact duplicate detection ----------

def find_contact_duplicates(contacts: list[dict]) -> list[dict]:
    """
    Returns a list of pair dicts:
      {
        "tier": "high"|"medium"|"low",
        "signal": "exact_email" | "linkedin" | "phone_lastname" | ...,
        "contact_a_id": str,
        "contact_b_id": str,
        "contact_a_label": str,
        "contact_b_label": str,
        "score": int,  # for ranking
      }
    Contacts already filtered (no excluded stage, no international) BEFORE this is called.
    """
    pairs: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    def add_pair(a: dict, b: dict, tier: str, signal: str, score: int) -> None:
        ids = tuple(sorted([str(a["id"]), str(b["id"])]))
        if ids in seen_pairs:
            return
        seen_pairs.add(ids)
        pairs.append({
            "tier": tier,
            "signal": signal,
            "score": score,
            "contact_a_id": ids[0],
            "contact_b_id": ids[1],
            "contact_a_label": _contact_label(a if str(a["id"]) == ids[0] else b),
            "contact_b_label": _contact_label(b if str(b["id"]) == ids[1] else a),
        })

    # --- HIGH: exact email ---
    by_email: dict[str, list[dict]] = defaultdict(list)
    for c in contacts:
        email = norm_email(c.get("properties", {}).get("email"))
        if email:
            by_email[email].append(c)
    for email, group in by_email.items():
        if len(group) < 2:
            continue
        # Pair each with each — typically just 2, occasionally more
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if _customer_lead_split_skip(a, b):
                    continue
                # Score: prefer pairs with more activity
                score = 1000 + _activity_score(a) + _activity_score(b)
                add_pair(a, b, "high", "exact_email", score)

    # --- HIGH: LinkedIn URL match ---
    by_linkedin: dict[str, list[dict]] = defaultdict(list)
    for c in contacts:
        li = norm_linkedin(c.get("properties", {}).get("hs_linkedin_url"))
        if li:
            by_linkedin[li].append(c)
    for li, group in by_linkedin.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if _customer_lead_split_skip(a, b):
                    continue
                score = 900 + _activity_score(a) + _activity_score(b)
                add_pair(a, b, "high", "linkedin_url", score)

    # --- HIGH: phone + lastname ---
    by_phone_last: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in contacts:
        phone = normalize_phone(c.get("properties", {}).get("phone"))
        lastname = norm_name(c.get("properties", {}).get("lastname"))
        if phone and lastname:
            by_phone_last[(phone, lastname)].append(c)
    for key, group in by_phone_last.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if _customer_lead_split_skip(a, b):
                    continue
                score = 850 + _activity_score(a) + _activity_score(b)
                add_pair(a, b, "high", "phone_lastname", score)

    # --- MEDIUM: lastname + email_local + company ---
    by_combo: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for c in contacts:
        props = c.get("properties", {})
        last = norm_name(props.get("lastname"))
        local = email_local(norm_email(props.get("email")))
        company = norm_name(props.get("company"))
        if last and local and company:
            by_combo[(last, local, company)].append(c)
    for key, group in by_combo.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if _customer_lead_split_skip(a, b):
                    continue
                # Don't re-add if already flagged as HIGH
                score = 500 + _activity_score(a) + _activity_score(b)
                add_pair(a, b, "medium", "lastname_emaillocal_company", score)

    # --- MEDIUM: firstname + lastname + company (no email overlap) ---
    by_name_co: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for c in contacts:
        props = c.get("properties", {})
        first = norm_name(props.get("firstname"))
        last = norm_name(props.get("lastname"))
        company = norm_name(props.get("company"))
        if first and last and company:
            by_name_co[(first, last, company)].append(c)
    for key, group in by_name_co.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                # Skip if they have the same email — handled by HIGH tier
                email_a = norm_email(a.get("properties", {}).get("email"))
                email_b = norm_email(b.get("properties", {}).get("email"))
                if email_a and email_b and email_a == email_b:
                    continue
                if _customer_lead_split_skip(a, b):
                    continue
                score = 450 + _activity_score(a) + _activity_score(b)
                add_pair(a, b, "medium", "firstname_lastname_company", score)

    return pairs


def _contact_label(c: dict) -> str:
    props = c.get("properties", {})
    first = (props.get("firstname") or "").strip()
    last = (props.get("lastname") or "").strip()
    name = f"{first} {last}".strip()
    if not name:
        name = (props.get("email") or "").strip() or f"id:{c['id']}"
    company = (props.get("company") or "").strip()
    suffix = f" @ {company}" if company else ""
    return f"{name}{suffix}"


def _activity_score(c: dict) -> int:
    props = c.get("properties", {})
    try:
        deals = int(props.get("num_associated_deals") or 0)
    except (ValueError, TypeError):
        deals = 0
    return deals * 10


def _customer_lead_split_skip(a: dict, b: dict) -> bool:
    """Skip if one is customer and other is lead and created >180 days apart."""
    from datetime import datetime, timezone
    pa = a.get("properties", {})
    pb = b.get("properties", {})
    stage_a = (pa.get("lifecyclestage") or "").lower()
    stage_b = (pb.get("lifecyclestage") or "").lower()
    if not ((stage_a == "customer" and stage_b == "lead") or (stage_b == "customer" and stage_a == "lead")):
        return False
    try:
        created_a = datetime.fromisoformat((pa.get("createdate") or "").replace("Z", "+00:00"))
        created_b = datetime.fromisoformat((pb.get("createdate") or "").replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return False
    delta_days = abs((created_a - created_b).days)
    return delta_days > 180


# ---------- Company duplicate detection ----------

def find_company_duplicates(companies: list[dict], max_fuzzy_distance: int = 3) -> list[dict]:
    """Returns a list of pair dicts for companies. Same shape as contacts."""
    pairs: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    def add_pair(a: dict, b: dict, tier: str, signal: str, score: int) -> None:
        ids = tuple(sorted([str(a["id"]), str(b["id"])]))
        if ids in seen_pairs:
            return
        seen_pairs.add(ids)
        pairs.append({
            "tier": tier,
            "signal": signal,
            "score": score,
            "company_a_id": ids[0],
            "company_b_id": ids[1],
            "company_a_label": _company_label(a if str(a["id"]) == ids[0] else b),
            "company_b_label": _company_label(b if str(b["id"]) == ids[1] else a),
        })

    # --- HIGH: same domain ---
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for co in companies:
        props = co.get("properties", {})
        domain = extract_domain(props.get("domain") or props.get("website"))
        if domain:
            by_domain[domain].append(co)
    for domain, group in by_domain.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                score = 1000 + _company_activity_score(a) + _company_activity_score(b)
                add_pair(a, b, "high", "exact_domain", score)

    # --- MEDIUM: same normalized name (must have at least one missing domain) ---
    by_name: dict[str, list[dict]] = defaultdict(list)
    for co in companies:
        props = co.get("properties", {})
        name = norm_company_name(props.get("name"))
        if name and len(name) > 2:
            by_name[name].append(co)
    for name, group in by_name.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                pa = a.get("properties", {})
                pb = b.get("properties", {})
                domain_a = extract_domain(pa.get("domain") or pa.get("website"))
                domain_b = extract_domain(pb.get("domain") or pb.get("website"))
                if domain_a and domain_b and domain_a == domain_b:
                    continue  # handled by HIGH
                # Surface as medium only if at least one is missing domain or they differ
                score = 500 + _company_activity_score(a) + _company_activity_score(b)
                add_pair(a, b, "medium", "same_name", score)

    # --- MEDIUM: same phone + fuzzy name match ---
    by_phone: dict[str, list[dict]] = defaultdict(list)
    for co in companies:
        phone = normalize_phone(co.get("properties", {}).get("phone"))
        if phone and len(phone) >= 7:
            by_phone[phone].append(co)
    for phone, group in by_phone.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                name_a = norm_company_name(a.get("properties", {}).get("name"))
                name_b = norm_company_name(b.get("properties", {}).get("name"))
                if not name_a or not name_b:
                    continue
                dist = levenshtein(name_a, name_b, max_dist=max_fuzzy_distance)
                if dist > max_fuzzy_distance:
                    continue
                score = 450 + _company_activity_score(a) + _company_activity_score(b)
                add_pair(a, b, "medium", "phone_fuzzy_name", score)

    return pairs


def _company_label(co: dict) -> str:
    props = co.get("properties", {})
    name = (props.get("name") or "").strip()
    domain = (props.get("domain") or "").strip()
    if not name:
        name = domain or f"id:{co['id']}"
    if domain and domain not in name:
        return f"{name} ({domain})"
    return name


def _company_activity_score(co: dict) -> int:
    props = co.get("properties", {})
    try:
        contacts = int(props.get("num_associated_contacts") or 0)
    except (ValueError, TypeError):
        contacts = 0
    try:
        deals = int(props.get("num_associated_deals") or 0)
    except (ValueError, TypeError):
        deals = 0
    return contacts * 5 + deals * 10


# ---------- Tier dedup: don't surface a pair as MEDIUM if it's already HIGH ----------

def dedupe_pairs_by_tier(pairs: list[dict], id_keys: tuple[str, str]) -> list[dict]:
    """If a pair appears at multiple tiers, keep only the highest."""
    tier_rank = {"high": 0, "medium": 1, "low": 2}
    best: dict[tuple[str, str], dict] = {}
    for p in pairs:
        key = tuple(sorted([p[id_keys[0]], p[id_keys[1]]]))
        if key not in best or tier_rank[p["tier"]] < tier_rank[best[key]["tier"]]:
            best[key] = p
    # Preserve original order for stability
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for p in pairs:
        key = tuple(sorted([p[id_keys[0]], p[id_keys[1]]]))
        if key in seen:
            continue
        seen.add(key)
        out.append(best[key])
    return out
