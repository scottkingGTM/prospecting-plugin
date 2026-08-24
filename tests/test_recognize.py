"""Unit tests for prospector.recognize -- classifier, cache, verdicts.

No live network or DB: the Database is replaced with a stub that emulates
just enough of prospector.recognize_cache (store on INSERT, expire, purge)
for the TTL behavior to be tested for real, and the HubSpot client is a
call-counting stub built against the agreed Phase-1 interface.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter

import pytest

from prospector.recognize import (
    RECOGNIZE_CACHE_TTL_S,
    Surface,
    classify_surface,
    parse_linkedin_title,
    recognize,
    slug_name_guess,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubDb:
    """Stands in for prospector.database.Database. Emulates the recognize
    cache table (keyed dict with real expiry) and returns canned rows for
    the account view and the reps table."""

    def __init__(self, account_rows=None, rep_rows=None):
        self.account_rows = account_rows or []
        self.rep_rows = rep_rows or []
        # cache_key -> (payload dict, expires_at epoch seconds)
        self.cache: dict[str, tuple[dict, float]] = {}
        self.queries: list[tuple[str, object]] = []
        self.executes: list[tuple[str, object]] = []

    def query(self, sql, params=None):
        self.queries.append((sql, params))
        if "recognize_cache" in sql:
            entry = self.cache.get(params["key"])
            if not entry:
                return []
            payload, expires_at = entry
            now = time.time()
            if expires_at <= now:
                return []
            age = max(0, int(params["ttl"] - (expires_at - now)))
            return [{"payload": payload, "cache_age_s": age}]
        if "v_account_360_mcp" in sql:
            return [dict(r) for r in self.account_rows]
        if "prospector.reps" in sql:
            return [dict(r) for r in self.rep_rows]
        raise AssertionError(f"unexpected query: {sql}")

    def execute(self, sql, params=None):
        self.executes.append((sql, params))
        if "INSERT INTO prospector.recognize_cache" in sql:
            self.cache[params["key"]] = (
                json.loads(params["payload"]),
                time.time() + params["ttl"],
            )
            return 1
        if "DELETE FROM prospector.recognize_cache" in sql:
            now = time.time()
            for key in [k for k, (_, exp) in self.cache.items() if exp < now]:
                del self.cache[key]
            return 0
        raise AssertionError(f"unexpected execute: {sql}")

    def expire(self, cache_key):
        """Force an entry past its TTL without waiting 24 hours."""
        payload, _ = self.cache[cache_key]
        self.cache[cache_key] = (payload, time.time() - 1)


class StubHubSpot:
    """Call-counting stub for the Phase-1 HubSpot client interface."""

    def __init__(self, contact=None, companies_by_domain=None, companies_by_slug=None,
                 owners=None, name_matches=None):
        self.contact = contact
        self.companies_by_domain = companies_by_domain or []
        self.companies_by_slug = companies_by_slug or []
        self.owners = owners or {}
        self.name_matches = name_matches or []
        self.calls: Counter = Counter()

    def find_contact_by_linkedin(self, norm_url):
        self.calls["find_contact_by_linkedin"] += 1
        self.last_contact_lookup = norm_url
        return dict(self.contact) if self.contact else None

    def find_contacts_by_name(self, first, last):
        self.calls["find_contacts_by_name"] += 1
        self.last_name_lookup = (first, last)
        return [dict(c) for c in self.name_matches]

    def find_companies_by_domain(self, domain):
        self.calls["find_companies_by_domain"] += 1
        self.last_domain_lookup = domain
        return [dict(c) for c in self.companies_by_domain]

    def find_company_by_linkedin_slug(self, slug):
        self.calls["find_company_by_linkedin_slug"] += 1
        self.last_slug_lookup = slug
        return [dict(c) for c in self.companies_by_slug]

    def get_owner(self, owner_id):
        self.calls["get_owner"] += 1
        return self.owners.get(str(owner_id))

    def contact_hubspot_url(self, contact_id):
        return f"https://app.hubspot.com/contacts/123/contact/{contact_id}"

    def company_hubspot_url(self, company_id):
        return f"https://app.hubspot.com/contacts/123/company/{company_id}"


def account_row(**overrides):
    """A benign v_account_360_mcp row: no gates fire unless a test asks."""
    row = {
        "hs_company_id": "9001",
        "name": "Acme Pest Control",
        "domain": "acmepest.com",
        "icp_tier": "Tier 2",
        "is_target_account": True,
        "lifecycle_stage": "lead",
        "owner_id": "111",
        "abm_stage": "S1",
        "employee_count": 250,
        "num_open_deals": 0,
        "total_deal_value": 0,
        "won_deals": 0,
        "lost_deals": 0,
        "last_deal_close_date": None,
        "is_current_client": False,
        "closed_lost_phase": None,
        "closed_lost_cooldown_days": None,
        "trend_cooling_off": False,
        "buying_committee_coverage": 40,
        "buying_committee_tiers": None,
        "coverage_components": {"ops": 1},
        "intl_has_intl_employees": False,
        "intl_overseas_confidence": None,
        "num_contacts": 3,
        "engaged_contacts_count": 1,
        "last_touch_at": "2026-08-10T00:00:00+00:00",
        "refreshed_at": "2026-08-18T07:00:00+00:00",
    }
    row.update(overrides)
    return row


CONTACT = {
    "id": "501",
    "firstname": "Jane",
    "lastname": "Doe",
    "email": "jane@acmepest.com",
    "jobtitle": "VP of Operations",
    "hubspot_owner_id": "111",
    "lifecyclestage": "lead",
    "lastmodifieddate": "2026-08-01T00:00:00Z",
    "associatedcompanyid": "9001",
}

COMPANY = {
    "id": "9001",
    "name": "Acme Pest Control",
    "domain": "acmepest.com",
    "hs_ideal_customer_profile": "tier_2",
    "hs_is_target_account": "true",
    "hubspot_owner_id": "111",
    "state": "TX",
    "linkedin_company_page": "https://www.linkedin.com/company/acme-pest",
}


# ---------------------------------------------------------------------------
# classify_surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,kind,key", [
    # linkedin profiles: query junk, trailing slash, uppercase all collapse
    ("https://www.linkedin.com/in/jane-doe/?utm_source=share&x=1",
     "linkedin_profile", "linkedin.com/in/jane-doe"),
    ("https://linkedin.com/in/jane-doe/",
     "linkedin_profile", "linkedin.com/in/jane-doe"),
    ("https://WWW.LinkedIn.com/in/Jane-Doe",
     "linkedin_profile", "linkedin.com/in/jane-doe"),
    ("https://m.linkedin.com/in/jane-doe",
     "linkedin_profile", "linkedin.com/in/jane-doe"),
    # sales nav: lead AND company pages are both dead ends
    ("https://www.linkedin.com/sales/lead/ACwAAAxYZ,NAME_SEARCH,abc",
     "sales_nav", ""),
    ("https://www.linkedin.com/sales/company/12345", "sales_nav", ""),
    # company page
    ("https://www.linkedin.com/company/Acme-Pest/about/",
     "linkedin_company", "linkedin.com/company/acme-pest"),
    # other linkedin surfaces idle
    ("https://www.linkedin.com/feed/", "linkedin_other", ""),
    ("https://www.linkedin.com/search/results/people/?keywords=ops",
     "linkedin_other", ""),
    ("https://www.linkedin.com/jobs/view/999", "linkedin_other", ""),
    # websites: registered-domain collapse
    ("https://www.acmepest.com/services?ref=google", "website", "acmepest.com"),
    ("https://careers.acmepest.com/openings", "website", "acmepest.com"),
    ("https://www.acmepest.co.uk/", "website", "acmepest.co.uk"),
    # denylist + unparseable -> ignored
    ("https://www.google.com/search?q=acme", "ignored", ""),
    ("https://mail.google.com/mail/u/0/", "ignored", ""),
    ("https://gmail.com", "ignored", ""),
    ("https://app.hubspot.com/contacts/123", "ignored", ""),
    ("http://localhost:3000/dev", "ignored", ""),
    ("http://127.0.0.1:8080/", "ignored", ""),
    ("chrome://extensions/", "ignored", ""),
    ("about:blank", "ignored", ""),
    ("total garbage not a url", "ignored", ""),
    ("", "ignored", ""),
    (None, "ignored", ""),
])
def test_classify_surface(url, kind, key):
    surface = classify_surface(url)
    assert surface.kind == kind
    assert surface.key == key


def test_mobile_and_www_profiles_share_one_key():
    """m. and www. must collapse to ONE identity -- the vendored
    norm_linkedin only strips www., so the host handling in
    classify_surface has to close the m. gap."""
    mobile = classify_surface("https://m.linkedin.com/in/Jane-Doe/")
    www = classify_surface("https://www.linkedin.com/in/jane-doe?utm=x")
    assert mobile.key == www.key == "linkedin.com/in/jane-doe"


def test_sales_nav_is_flagged_unenrichable():
    surface = classify_surface("https://www.linkedin.com/sales/lead/ACwAA,x,y")
    assert surface.kind == "sales_nav"
    assert surface.extra == {"enrichable": False}


def test_linkedin_company_extra_carries_bare_slug():
    surface = classify_surface("https://www.linkedin.com/company/Acme-Pest")
    assert surface.extra["slug"] == "acme-pest"


# ---------------------------------------------------------------------------
# cache behavior
# ---------------------------------------------------------------------------


PROFILE_SURFACE = Surface("linkedin_profile", key="linkedin.com/in/jane-doe")


def test_second_call_within_ttl_is_served_from_cache():
    db = StubDb(account_rows=[account_row()],
                rep_rows=[{"display_name": "Nick"}])
    hs = StubHubSpot(contact=CONTACT)

    first = recognize(db, hs, PROFILE_SURFACE)
    second = recognize(db, hs, PROFILE_SURFACE)

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["cache_age_s"] >= 0
    # THE point of the cache: the second call never touched HubSpot.
    assert hs.calls["find_contact_by_linkedin"] == 1
    # Same answer either way (minus the cache bookkeeping keys).
    strip = lambda d: {k: v for k, v in d.items() if k not in ("cached", "cache_age_s")}
    assert strip(first) == strip(second)
    assert "li:linkedin.com/in/jane-doe" in db.cache


def test_force_refresh_bypasses_a_live_cache_entry():
    db = StubDb(account_rows=[account_row()])
    hs = StubHubSpot(contact=CONTACT)

    recognize(db, hs, PROFILE_SURFACE)
    refreshed = recognize(db, hs, PROFILE_SURFACE, force_refresh=True)

    assert refreshed["cached"] is False
    assert hs.calls["find_contact_by_linkedin"] == 2


def test_expired_entry_is_refetched_and_purged():
    db = StubDb(account_rows=[account_row()])
    hs = StubHubSpot(contact=CONTACT)

    recognize(db, hs, PROFILE_SURFACE)
    db.expire("li:linkedin.com/in/jane-doe")
    result = recognize(db, hs, PROFILE_SURFACE)

    assert result["cached"] is False
    assert hs.calls["find_contact_by_linkedin"] == 2
    # The miss path ran the opportunistic purge before re-inserting.
    assert any("DELETE FROM prospector.recognize_cache" in sql
               for sql, _ in db.executes)


def test_website_cache_key_uses_dom_prefix():
    db = StubDb(account_rows=[account_row()])
    hs = StubHubSpot(companies_by_domain=[COMPANY])
    recognize(db, hs, Surface("website", key="acmepest.com"))
    assert "dom:acmepest.com" in db.cache


def test_idle_surfaces_never_touch_cache_or_hubspot():
    db = StubDb()
    hs = StubHubSpot()
    for kind in ("linkedin_other", "ignored"):
        result = recognize(db, hs, Surface(kind))
        assert result["verdict"] == "idle"
    assert db.queries == [] and db.executes == []
    assert sum(hs.calls.values()) == 0


def test_single_flight_two_racing_threads_cost_one_live_lookup():
    """Two threads miss on the SAME key at once: the winner does the live
    lookup, the loser waits on the per-key lock, re-checks the cache, and
    is served the winner's payload -- exactly ONE HubSpot call."""

    class GatedHubSpot(StubHubSpot):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.entered = threading.Event()   # set when a live lookup starts
            self.release = threading.Event()   # test opens the gate

        def find_contact_by_linkedin(self, norm_url):
            self.entered.set()
            assert self.release.wait(timeout=5), "gate never opened"
            return super().find_contact_by_linkedin(norm_url)

    db = StubDb(account_rows=[account_row()],
                rep_rows=[{"display_name": "Nick"}])
    hs = GatedHubSpot(contact=CONTACT)
    results: dict[str, dict] = {}

    def call(tag):
        results[tag] = recognize(db, hs, PROFILE_SURFACE)

    winner = threading.Thread(target=call, args=("winner",))
    winner.start()
    assert hs.entered.wait(timeout=5)  # winner is inside the live lookup
    loser = threading.Thread(target=call, args=("loser",))
    loser.start()
    time.sleep(0.1)  # let the loser park on the key lock
    hs.release.set()
    winner.join(timeout=5)
    loser.join(timeout=5)
    assert not winner.is_alive() and not loser.is_alive()

    assert hs.calls["find_contact_by_linkedin"] == 1
    assert results["winner"]["cached"] is False
    assert results["loser"]["cached"] is True
    strip = lambda d: {k: v for k, v in d.items()
                       if k not in ("cached", "cache_age_s")}
    assert strip(results["winner"]) == strip(results["loser"])

    # The per-key lock dict was pruned after both holders left.
    from prospector import recognize as recognize_mod
    assert recognize_mod._key_locks == {}


def test_cache_write_failure_still_returns_fresh_payload():
    """A dead cache table degrades to 'no caching', never to a 500: the
    fresh payload still comes back whole."""

    class CachePutFailsDb(StubDb):
        def execute(self, sql, params=None):
            if "INSERT INTO prospector.recognize_cache" in sql:
                raise RuntimeError("cache table is on fire")
            return super().execute(sql, params)

    db = CachePutFailsDb(account_rows=[account_row()],
                         rep_rows=[{"display_name": "Nick"}])
    hs = StubHubSpot(contact=CONTACT)

    result = recognize(db, hs, PROFILE_SURFACE)

    assert result["verdict"] == "green"
    assert result["cached"] is False
    assert result["contact"]["name"] == "Jane Doe"
    # Nothing was cached, so a second call does a second live lookup.
    recognize(db, hs, PROFILE_SURFACE)
    assert hs.calls["find_contact_by_linkedin"] == 2


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------


def test_contact_hit_is_green_with_hubspot_link():
    db = StubDb(account_rows=[account_row()],
                rep_rows=[{"display_name": "Nick"}])
    hs = StubHubSpot(
        contact=CONTACT,
        owners={"111": {"id": "111", "firstName": "Nick",
                        "lastName": "Rep", "email": "nick@example.com"}},
    )

    result = recognize(db, hs, PROFILE_SURFACE)

    assert result["verdict"] == "green"
    assert result["contact"]["name"] == "Jane Doe"
    assert result["contact"]["hubspot_url"].endswith("/contact/501")
    assert result["contact"]["owner_name"] == "Nick Rep"
    # No account context in the payload any more.
    assert "account" not in result


def test_unknown_profile_is_red_phase1():
    """Phase 1 has no company for a bare profile, so unknown == red;
    Phase 3's resolve is expected to upgrade this to yellow when the
    person's company IS a known account."""
    db = StubDb()
    hs = StubHubSpot(contact=None)
    result = recognize(db, hs, PROFILE_SURFACE)
    assert result["verdict"] == "red"
    assert result["contact"] is None
    assert "account" not in result


def test_multiple_contact_matches_flag_passes_through():
    db = StubDb(account_rows=[account_row()])
    hs = StubHubSpot(contact={**CONTACT, "_multiple_matches": True})
    result = recognize(db, hs, PROFILE_SURFACE)
    assert result["multiple_matches"] is True


def test_unknown_domain_is_red():
    db = StubDb()
    hs = StubHubSpot(companies_by_domain=[])
    result = recognize(db, hs, Surface("website", key="nobodyknows.com"))
    assert result["verdict"] == "red"
    assert result["company_matches"] == []
    assert result["merge_candidate"] is False
    assert "account" not in result


def test_multi_match_prefers_tiered_company_and_flags_merge():
    tierless = {**COMPANY, "id": "8000", "hs_ideal_customer_profile": None}
    tiered = {**COMPANY, "id": "9001"}
    db = StubDb(account_rows=[account_row()])
    # Tierless twin FIRST: preference must come from the tier, not the order.
    hs = StubHubSpot(companies_by_domain=[tierless, tiered])

    result = recognize(db, hs, Surface("website", key="acmepest.com"))

    assert result["verdict"] == "green"
    assert len(result["company_matches"]) == 2
    assert result["preferred_company_id"] == "9001"
    assert result["merge_candidate"] is True


def test_linkedin_company_surface_looks_up_by_slug():
    db = StubDb(account_rows=[account_row()])
    hs = StubHubSpot(companies_by_slug=[COMPANY])
    surface = classify_surface("https://www.linkedin.com/company/Acme-Pest/")
    result = recognize(db, hs, surface)
    assert hs.last_slug_lookup == "acme-pest"
    assert result["verdict"] == "green"
    assert result["merge_candidate"] is False


def test_sales_nav_returns_unsupported_surface():
    db = StubDb()
    hs = StubHubSpot()
    result = recognize(db, hs, Surface("sales_nav", extra={"enrichable": False}))
    assert result["verdict"] == "unsupported_surface"
    assert "public LinkedIn profile" in result["message"]
    assert sum(hs.calls.values()) == 0  # no lookups on a dead-end surface


# ---------------------------------------------------------------------------
# possible matches (URL miss -> name-guess candidates)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,expected", [
    ("dana-ops", ("dana", "ops")),
    # trailing LinkedIn id junk (digits, letter+digit hashes) is dropped
    ("dana-ops-1b2a3f", ("dana", "ops")),
    ("jane-doe-123456789", ("jane", "doe")),
    # suffix noise is dropped BEFORE picking the last token, so the surname
    # is smith, not jr
    ("john-smith-jr-8a49b23", ("john", "smith")),
    ("jane-doe-mba", ("jane", "doe")),
    # middle tokens are ignored: first + LAST remaining token
    ("mary-beth-johnson", ("mary", "johnson")),
    # unsplittable or initial-only slugs carry no derivable name
    ("johnsmith", None),
    ("j-smith", None),
    ("smith", None),
    ("", None),
    # all-noise slugs collapse to nothing
    ("jr-md", None),
    # unicode is preserved as-is (HubSpot search is case/accent tolerant
    # enough; names_agree strips accents on comparison)
    ("maría-garcía", ("maría", "garcía")),
    # a short mixed token (<5 chars) is NOT id junk -- kept as a name part
    ("al-b2b-jones", ("al", "jones")),
])
def test_slug_name_guess(slug, expected):
    assert slug_name_guess(slug) == expected


def test_url_miss_surfaces_only_unlinked_name_agreeing_candidates():
    """End-to-end: william-smith matches no contact by URL; the name search
    returns a linked contact (dropped -- provably another person or already
    linked), an unlinked Billy Smith (kept via the bill/william nickname
    group), and an unlinked wrong-name (dropped by names_agree). Verdict
    stays red; the card carries email_domain ONLY, never the address."""
    db = StubDb()
    hs = StubHubSpot(
        contact=None,
        name_matches=[
            {"id": "801", "firstname": "William", "lastname": "Smith",
             "email": "will@linkedco.com", "jobtitle": "CEO",
             "lifecyclestage": "customer", "hubspot_owner_id": "111",
             "hs_linkedin_url": "https://www.linkedin.com/in/other-william"},
            {"id": "802", "firstname": "Billy", "lastname": "Smith",
             "email": "Billy@AcmePest.com", "jobtitle": "Ops Manager",
             "lifecyclestage": "lead", "hubspot_owner_id": "111",
             "hs_linkedin_url": None},
            {"id": "803", "firstname": "Wilma", "lastname": "Smith",
             "email": "wilma@elsewhere.com", "jobtitle": "Controller",
             "lifecyclestage": "lead", "hubspot_owner_id": None,
             "hs_linkedin_url": ""},
        ],
        owners={"111": {"id": "111", "firstName": "Nick",
                        "lastName": "Rep", "email": "nick@example.com"}},
    )
    surface = Surface("linkedin_profile", key="linkedin.com/in/william-smith")

    result = recognize(db, hs, surface)

    assert result["verdict"] == "red"  # a hint, never a match
    assert hs.last_name_lookup == ("william", "smith")
    (match,) = result["possible_matches"]
    assert match == {
        "hs_contact_id": "802",
        "name": "Billy Smith",
        "jobtitle": "Ops Manager",
        "email_domain": "acmepest.com",
        "lifecycle_stage": "lead",
        "owner_name": "Nick Rep",
        "hubspot_url": "https://app.hubspot.com/contacts/123/contact/802",
    }
    assert "email" not in match  # domain only -- the local part is PII noise
    assert result["possible_match_note"] == (
        "matched by name from the profile URL — needs your eyes, "
        "never auto-linked"
    )

    # Cacheable like the rest of the payload: the second call is served
    # from cache, possible_matches intact, no second name search.
    again = recognize(db, hs, surface)
    assert again["cached"] is True
    assert again["possible_matches"] == result["possible_matches"]
    assert hs.calls["find_contacts_by_name"] == 1


def test_possible_matches_cap_at_three_in_returned_order():
    def unlinked(i):
        return {"id": str(i), "firstname": "Dana", "lastname": "Ops",
                "email": f"dana{i}@x{i}.com", "jobtitle": None,
                "lifecyclestage": None, "hubspot_owner_id": None,
                "hs_linkedin_url": None}

    db = StubDb()
    hs = StubHubSpot(contact=None,
                     name_matches=[unlinked(i) for i in range(1, 6)])
    result = recognize(db, hs, Surface("linkedin_profile",
                                       key="linkedin.com/in/dana-ops"))
    assert [m["hs_contact_id"] for m in result["possible_matches"]] == [
        "1", "2", "3",
    ]


def test_underivable_slug_skips_the_name_search_entirely():
    db = StubDb()
    hs = StubHubSpot(contact=None)
    result = recognize(db, hs, Surface("linkedin_profile",
                                       key="linkedin.com/in/johnsmith"))
    assert result["verdict"] == "red"
    assert result["possible_matches"] == []
    assert hs.calls["find_contacts_by_name"] == 0


def test_green_profile_carries_no_possible_match_keys():
    db = StubDb(account_rows=[account_row()],
                rep_rows=[{"display_name": "Nick"}])
    hs = StubHubSpot(contact=CONTACT)
    result = recognize(db, hs, PROFILE_SURFACE)
    assert result["verdict"] == "green"
    assert "possible_matches" not in result
    assert "possible_match_note" not in result
    assert hs.calls["find_contacts_by_name"] == 0


# ---------------------------------------------------------------------------
# tab-title name hint (the mangled-slug autofill case)
# ---------------------------------------------------------------------------


PROFILE = "linkedin_profile"
COMPANY_KIND = "linkedin_company"

JANE = {"full_name": "Jane Doe", "first_name": "Jane", "last_name": "Doe"}


@pytest.mark.parametrize("title,kind,expected", [
    # plain name, with and without headline chrome
    ("Jane Doe | LinkedIn", PROFILE, JANE),
    ("Jane Doe - VP of Operations at Acme | LinkedIn", PROFILE, JANE),
    ("Jane Doe | VP of Operations at Acme | LinkedIn", PROFILE, JANE),
    # notification counter prefix
    ("(2) Jane Doe | LinkedIn", PROFILE, JANE),
    # non-breaking space around the pipe (LinkedIn does this)
    ("Jane Doe\u00a0|\u00a0LinkedIn", PROFILE, JANE),
    # credentials stripped FIRST (strip_trailing_credentials), honorific too
    ("Dr. Jane Doe, MBA - Chief of Everything | LinkedIn", PROFILE, JANE),
    # middle tokens ignored: first token = first, LAST token = last
    ("Mary Beth Johnson | LinkedIn", PROFILE,
     {"full_name": "Mary Beth Johnson", "first_name": "Mary",
      "last_name": "Johnson"}),
    # THE case: the title carries the real name the slug mangled
    ("Alex Palmer - Operations Leader | LinkedIn", PROFILE,
     {"full_name": "Alex Palmer", "first_name": "Alex", "last_name": "Palmer"}),
    # single token: full_name only, first/last EMPTY (empty invites a fix)
    ("Cher | LinkedIn", PROFILE,
     {"full_name": "Cher", "first_name": "", "last_name": ""}),
    # generic LinkedIn chrome is not a name
    ("LinkedIn", PROFILE, {}),
    ("Feed | LinkedIn", PROFILE, {}),
    ("Sign Up | LinkedIn", PROFILE, {}),
    ("(3) Notifications | LinkedIn", PROFILE, {}),
    # empty / None
    ("", PROFILE, {}),
    (None, PROFILE, {}),
    # company page titles
    ("Acme Pest Control | LinkedIn", COMPANY_KIND,
     {"company_name": "Acme Pest Control"}),
    ("Acme Pest Control - Overview | LinkedIn", COMPANY_KIND,
     {"company_name": "Acme Pest Control"}),
    ("LinkedIn", COMPANY_KIND, {}),
    # unknown kind never yields a person hint
    ("Jane Doe | LinkedIn", "website", {}),
])
def test_parse_linkedin_title(title, kind, expected):
    assert parse_linkedin_title(title, kind) == expected


def test_parse_linkedin_title_is_safe_on_oversized_input():
    """The extension truncates to 300 chars and the server ignores longer,
    but the parser itself must stay safe on ANY length."""
    huge = "A" * 400 + " | LinkedIn"
    result = parse_linkedin_title(huge, PROFILE)
    assert result == {"full_name": "A" * 400, "first_name": "", "last_name": ""}
    assert parse_linkedin_title("x" * 5000, PROFILE) == {
        "full_name": "x" * 5000, "first_name": "", "last_name": ""}


ALEX_UNLINKED = {
    "id": "901", "firstname": "Alex", "lastname": "Palmer",
    "email": "alex@globexops.com", "jobtitle": "COO",
    "lifecyclestage": "lead", "hubspot_owner_id": None,
    "hs_linkedin_url": None,
}

ALEX_SURFACE = Surface("linkedin_profile", key="linkedin.com/in/alexpalmer-sf")
ALEX_TITLE = "Alex Palmer - Operations Leader | LinkedIn"


def test_title_derived_name_beats_slug_guess_for_possible_matches():
    """alexpalmer-sf slug-guesses ('alexpalmer', 'sf') -- the bad
    autofill. With the tab title in hand the search runs on the REAL name,
    the candidate passes names_agree, and the payload carries name_hint."""
    db = StubDb()
    hs = StubHubSpot(contact=None, name_matches=[ALEX_UNLINKED])

    result = recognize(db, hs, ALEX_SURFACE, page_title=ALEX_TITLE)

    assert hs.last_name_lookup == ("Alex", "Palmer")
    assert result["name_hint"] == {
        "full_name": "Alex Palmer", "first_name": "Alex", "last_name": "Palmer",
    }
    assert [m["hs_contact_id"] for m in result["possible_matches"]] == ["901"]
    assert result["verdict"] == "red"  # still a hint, never a match


def test_no_title_still_falls_back_to_slug_guess():
    """Without a title (or with a generic one) the slug guess still drives
    the search -- the slug stays the fallback, not a removed feature."""
    for title in (None, "LinkedIn", "Feed | LinkedIn"):
        db = StubDb()
        hs = StubHubSpot(contact=None, name_matches=[ALEX_UNLINKED])
        result = recognize(db, hs, ALEX_SURFACE, page_title=title)
        # Slug guess ('alexpalmer', 'sf') ran the search; names_agree then
        # rightly rejected Alex Palmer against 'alexpalmer sf'.
        assert hs.last_name_lookup == ("alexpalmer", "sf")
        assert result["possible_matches"] == []
        assert "name_hint" not in result


def test_cached_hit_gains_name_hint_without_contact_relookup():
    """The cache-freshness rule: a payload cached WITHOUT a hint (title not
    available yet) must gain the hint when a later request carries one --
    recomputing possible-matches only, never the contact/account lookups."""
    db = StubDb()
    hs = StubHubSpot(contact=None, name_matches=[ALEX_UNLINKED])

    first = recognize(db, hs, ALEX_SURFACE)  # no title -> no hint cached
    assert "name_hint" not in first
    assert first["possible_matches"] == []

    second = recognize(db, hs, ALEX_SURFACE, page_title=ALEX_TITLE)
    assert second["cached"] is True
    assert second["name_hint"]["first_name"] == "Alex"
    assert [m["hs_contact_id"] for m in second["possible_matches"]] == ["901"]
    # Contact lookup NOT re-run; only the name search re-ran (slug try +
    # title try).
    assert hs.calls["find_contact_by_linkedin"] == 1
    assert hs.calls["find_contacts_by_name"] == 2

    # The refreshed payload was written back: a third call with the same
    # title is a plain hit -- no third name search.
    third = recognize(db, hs, ALEX_SURFACE, page_title=ALEX_TITLE)
    assert third["cached"] is True
    assert third["name_hint"]["first_name"] == "Alex"
    assert hs.calls["find_contacts_by_name"] == 2


def test_cached_green_hit_gains_hint_without_any_lookup():
    """Green payloads have no possible_matches to recompute: the refresh
    adds the hint and touches HubSpot not at all."""
    db = StubDb(account_rows=[account_row()],
                rep_rows=[{"display_name": "Nick"}])
    hs = StubHubSpot(contact=CONTACT)

    recognize(db, hs, PROFILE_SURFACE)  # cached, hint-less
    calls_before = sum(hs.calls.values())
    result = recognize(db, hs, PROFILE_SURFACE,
                       page_title="Jane Doe - VP Ops | LinkedIn")

    assert result["cached"] is True
    assert result["verdict"] == "green"
    assert result["name_hint"] == JANE
    assert sum(hs.calls.values()) == calls_before  # zero new lookups


def test_fresh_profile_with_title_carries_hint_even_when_green():
    db = StubDb(account_rows=[account_row()],
                rep_rows=[{"display_name": "Nick"}])
    hs = StubHubSpot(contact=CONTACT)
    result = recognize(db, hs, PROFILE_SURFACE,
                       page_title="Jane Doe | LinkedIn")
    assert result["cached"] is False
    assert result["verdict"] == "green"
    assert result["name_hint"] == JANE


def test_company_surface_title_becomes_company_name_hint():
    db = StubDb(account_rows=[account_row()])
    hs = StubHubSpot(companies_by_slug=[COMPANY])
    surface = classify_surface("https://www.linkedin.com/company/Acme-Pest/")
    result = recognize(db, hs, surface,
                       page_title="Acme Pest Control | LinkedIn")
    assert result["name_hint"] == {"company_name": "Acme Pest Control"}
    # No lookup change: still the same slug lookup as without a title.
    assert hs.last_slug_lookup == "acme-pest"


def test_single_token_title_hint_does_not_drive_name_search():
    """A single-token title has no first/last split -- the search falls
    back to the slug guess; the hint (full_name only) still rides along."""
    db = StubDb()
    hs = StubHubSpot(contact=None, name_matches=[ALEX_UNLINKED])
    result = recognize(db, hs, ALEX_SURFACE, page_title="Cher | LinkedIn")
    assert hs.last_name_lookup == ("alexpalmer", "sf")  # slug fallback
    assert result["name_hint"] == {
        "full_name": "Cher", "first_name": "", "last_name": ""}


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


def test_ttl_constant_defaults_to_24_hours():
    assert RECOGNIZE_CACHE_TTL_S == 24 * 3600
