"""Recognize engine: what is the rep looking at, and do we already have it?

Two jobs, deliberately separated:

  classify_surface(url)  -- pure, no I/O: buckets a browser URL into one of
      {linkedin_profile, sales_nav, linkedin_company, linkedin_other,
       website, ignored} and produces the canonical lookup key.
  recognize(db, hubspot, surface)  -- I/O: answers "do we already know this
      person/company?" from HubSpot, behind a short-TTL Postgres cache
      (prospector.recognize_cache) so a rep flipping between the same two
      tabs costs one lookup, not twenty.

parse_linkedin_title (pure) turns the browser tab TITLE into a name hint.
The extension deliberately runs no content scripts, so the tab title --
plain metadata the `tabs` permission already exposes, same risk posture as
the URL -- is the only place the person's real display name is visible to
us. Without it the slug is the sole name source, and a slug with a trailing
location suffix (e.g. 'jane-doe-sf') would autofill a garbled first/last
name while the real name sits in the tab title.

Verdict semantics:
  green  -- the thing on screen is already in the CRM (contact found for a
            profile surface; company found for a website/company surface).
  red    -- not in the CRM.

The HubSpot client is injected (constructor-style), never imported: this
module codes against the agreed interface only (find_contact_by_linkedin,
find_contacts_by_name, find_companies_by_domain,
find_company_by_linkedin_slug, get_owner, contact_hubspot_url,
company_hubspot_url).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import unquote, urlsplit

from .filters import extract_domain
from .matching import norm_linkedin
from .nicknames import names_agree, strip_trailing_credentials

if TYPE_CHECKING:
    from .database import Database

logger = logging.getLogger(__name__)

# Cache TTL. Env-tunable (RECOGNIZE_CACHE_TTL_S) but read ONCE at import --
# it is an operational knob, not a per-request parameter.
RECOGNIZE_CACHE_TTL_S = int(os.getenv("RECOGNIZE_CACHE_TTL_S", str(24 * 3600)))


# ---------------------------------------------------------------------------
# Surface classification (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Surface:
    """One classified browser surface. `key` is the canonical lookup/cache
    identity (normalized LinkedIn path or registered domain); `extra` carries
    kind-specific details (e.g. the bare company slug)."""

    kind: str
    key: str = ""
    extra: dict = field(default_factory=dict)


# Registered domains the panel must never treat as a prospect's website:
# search/social/consumer surfaces, our own tooling (HubSpot, Supabase,
# Railway, Slack, Zoom, Notion), and webmail. Hitting HubSpot's company
# search with "google.com" would only ever return junk matches.
_DENYLIST: frozenset[str] = frozenset({
    "google.com",
    "gmail.com",
    "googlemail.com",
    "youtube.com",
    "facebook.com",
    "fb.com",
    "x.com",
    "twitter.com",
    "instagram.com",
    "reddit.com",
    "github.com",
    "hubspot.com",
    "supabase.com",
    "supabase.co",
    "railway.app",
    "railway.com",
    "slack.com",
    "zoom.us",
    "notion.so",
    "notion.com",
})

# google.* exists under dozens of ccTLDs (google.co.uk, google.ca, ...) and
# docs./calendar./mail. all collapse to it -- catch the brand, not just .com.
_DENYLISTED_BRANDS: frozenset[str] = frozenset({"google"})

# Host prefixes that mean "internal tool / webmail / calendar", whatever the
# domain: app.hubspot.com, mail.acme.com, calendar.google.com, ...
_IGNORED_HOST_PREFIXES = ("app.", "mail.", "calendar.")

# Common second-level labels under 2-letter ccTLDs (acmepest.co.uk must
# register as acmepest.co.uk, not co.uk). A full public-suffix list is
# overkill for a panel that mostly sees US home-services sites.
_CC_SLDS: frozenset[str] = frozenset({"co", "com", "net", "org", "ac", "gov", "edu"})

_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.\-]*):")


def classify_surface(url: str | None) -> Surface:
    """Bucket a raw browser URL. Pure -- no I/O, safe to call per keystroke.

    Empty/None/garbage/denylisted/non-http all land on kind='ignored', which
    recognize() turns into an idle panel rather than an error.
    """
    if not url or not isinstance(url, str):
        return Surface("ignored")
    raw = url.strip()
    if not raw:
        return Surface("ignored")

    # Non-http schemes first (chrome://, about:, file:, mailto:): these are
    # prefix-shaped, not domain-shaped, so they get their own check before
    # any domain parsing.
    scheme_match = _SCHEME_RE.match(raw.lower())
    if scheme_match and scheme_match.group(1) not in ("http", "https"):
        return Surface("ignored")

    # extract_domain lowercases and strips protocol + www. -- exactly the
    # host normalization norm_linkedin does NOT do for us (see below).
    host = extract_domain(raw).split(":", 1)[0]
    if not host:
        return Surface("ignored")
    if host == "localhost" or host.endswith(".localhost") or host.startswith("127."):
        return Surface("ignored")

    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        path = urlsplit(raw if "://" in raw else f"https://{raw}").path
        segments = [unquote(seg) for seg in path.split("/") if seg]
        return _classify_linkedin(segments)

    if any(host.startswith(prefix) for prefix in _IGNORED_HOST_PREFIXES):
        return Surface("ignored")
    registered = _registered_domain(host)
    if not registered:
        return Surface("ignored")
    if registered in _DENYLIST or registered.split(".", 1)[0] in _DENYLISTED_BRANDS:
        return Surface("ignored")
    return Surface("website", key=registered)


def _classify_linkedin(segments: list[str]) -> Surface:
    """Classify a linkedin.com path (host already verified + normalized)."""
    if not segments:
        return Surface("linkedin_other")
    head = segments[0].lower()

    if head == "sales":
        # Sales Navigator is a recognized DEAD END, not an error: FullEnrich
        # rejects /sales/ URLs outright -- a large share of contacts sourced
        # this way get dropped for exactly this. The panel must tell the rep
        # to open the public /in/ profile, never pretend it can enrich here.
        return Surface("sales_nav", extra={"enrichable": False})

    if head == "in" and len(segments) >= 2 and segments[1].strip():
        # Host normalization happens HERE, before norm_linkedin: the vendored
        # norm_linkedin strips www. but NOT m., so a mobile URL would mint a
        # second cache/dedupe identity for the same person if we passed the
        # raw host through. Rebuilding on the bare apex collapses www./m./
        # any future subdomain to one canonical key.
        slug = segments[1].strip()
        return Surface("linkedin_profile", key=norm_linkedin(f"linkedin.com/in/{slug}"))

    if head == "company" and len(segments) >= 2 and segments[1].strip():
        slug = segments[1].strip().lower()
        return Surface(
            "linkedin_company",
            key=f"linkedin.com/company/{slug}",
            extra={"slug": slug},
        )

    # Feed, search, jobs, notifications, a bare /in/ ... nothing actionable.
    return Surface("linkedin_other")


def _registered_domain(host: str) -> str:
    """Collapse a host to its registered domain (careers.acmepest.com ->
    acmepest.com; acmepest.co.uk stays acmepest.co.uk). Returns '' for
    anything that is not a plausible domain (no dot, numeric TLD/IP)."""
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return ""
    tld = labels[-1]
    if not re.fullmatch(r"[a-z]{2,}", tld):
        return ""  # bare IPs (192.168.x.x) and junk like acme.123
    if len(labels) >= 3 and len(tld) == 2 and labels[-2] in _CC_SLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# ---------------------------------------------------------------------------
# Recognize (I/O)
# ---------------------------------------------------------------------------

# cache_age_s is derived from expires_at (the only timestamp stored):
# age = TTL - seconds_remaining. GREATEST(0, ...) guards a TTL that was
# lowered between writes.
_CACHE_GET_SQL = """
SELECT payload,
       GREATEST(0, %(ttl)s - CEIL(EXTRACT(EPOCH FROM (expires_at - now()))))::int
           AS cache_age_s
FROM prospector.recognize_cache
WHERE cache_key = %(key)s
  AND expires_at > now()
"""

_CACHE_PUT_SQL = """
INSERT INTO prospector.recognize_cache (cache_key, payload, expires_at)
VALUES (%(key)s, %(payload)s::jsonb, now() + %(ttl)s * interval '1 second')
ON CONFLICT (cache_key) DO UPDATE
    SET payload = EXCLUDED.payload,
        expires_at = EXCLUDED.expires_at
"""

_CACHE_PURGE_SQL = "DELETE FROM prospector.recognize_cache WHERE expires_at < now()"

# Single-flight machinery: one Lock per in-flight cache key so two requests
# racing the same miss (two tabs, one flapping tab) cost ONE set of live
# lookups -- the loser waits, re-checks the cache, and is served the
# winner's payload. Entries are refcounted under the meta-lock and pruned
# when the last holder leaves, so the dict never grows past the number of
# keys actually in flight.
_key_locks: dict[str, list] = {}  # cache_key -> [threading.Lock, holder_count]
_key_locks_meta = threading.Lock()


@contextmanager
def _single_flight(cache_key: str) -> Iterator[None]:
    with _key_locks_meta:
        entry = _key_locks.setdefault(cache_key, [threading.Lock(), 0])
        entry[1] += 1
    entry[0].acquire()
    try:
        yield
    finally:
        entry[0].release()
        with _key_locks_meta:
            entry[1] -= 1
            if entry[1] == 0 and _key_locks.get(cache_key) is entry:
                del _key_locks[cache_key]


def recognize(
    db: "Database",
    hubspot: Any,
    surface: Surface,
    force_refresh: bool = False,
    page_title: str | None = None,
) -> dict:
    """Build the /recognize response body for one classified surface.

    Cacheable kinds (linkedin_profile, linkedin_company, website) go through
    prospector.recognize_cache with a 24h TTL; the rest are answered inline
    (they cost no lookups, so caching them would only add rows).

    `page_title` is the browser tab title (already validated by the route:
    str, <=300 chars, or None). When it parses to a name it rides into the
    payload as `name_hint` and drives the possible-matches search -- the
    title carries the person's REAL name where the slug only carries a
    lossy guess (the garbled-slug-name failure mode).
    """
    if surface.kind in ("linkedin_other", "ignored"):
        # Nothing to look up -- the panel idles quietly.
        return {"verdict": "idle", "surface": {"kind": surface.kind}}

    if surface.kind == "sales_nav":
        return {
            "verdict": "unsupported_surface",
            "surface": {"kind": "sales_nav"},
            "message": (
                "Sales Navigator URLs can't be enriched (the vendor rejects "
                "them) -- open the person's public LinkedIn profile "
                "(linkedin.com/in/...) and the panel will pick it up."
            ),
        }

    # Parse the tab title ONCE, up front. Company surfaces (website AND
    # linkedin_company) share the company parsing: a website <title>'s part
    # before " - "/" | " is usually the business name, and the hint only
    # ever feeds /resolve prefills -- never a lookup.
    if surface.kind == "linkedin_profile":
        name_hint = parse_linkedin_title(page_title, "linkedin_profile")
    else:
        name_hint = parse_linkedin_title(page_title, "linkedin_company")

    cache_key = _cache_key(surface)

    if not force_refresh:
        hit = _cache_get(db, cache_key)
        if hit is not None:
            return _with_refreshed_hint(db, hubspot, surface, cache_key,
                                        hit, name_hint)

    with _single_flight(cache_key):
        # RE-CHECK under the key lock: if we lost the race, the winner has
        # already filled the cache and we never touch HubSpot.
        if not force_refresh:
            hit = _cache_get(db, cache_key)
            if hit is not None:
                return _with_refreshed_hint(db, hubspot, surface, cache_key,
                                            hit, name_hint)

        # Opportunistic eviction, on the miss path only (a pure cache hit
        # stays write-free) and at most once per call. Best-effort: a failed
        # purge must never fail the recognize itself.
        try:
            db.execute(_CACHE_PURGE_SQL)
        except Exception:
            logger.warning("recognize cache purge failed", exc_info=True)

        if surface.kind == "linkedin_profile":
            payload = _recognize_profile(db, hubspot, surface, name_hint)
        else:  # website | linkedin_company -- same shape, different lookup
            payload = _recognize_company_surface(db, hubspot, surface, name_hint)

        # Round-trip through JSON before storing/returning so a fresh
        # response and a cached one are byte-identical in shape:
        # dates/Decimals become strings NOW, not only after a jsonb round
        # trip.
        payload = json.loads(json.dumps(payload, default=str))

        # Best-effort: a failed cache write must never eat the answer -- the
        # rep still gets the fresh payload, only the caching is lost.
        try:
            db.execute(
                _CACHE_PUT_SQL,
                {
                    "key": cache_key,
                    "payload": json.dumps(payload),
                    "ttl": RECOGNIZE_CACHE_TTL_S,
                },
            )
        except Exception:
            logger.warning("recognize cache write failed", exc_info=True)
        return {**payload, "cached": False}


def _cache_get(db: "Database", cache_key: str) -> dict | None:
    """One cache probe -> the response dict for a live entry, else None."""
    rows = db.query(
        _CACHE_GET_SQL, {"key": cache_key, "ttl": RECOGNIZE_CACHE_TTL_S}
    )
    if not rows:
        return None
    return {
        **rows[0]["payload"],
        "cached": True,
        "cache_age_s": rows[0]["cache_age_s"],
    }


def _with_refreshed_hint(
    db: "Database",
    hubspot: Any,
    surface: Surface,
    cache_key: str,
    hit: dict,
    name_hint: dict,
) -> dict:
    """Cache hit, and the caller NOW has a parseable tab title the cached
    payload lacked (titles often arrive a beat after the URL, so the first
    recognize of a profile can cache hint-less): graft the name_hint on and
    recompute the possible-matches from the better name, WITHOUT redoing
    the contact/account lookups -- the cached verdict/contact/account are
    still good, only the name knowledge improved. Without this refresh a
    24h cache entry would pin the slug-guessed name for a day (the
    garbled-slug-name failure mode).

    The rewrite keeps the entry's REMAINING TTL (expires_at is derived from
    cache_age_s) so refreshing a hint never extends how long the CRM facts
    live in cache."""
    if not name_hint or hit.get("name_hint"):
        return hit  # nothing to add, or the payload already knows the name

    cache_age_s = hit.get("cache_age_s")
    payload = {k: v for k, v in hit.items()
               if k not in ("cached", "cache_age_s")}
    payload["name_hint"] = name_hint
    # Only red profile payloads carry possible_matches; recompute those
    # from the title-derived name (green payloads and company surfaces
    # just gain the hint).
    if (surface.kind == "linkedin_profile"
            and "possible_matches" in payload and hubspot is not None):
        payload["possible_matches"] = _possible_matches(
            hubspot, surface.key, name_hint)
    payload = json.loads(json.dumps(payload, default=str))

    remaining_ttl = max(1, RECOGNIZE_CACHE_TTL_S - int(cache_age_s or 0))
    try:
        db.execute(
            _CACHE_PUT_SQL,
            {"key": cache_key, "payload": json.dumps(payload),
             "ttl": remaining_ttl},
        )
    except Exception:
        logger.warning("recognize cache hint refresh failed", exc_info=True)
    return {**payload, "cached": True, "cache_age_s": cache_age_s}


def _cache_key(surface: Surface) -> str:
    # Prefixes keep the two key spaces collision-proof (see the column
    # comment on prospector.recognize_cache.cache_key).
    if surface.kind == "website":
        return f"dom:{surface.key}"
    return f"li:{surface.key}"


# -- linkedin_profile ---------------------------------------------------------

# Suffix/credential tokens people staple onto their slug (john-smith-jr,
# jane-doe-mba). Dropped from the END before the last remaining token is
# taken as the surname -- otherwise "jr" would become the guessed last name
# and names_agree would reject every real candidate.
_SLUG_SUFFIX_NOISE: frozenset[str] = frozenset({
    "jr", "sr", "ii", "iii", "iv", "md", "phd", "mba", "cpa",
})

# Cap on the possible-match list: three candidates is a glance, ten is a
# chore the rep will skip.
_MAX_POSSIBLE_MATCHES = 3


def _is_slug_id_junk(token: str) -> bool:
    """LinkedIn slug id junk: the numeric/hash suffix LinkedIn appends to
    de-duplicate slugs (jane-doe-1b2a3f, john-smith-8a49b23). Pure digits,
    or 5+ chars mixing letters and digits."""
    if token.isdigit():
        return True
    return (
        len(token) >= 5
        and any(ch.isdigit() for ch in token)
        and any(ch.isalpha() for ch in token)
    )


def slug_name_guess(slug: str) -> tuple[str, str] | None:
    """Best-effort (first, last) name guess from a /in/ slug, else None.

    Pure, no I/O. Percent-decoding already happened upstream
    (classify_surface unquotes path segments), so the slug arrives as
    human text. Pipeline: split on '-', drop trailing id junk (see
    _is_slug_id_junk), drop single-char tokens (initials carry no search
    signal), drop trailing suffix/credential noise -- then first token =
    first name, LAST remaining token = last name (middle tokens ignored).
    Fewer than 2 survivors means the slug carries no derivable name
    (johnsmith, j-smith). Returned as-is, lowercase and all: HubSpot's
    search and names_agree are both case-insensitive, so title-casing here
    would be cosmetic churn.
    """
    tokens = [t for t in (slug or "").split("-") if t]
    while tokens and _is_slug_id_junk(tokens[-1]):
        tokens.pop()
    tokens = [t for t in tokens if len(t) > 1]
    while tokens and tokens[-1].lower() in _SLUG_SUFFIX_NOISE:
        tokens.pop()
    if len(tokens) < 2:
        return None
    return tokens[0], tokens[-1]


# --- tab-title parsing (pure) ----------------------------------------------
# LinkedIn tab titles look like:
#   "Jane Doe - VP of Operations at Acme | LinkedIn"
#   "(2) Jane Doe | LinkedIn"                       (notification counter)
#   "Acme Pest Control | LinkedIn"                  (company page)
# The part before the first " - " or " | " is the display name -- the real
# one, not the slug's lowercase guess (the garbled-slug-name failure mode).

# Leading notification counter: "(2) Jane Doe | LinkedIn".
_TITLE_COUNTER_RE = re.compile(r"^\(\d+\)\s+")
# Trailing " | LinkedIn" -- \s covers the non-breaking space (U+00A0)
# LinkedIn sometimes uses around the pipe.
_TITLE_LINKEDIN_SUFFIX_RE = re.compile(r"\s*\|\s*linkedin\s*$", re.IGNORECASE)
# First " - " or " | " separator (space-delimited, so hyphenated names like
# Smith-Jones survive).
_TITLE_SEP_RE = re.compile(r"\s[|\-]\s")

# Titles that are LinkedIn chrome (or website boilerplate), not a name.
# Compared lowercase AFTER the " | LinkedIn" suffix is stripped.
_GENERIC_TITLES: frozenset[str] = frozenset({
    "linkedin", "feed", "sign up", "sign in", "log in", "join linkedin",
    "notifications", "my network", "jobs", "messaging", "search",
    "home", "welcome",
})

# Leading honorifics: "Dr. Jane Doe" must not autofill First="Dr.".
_LEADING_HONORIFICS: frozenset[str] = frozenset({
    "dr", "mr", "mrs", "ms", "prof",
})


def parse_linkedin_title(title: str | None, kind: str) -> dict:
    """Best-effort name hint from a browser tab title. Pure, no I/O.

    kind='linkedin_profile' -> {"full_name", "first_name", "last_name"}
        (single-token names keep full_name; first/last stay empty -- an
        empty field invites a fix, a wrong guess invites a bad record).
    kind='linkedin_company' -> {"company_name": ...}.
    Garbage, empty, or generic-chrome titles ("LinkedIn", "Feed | LinkedIn",
    "Sign Up | LinkedIn") -> {} for every kind.

    Length is unbounded here on purpose: the extension truncates to 300
    chars and the server ignores anything longer, so this just has to stay
    safe on whatever string it is handed.
    """
    if not title or not isinstance(title, str):
        return {}
    text = _TITLE_COUNTER_RE.sub("", title.strip())
    text = _TITLE_LINKEDIN_SUFFIX_RE.sub("", text).strip()
    if not text:
        return {}
    # Everything after the first " - " / " | " is headline/nav chrome.
    head = _TITLE_SEP_RE.split(text, maxsplit=1)[0].strip()
    if not head or head.lower() in _GENERIC_TITLES:
        return {}

    if kind == "linkedin_company":
        return {"company_name": head}
    if kind != "linkedin_profile":
        return {}

    # Credentials first ("Jane Doe, MBA" -> "Jane Doe"), then honorifics.
    tokens = strip_trailing_credentials(head).split()
    while tokens and tokens[0].lower().rstrip(".") in _LEADING_HONORIFICS:
        tokens.pop(0)
    if not tokens:
        return {}
    full_name = " ".join(tokens)
    if len(tokens) == 1:
        return {"full_name": full_name, "first_name": "", "last_name": ""}
    # First token = first name, LAST token = last name; middles ignored --
    # same convention as slug_name_guess.
    return {
        "full_name": full_name,
        "first_name": tokens[0],
        "last_name": tokens[-1],
    }


def _possible_matches(
    hubspot: Any, profile_key: str, name_hint: dict | None = None
) -> list[dict]:
    """READ-ONLY possible-match list for a profile whose URL matched no
    contact: existing HubSpot contacts who might be this person but have no
    LinkedIn URL on file (a pilot review surfaced this URL-only blind spot).

    Recall comes from find_contacts_by_name; correctness is enforced HERE:
      * a candidate with ANY hs_linkedin_url is dropped -- a different URL
        means provably a different person, the same URL would have been the
        green match; either way not a backfill candidate;
      * the candidate's stored full name must names_agree with the guess
        (nickname/credential tolerance -- Billy Smith vs william-smith).
    Never a verdict change and never a write: linking is a Phase-3 rep
    click, this list is just "needs your eyes".

    The name comes from the tab-title hint when one parsed with BOTH a
    first and last name; the slug guess is only the fallback. The title is
    the person's real display name -- the slug is a lossy lowercase mash
    that autofills a garbled first/last name (e.g. from a slug like
    'jane-doe-sf').
    """
    if name_hint and name_hint.get("first_name") and name_hint.get("last_name"):
        first = name_hint["first_name"]
        last = name_hint["last_name"]
        guessed_full = name_hint.get("full_name") or f"{first} {last}"
    else:
        guess = slug_name_guess(profile_key.rsplit("/", 1)[-1])
        if not guess:
            return []
        first, last = guess
        guessed_full = f"{first} {last}"

    matches: list[dict] = []
    for candidate in hubspot.find_contacts_by_name(first, last) or []:
        if (candidate.get("hs_linkedin_url") or "").strip():
            continue
        candidate_name = " ".join(
            part
            for part in (candidate.get("firstname"), candidate.get("lastname"))
            if part
        ).strip()
        if not names_agree(candidate_name, guessed_full):
            continue
        matches.append(_possible_match_card(hubspot, candidate))
        if len(matches) >= _MAX_POSSIBLE_MATCHES:
            break
    return matches


def _possible_match_card(hubspot: Any, contact: dict) -> dict:
    # email_domain ONLY, never the full address: the local part is PII noise
    # here -- the domain is the company hint the rep actually needs.
    email = contact.get("email") or ""
    email_domain = email.rsplit("@", 1)[-1].lower() if "@" in email else None
    name = " ".join(
        part for part in (contact.get("firstname"), contact.get("lastname")) if part
    ).strip()
    return {
        "hs_contact_id": str(contact["id"]),
        "name": name or f"id:{contact['id']}",
        "jobtitle": contact.get("jobtitle"),
        "email_domain": email_domain,
        "lifecycle_stage": contact.get("lifecyclestage"),
        "owner_name": _hubspot_owner_name(hubspot, contact.get("hubspot_owner_id")),
        "hubspot_url": hubspot.contact_hubspot_url(contact["id"]),
    }


def _profile_verdict(contact: dict | None) -> str:
    """Phase 1: green iff the contact exists. A yellow verdict ('person
    unknown but their COMPANY is a known account') needs the person's company,
    which a bare profile page doesn't give us -- Phase 3's resolve step is
    expected to upgrade red -> yellow here once it can attach one. Keep this
    factored out so that upgrade is a one-function change."""
    return "green" if contact else "red"


def _recognize_profile(
    db: "Database",
    hubspot: Any,
    surface: Surface,
    name_hint: dict | None = None,
) -> dict:
    contact = hubspot.find_contact_by_linkedin(surface.key)

    payload = {
        "surface": {"kind": surface.kind, "key": surface.key},
        "verdict": _profile_verdict(contact),
        "contact": _contact_card(hubspot, contact) if contact else None,
        # More than one CRM contact shares this LinkedIn URL -- surfaced so
        # the panel can flag probable duplicates instead of silently picking.
        "multiple_matches": bool(contact and contact.get("_multiple_matches")),
    }

    if name_hint:
        # wire contract with extension/sidepanel.js: the commit form's
        # autofill prefers this over its client-side slug guess (the
        # garbled-slug-name failure mode). {full_name, first_name, last_name}.
        payload["name_hint"] = name_hint

    if not contact:
        # URL miss: the profile matched NO contact by hs_linkedin_url. Look
        # for contacts who might be this person but have no LinkedIn URL on
        # file. Verdict STAYS red -- this is a hint, never a match, and the
        # one-click link write ships with Phase 3.
        # wire contract with extension/sidepanel.js -- `possible_matches` is
        # a list of {hs_contact_id, name, jobtitle, email_domain (never the
        # full address), lifecycle_stage, owner_name, hubspot_url};
        # `possible_match_note` is rendered verbatim. Cached with the rest
        # of the payload (plain JSON, same 24h TTL).
        payload["possible_matches"] = _possible_matches(
            hubspot, surface.key, name_hint)
        payload["possible_match_note"] = (
            "matched by name from the profile URL — needs your eyes, "
            "never auto-linked"
        )

    return payload


def _hubspot_owner_name(hubspot: Any, owner_id: Any) -> str | None:
    """Owner display name via the HubSpot owners API; falls back to the raw
    id so ownership is never hidden just because the lookup missed (the
    deactivated-rep pattern)."""
    if not owner_id:
        return None
    owner = hubspot.get_owner(owner_id)
    if owner:
        return " ".join(
            part for part in (owner.get("firstName"), owner.get("lastName")) if part
        ).strip() or owner.get("email")
    return str(owner_id)


def _contact_card(hubspot: Any, contact: dict) -> dict:
    name = " ".join(
        part for part in (contact.get("firstname"), contact.get("lastname")) if part
    ).strip()
    owner_name = _hubspot_owner_name(hubspot, contact.get("hubspot_owner_id"))
    return {
        "id": str(contact["id"]),
        "name": name or contact.get("email") or f"id:{contact['id']}",
        "email": contact.get("email"),
        "jobtitle": contact.get("jobtitle"),
        "lifecyclestage": contact.get("lifecyclestage"),
        "lastmodifieddate": contact.get("lastmodifieddate"),
        "owner_name": owner_name,
        "hubspot_url": hubspot.contact_hubspot_url(contact["id"]),
    }


# -- website / linkedin_company ------------------------------------------------

def _recognize_company_surface(
    db: "Database",
    hubspot: Any,
    surface: Surface,
    name_hint: dict | None = None,
) -> dict:
    if surface.kind == "website":
        matches = hubspot.find_companies_by_domain(surface.key) or []
    else:
        slug = surface.extra.get("slug") or surface.key.rsplit("/", 1)[-1]
        matches = hubspot.find_company_by_linkedin_slug(slug) or []

    preferred_id = None
    if matches:
        # Multi-match rule: prefer the record that carries an ICP tier
        # (hs_ideal_customer_profile set) -- a tierless twin is usually an
        # import stray. All matches are still listed, and merge_candidate
        # tells the panel to flag the duplicate for hygiene.
        preferred = next(
            (m for m in matches if m.get("hs_ideal_customer_profile")), matches[0]
        )
        preferred_id = str(preferred["id"])

    payload = {
        "surface": {"kind": surface.kind, "key": surface.key},
        # green = this company is already in the CRM (same semantics as the
        # profile surface: green means "known", red means "net-new").
        "verdict": "green" if matches else "red",
        "company_matches": [_company_card(hubspot, m) for m in matches],
        "preferred_company_id": preferred_id,
        "merge_candidate": len(matches) > 1,
    }
    if name_hint:
        # {company_name}: a /resolve prefill for the panel later. Changes
        # no lookup and no verdict on this surface.
        payload["name_hint"] = name_hint
    return payload


def _company_card(hubspot: Any, company: dict) -> dict:
    return {
        "id": str(company["id"]),
        "name": company.get("name"),
        "domain": company.get("domain"),
        "tier": company.get("hs_ideal_customer_profile") or None,
        "is_target_account": company.get("hs_is_target_account"),
        "state": company.get("state"),
        "linkedin_company_page": company.get("linkedin_company_page"),
        "hubspot_url": hubspot.company_hubspot_url(company["id"]),
    }
