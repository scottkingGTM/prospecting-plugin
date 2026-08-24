"""HubSpot client for prospecting_plugin.

Phase 1 shipped this module read-only; the commit write-pipeline phase added
the write methods (create/update/associate/note). The safety boundary did NOT
move into this module: every write here is plumbing only, reachable solely
through prospector.writer's guarded pipeline (confirm -> idempotency -> cap ->
audit-attempt -> DRY_RUN stop -> side effect -> audit-outcome). Nothing in
this file checks DRY_RUN because nothing in this file is allowed to decide to
write -- writer.py decides, this module executes. Write methods raise
HubSpotError on any failure; they never return a partial success.

Retry posture mirrors a shared HubSpot-write helper we've used before: 429
and 5xx are retried with exponential backoff (jittered, Retry-After honored
when
HubSpot sends it), everything else fails fast. 401/403 in particular are
never retried -- a bad or under-scoped token cannot fix itself, and hammering
the API with it just burns the rate limit.

The token is set on the underlying httpx client ONCE at construction and is
never logged and never included in an exception message. Error text is built
from the status code, method, path, and (truncated) response body only.

LinkedIn URLs: prospector.matching.norm_linkedin defines the ONE canonical
form for this app (no protocol, no www, no trailing slash, lowercased).
HubSpot's search is exact-match on whatever string a portal happens to store,
so `find_contact_by_linkedin` ORs the canonical form together with the common
stored variants (https://www. prefix, trailing slash) in a single search
rather than inventing a second normalizer.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

import httpx

from .filters import extract_domain
from .matching import norm_linkedin

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hubapi.com"
TIMEOUT_SECONDS = 30.0

# Total attempts per request (1 initial + 3 retries), matching the posture of
# the shared HubSpot-write helper.
MAX_ATTEMPTS = 4

CONTACT_SEARCH_PROPERTIES = [
    "firstname",
    "lastname",
    "email",
    "jobtitle",
    "hs_linkedin_url",
    "hubspot_owner_id",
    "lifecyclestage",
    "lastmodifieddate",
    "associatedcompanyid",
]

# Properties for the possible-match name search. Deliberately WITHOUT
# lastmodifieddate: a name-guess candidate card shows who the person might
# be (name/title/domain/owner/stage), not record freshness.
NAME_SEARCH_PROPERTIES = [
    "firstname",
    "lastname",
    "email",
    "jobtitle",
    "hs_linkedin_url",
    "hubspot_owner_id",
    "lifecyclestage",
    "associatedcompanyid",
]

COMPANY_SEARCH_PROPERTIES = [
    "name",
    "domain",
    "hs_ideal_customer_profile",
    "hs_is_target_account",
    "hubspot_owner_id",
    "state",
    "linkedin_company_page",
    # createdate feeds recognize's pending_sync-vs-untracked call: a view
    # miss only reads as "sync lag" when the HubSpot record is actually new
    # (a pilot review found long-standing untiered records are NOT pending
    # anything).
    "createdate",
]

# Properties for the /record detail endpoint (company). Superset of the
# search list on purpose: the panel's record card shows the basics a rep
# would otherwise open HubSpot for (a pilot review found the card showed
# only name+domain+chip and nothing else).
COMPANY_DETAIL_PROPERTIES = [
    "name",
    "domain",
    "industry",
    "numberofemployees",
    "city",
    "state",
    "lifecyclestage",
    "hs_ideal_customer_profile",
    "hs_is_target_account",
    "hubspot_owner_id",
    "createdate",
    "hs_lastmodifieddate",
    "description",
    "phone",
    "website",
]

# Properties for the /record detail endpoint (contact). Distinct from the
# narrow CONTACT_SEARCH_PROPERTIES on purpose -- that list backs the
# link-guard re-read (get_contact) and must stay small and stable.
CONTACT_DETAIL_PROPERTIES = [
    "firstname",
    "lastname",
    "jobtitle",
    "email",
    "hs_additional_emails",
    "phone",
    "hs_linkedin_url",
    "lifecyclestage",
    "hubspot_owner_id",
    "associatedcompanyid",
    "createdate",
    "lastmodifieddate",
    "notes_last_contacted",
    "num_associated_deals",
]

# Properties for the /record company card's contact roster: enough to render
# a one-line person row, nothing more.
COMPANY_CONTACT_LIST_PROPERTIES = [
    "firstname",
    "lastname",
    "jobtitle",
    "email",
    "hs_linkedin_url",
    "lifecyclestage",
]

# Properties for the live email pre-check. hs_additional_emails is included
# so matched_email can be attributed when the hit came from a secondary
# address rather than the primary email property.
EMAIL_SEARCH_PROPERTIES = [
    "firstname",
    "lastname",
    "email",
    "hs_additional_emails",
    "jobtitle",
    "hubspot_owner_id",
    "lifecyclestage",
    "associatedcompanyid",
]

# HubSpot caps a search at 5 filterGroups. find_contacts_by_emails spends
# one group on `email IN (chunk)` and one group PER email on
# `hs_additional_emails CONTAINS_TOKEN`, so the chunk size is 4 (1 + 4 = 5).
# At that size neither the IN-operator value ceiling nor the 100-result
# page is ever the binding constraint.
_EMAILS_PER_SEARCH = 4

# Batch endpoints (companies/deals batch read, batch create) accept at most
# 100 inputs per request.
_BATCH_LIMIT = 100

# v4 association type ids (HUBSPOT_DEFINED) used when creating a note.
_ASSOC_NOTE_TO_CONTACT = 202
_ASSOC_NOTE_TO_COMPANY = 190

# HUBSPOT_DEFINED contact -> company PRIMARY association, accepted inline by
# the v3 create endpoints. Used so a contact is BORN associated (from a
# hardening pass): the portal's "auto-create companies from email domains"
# setting fires in the gap between a bare create and a later associate call
# -- it once minted dozens of junk companies off confirmed-alternate email
# domains. Inline association leaves it no gap.
_ASSOC_CONTACT_TO_COMPANY_PRIMARY = 279


class HubSpotError(Exception):
    """A HubSpot call failed in a way the caller must handle.

    `status_code` is the HTTP status that caused the failure, or None when
    the request never got a response (transport-level failure).
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _linkedin_search_variants(raw_url: str) -> list[str]:
    """The canonical norm_linkedin form plus the common as-stored variants.

    HubSpot search is exact-match, and portals store LinkedIn URLs however
    they arrived: bare, with https://www., with a trailing slash. One search
    ORs all four spellings so a match is found regardless of which one a
    given contact record carries.
    """
    canonical = norm_linkedin(raw_url)
    if not canonical:
        return []
    # Five variants -- one filterGroup each, which stays within HubSpot's
    # cap of 5 filterGroups per search.
    return [
        canonical,
        canonical + "/",
        f"https://www.{canonical}",
        f"https://www.{canonical}/",
        f"https://{canonical}",
    ]


def company_page_slug(url: str | None) -> str:
    """The bare /company/<slug> slug from a LinkedIn company-page URL,
    lowercased -- '' when the input is empty or carries no /company/
    segment (a bare slug like 'acme-corp' therefore returns ''). Same
    normalization recognize.py applies to a live company-page URL
    (norm_linkedin: lowercase, strip protocol/www, strip query/fragment,
    strip trailing slash), so the two sides compare the identical
    canonical slug.

    Public on purpose: server.py's /resolve route reuses it to turn the
    panel's full company-page URL into the bare slug the slug search
    needs (from a hardening pass) -- ONE parser, never two."""
    normalized = norm_linkedin(url)
    if not normalized:
        return ""
    segments = [seg for seg in normalized.split("/") if seg]
    for i, segment in enumerate(segments):
        if segment == "company" and i + 1 < len(segments):
            return segments[i + 1]
    return ""


def _flatten(record: dict) -> dict:
    """HubSpot's {id, properties: {...}} -> one flat dict {id, <properties>}."""
    flat: dict[str, Any] = {"id": str(record.get("id") or "")}
    flat.update(record.get("properties") or {})
    return flat


def _matched_email(flat: dict, wanted: list[str]) -> str:
    """Which of the searched-for emails this flattened contact matched on.

    The primary `email` property wins when it is one of the searched
    addresses; otherwise the semicolon-separated hs_additional_emails list
    is scanned. When neither attributes cleanly (HubSpot matched on
    something we cannot see), the record's own primary email is surfaced so
    the caller still sees WHY the record is in the result set.
    """
    primary = (flat.get("email") or "").strip().lower()
    if primary in wanted:
        return primary
    extra = flat.get("hs_additional_emails") or ""
    tokens = {t.strip().lower() for t in str(extra).split(";") if t.strip()}
    for w in wanted:
        if w in tokens:
            return w
    return primary


class HubSpotClient:
    """Thin read-only wrapper around the HubSpot v3 CRM API.

    `transport` is injectable so tests can pass an httpx.MockTransport and
    exercise every retry branch without a network. `sleep` is the backoff
    sleeper, injectable for the same reason. `portal_id` (optional, from
    HUBSPOT_PORTAL_ID via config) is only used to build app.hubspot.com
    record links; when empty the URL helpers return None and the panel
    simply omits the link.
    """

    def __init__(
        self,
        token: str,
        transport: httpx.BaseTransport | None = None,
        *,
        portal_id: str = "",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._portal_id = (portal_id or "").strip()
        self._sleep = sleep
        # Auth header set once, here, and never touched again -- no other
        # code path in this module ever sees or formats the token.
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            transport=transport,
        )

    # -- transport ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        json: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        """One HTTP call with the module's retry posture (see module docstring).

        Raises HubSpotError on any non-2xx outcome; the message never
        contains the token (it is built from status/method/path/body only).
        """
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = self._client.request(method, path, json=json, params=params)
            except httpx.HTTPError as exc:
                # Transport-level failure (timeout, connect error). Retry it
                # like a 5xx; `from None` so the httpx exception -- which can
                # carry the full request, headers included -- never rides
                # along into our error chain.
                if attempt < MAX_ATTEMPTS - 1:
                    delay = self._backoff_delay(attempt, retry_after=None)
                    logger.warning(
                        "HubSpot %s %s transport error (%s); retrying in %.1fs "
                        "(attempt %d/%d)",
                        method, path, type(exc).__name__, delay,
                        attempt + 1, MAX_ATTEMPTS,
                    )
                    self._sleep(delay)
                    continue
                raise HubSpotError(
                    f"HubSpot {method} {path} failed after {MAX_ATTEMPTS} "
                    f"attempts: {type(exc).__name__}"
                ) from None

            if resp.status_code in (401, 403):
                # Never retried: a bad token cannot fix itself.
                raise HubSpotError(
                    f"HubSpot {method} {path} returned HTTP {resp.status_code} "
                    "-- check HUBSPOT_TOKEN scopes",
                    status_code=resp.status_code,
                )

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < MAX_ATTEMPTS - 1:
                    delay = self._backoff_delay(
                        attempt, retry_after=resp.headers.get("Retry-After"))
                    logger.warning(
                        "HubSpot %s %s returned HTTP %s; retrying in %.1fs "
                        "(attempt %d/%d)",
                        method, path, resp.status_code, delay,
                        attempt + 1, MAX_ATTEMPTS,
                    )
                    self._sleep(delay)
                    continue
                raise HubSpotError(
                    f"HubSpot {method} {path} returned HTTP {resp.status_code} "
                    f"after {MAX_ATTEMPTS} attempts: {resp.text[:200]}",
                    status_code=resp.status_code,
                )

            if resp.status_code >= 400:
                raise HubSpotError(
                    f"HubSpot {method} {path} returned HTTP {resp.status_code}: "
                    f"{resp.text[:200]}",
                    status_code=resp.status_code,
                )

            return resp

        raise HubSpotError("unreachable")  # pragma: no cover

    @staticmethod
    def _backoff_delay(attempt: int, retry_after: str | None) -> float:
        """Retry-After wins when HubSpot sends a usable one -- capped at 30s
        so a pathological header can't park a request thread for an hour --
        otherwise exponential backoff (1s, 2s, 4s) with jitter so parallel
        callers don't retry in lockstep."""
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), 30.0))
            except ValueError:
                pass  # non-numeric Retry-After -> fall through to backoff
        return float(2 ** attempt) + random.uniform(0.0, 0.5)

    def _search(self, object_type: str, body: dict) -> dict:
        resp = self._request(
            "POST", f"/crm/v3/objects/{object_type}/search", json=body)
        return resp.json() or {}

    # -- contacts -------------------------------------------------------------

    def find_contact_by_linkedin(self, norm_url: str) -> dict | None:
        """Find the contact whose hs_linkedin_url matches, in any spelling.

        One search: filterGroups are ORed by HubSpot, so the canonical
        norm_linkedin form and the stored variants each get their own group.
        Returns the first match flattened to {id, <properties>}, or None.
        When more than one contact carries the URL -- a live dedupe problem,
        not a bug here -- the first is returned with "_multiple_matches"
        set to the total so the caller can surface it.
        """
        variants = _linkedin_search_variants(norm_url)
        if not variants:
            return None
        body = {
            "filterGroups": [
                {"filters": [{
                    "propertyName": "hs_linkedin_url",
                    "operator": "EQ",
                    "value": variant,
                }]}
                for variant in variants
            ],
            "properties": CONTACT_SEARCH_PROPERTIES,
            "limit": 10,
        }
        data = self._search("contacts", body)
        results = data.get("results") or []
        if not results:
            return None
        match = _flatten(results[0])
        total = data.get("total") or len(results)
        if total > 1:
            match["_multiple_matches"] = total
        return match

    def find_contacts_by_name(self, first: str, last: str) -> list[dict]:
        """Contacts whose first AND last name carry the given tokens.

        Powers the "possible match" list on a profile whose URL matched no
        contact (a pilot review found URL-only matching misses contacts that
        simply lack hs_linkedin_url). ONE filterGroup, two
        ANDed CONTAINS_TOKEN filters -- token matching keeps "Billy" from
        matching inside "Billyson" while tolerating middle names and
        credentials stored in the name fields. Recall net only: the CALLER
        owns correctness (nickname agreement, dropping already-linked
        contacts) -- this method just returns what HubSpot found, flattened,
        capped at 10.
        """
        first = (first or "").strip()
        last = (last or "").strip()
        if not first or not last:
            return []
        body = {
            "filterGroups": [{"filters": [
                {
                    "propertyName": "firstname",
                    "operator": "CONTAINS_TOKEN",
                    "value": first,
                },
                {
                    "propertyName": "lastname",
                    "operator": "CONTAINS_TOKEN",
                    "value": last,
                },
            ]}],
            "properties": NAME_SEARCH_PROPERTIES,
            "limit": 10,
        }
        data = self._search("contacts", body)
        return [_flatten(r) for r in data.get("results") or []]

    # -- companies ------------------------------------------------------------

    def find_companies_by_domain(self, domain: str) -> list[dict]:
        """All companies on a domain, flattened, in HubSpot's result order.

        Returns ALL matches: multiple company records sharing one domain is a
        live condition in this portal, and hiding it behind a first-match
        would route prospects to the wrong record. The input is normalized to
        the bare registered domain (lowercased, protocol/www stripped) before
        the exact-match search.
        """
        normalized = extract_domain(domain)
        if not normalized:
            return []
        body = {
            "filterGroups": [{"filters": [{
                "propertyName": "domain",
                "operator": "EQ",
                "value": normalized,
            }]}],
            "properties": COMPANY_SEARCH_PROPERTIES,
            "limit": 100,
        }
        data = self._search("companies", body)
        return [_flatten(r) for r in data.get("results") or []]

    def find_company_by_linkedin_slug(self, slug: str) -> list[dict]:
        """Companies whose linkedin_company_page is the /company/<slug> page.

        CONTAINS_TOKEN is the recall net (portals store the company page URL
        in as many spellings as the contact one; the slug is the stable
        part), but it is NOT the correctness gate: CONTAINS_TOKEN tokenizes
        on non-alphanumerics, so 'acme' matches 'acme-corp' -- a
        wrong-company GREEN cached 24h. Exact-slug equality is the
        correctness gate (from a hardening pass): each result's slug is
        extracted from its linkedin_company_page (lowercased, trailing
        slash/query stripped -- see company_page_slug) and must equal the
        requested slug, case-insensitively. Records with an empty
        linkedin_company_page are dropped -- they only matched via token
        noise. Returns the surviving matches, flattened.
        """
        slug = (slug or "").strip()
        if not slug:
            return []
        body = {
            "filterGroups": [{"filters": [{
                "propertyName": "linkedin_company_page",
                "operator": "CONTAINS_TOKEN",
                "value": slug,
            }]}],
            "properties": COMPANY_SEARCH_PROPERTIES,
            "limit": 100,
        }
        data = self._search("companies", body)
        wanted = slug.lower()
        return [
            flat
            for flat in (_flatten(r) for r in data.get("results") or [])
            if company_page_slug(flat.get("linkedin_company_page")) == wanted
        ]

    # -- owners ---------------------------------------------------------------

    def get_owner(self, owner_id: str) -> dict | None:
        """Owner lookup -> {id, firstName, lastName, email}, or None on 404.

        A 404 is a normal outcome (a deactivated rep still referenced by a
        record), so it maps to None rather than an error; everything else
        propagates.
        """
        try:
            resp = self._request("GET", f"/crm/v3/owners/{owner_id}")
        except HubSpotError as exc:
            if exc.status_code == 404:
                return None
            raise
        data = resp.json() or {}
        return {
            "id": str(data.get("id") or owner_id),
            "firstName": data.get("firstName") or "",
            "lastName": data.get("lastName") or "",
            "email": data.get("email") or "",
        }

    # -- contact reads for the write pipeline ---------------------------------

    def find_contacts_by_emails(self, emails: list[str]) -> list[dict]:
        """Live email pre-check: every contact carrying ANY of these emails.

        One search per chunk of 4 emails (see _EMAILS_PER_SEARCH): filter
        group 1 is `email IN (chunk)`, plus one `hs_additional_emails
        CONTAINS_TOKEN <email>` group per email -- the 5-filterGroup cap is
        exactly filled. Groups are ORed by HubSpot, so a hit on either the
        primary or a secondary address surfaces the contact. Each flattened
        result carries `matched_email` (op_clay lesson: results are never
        attributed by position, only by echoed content). Duplicated inputs
        and cross-chunk duplicate contacts are collapsed.
        """
        wanted = [e.strip().lower() for e in emails or [] if e and e.strip()]
        seen_emails: set[str] = set()
        unique = [e for e in wanted
                  if not (e in seen_emails or seen_emails.add(e))]
        out: list[dict] = []
        seen_ids: set[str] = set()
        for i in range(0, len(unique), _EMAILS_PER_SEARCH):
            chunk = unique[i:i + _EMAILS_PER_SEARCH]
            groups: list[dict] = [{"filters": [{
                "propertyName": "email",
                "operator": "IN",
                "values": chunk,
            }]}]
            groups += [
                {"filters": [{
                    "propertyName": "hs_additional_emails",
                    "operator": "CONTAINS_TOKEN",
                    "value": email,
                }]}
                for email in chunk
            ]
            body = {
                "filterGroups": groups,
                "properties": EMAIL_SEARCH_PROPERTIES,
                "limit": 100,
            }
            data = self._search("contacts", body)
            for record in data.get("results") or []:
                flat = _flatten(record)
                if flat["id"] in seen_ids:
                    continue
                seen_ids.add(flat["id"])
                flat["matched_email"] = _matched_email(flat, chunk)
                out.append(flat)
        return out

    def get_contact(self, contact_id: str) -> dict | None:
        """One contact by id, flattened -- or None on 404.

        The live re-read behind the link_linkedin guard: the guard must
        compare against what HubSpot holds NOW, not against whatever the
        panel cached when the rep opened it. 404 maps to None (deleted or
        merged-away id); everything else propagates.
        """
        try:
            resp = self._request(
                "GET",
                f"/crm/v3/objects/contacts/{contact_id}",
                params={"properties": ",".join(CONTACT_SEARCH_PROPERTIES)},
            )
        except HubSpotError as exc:
            if exc.status_code == 404:
                return None
            raise
        return _flatten(resp.json() or {})

    # -- record detail reads (the /record endpoint) ----------------------------

    def get_company(self, company_id: str) -> dict | None:
        """One company by id with the detail property set, flattened -- or
        None on 404 (deleted or merged-away id; a merge mints a new
        canonical id and the old one goes dark)."""
        try:
            resp = self._request(
                "GET",
                f"/crm/v3/objects/companies/{company_id}",
                params={"properties": ",".join(COMPANY_DETAIL_PROPERTIES)},
            )
        except HubSpotError as exc:
            if exc.status_code == 404:
                return None
            raise
        return _flatten(resp.json() or {})

    def get_contact_detail(self, contact_id: str) -> dict | None:
        """One contact by id with the detail property set, flattened -- or
        None on 404.

        Deliberately separate from get_contact: that narrow read backs the
        link_linkedin guard and its property list is a wire contract with
        writer.py -- widening it there to feed a panel card would couple
        the guard to display concerns."""
        try:
            resp = self._request(
                "GET",
                f"/crm/v3/objects/contacts/{contact_id}",
                params={"properties": ",".join(CONTACT_DETAIL_PROPERTIES)},
            )
        except HubSpotError as exc:
            if exc.status_code == 404:
                return None
            raise
        return _flatten(resp.json() or {})

    def get_company_contacts(self, company_id: str, limit: int = 10) -> list[dict]:
        """Up to `limit` contacts associated to a company, flattened.

        v4 association list (first page only -- a panel roster, not an
        export), then a contacts batch read for the display properties.
        A 404 from the association API means the company id no longer
        resolves; for a read-only roster that is an empty list, not an
        error -- the /record route surfaces the company's own 404
        separately via get_company."""
        try:
            resp = self._request(
                "GET",
                f"/crm/v4/objects/companies/{company_id}/associations/contacts",
                params={"limit": limit},
            )
        except HubSpotError as exc:
            if exc.status_code == 404:
                return []
            raise
        contact_ids: list[str] = []
        for row in (resp.json() or {}).get("results") or []:
            to_id = row.get("toObjectId")
            if to_id and str(to_id) not in contact_ids:
                contact_ids.append(str(to_id))
        contact_ids = contact_ids[:limit]
        if not contact_ids:
            return []
        batch = self._request(
            "POST", "/crm/v3/objects/contacts/batch/read",
            json={
                "inputs": [{"id": contact_id} for contact_id in contact_ids],
                "properties": COMPANY_CONTACT_LIST_PROPERTIES,
            },
        )
        return [_flatten(r) for r in (batch.json() or {}).get("results") or []]

    # -- writes (reachable only through prospector.writer's pipeline) ---------

    def create_contact(self, props: dict, company_id: str | None = None) -> dict:
        """Create ONE contact via batch/create and echo-match the response.

        batch/create even for a single input, and the created object is
        STILL located by its echoed email rather than by position -- a lesson
        from a shared write helper: batch results come back out of order, and
        code that zips against inputs writes the wrong ids into its own
        bookkeeping. With no email to match on
        (an email-less commit is a legal LinkedIn-identity create) a
        single-result response is accepted as-is. No echo match =
        HubSpotError, because returning an unverified record would poison
        every downstream step (associate, note, audit detail).

        `company_id`, when given, rides in the create payload as the
        HUBSPOT_DEFINED contact->company primary association (type 279) so
        the contact is BORN associated -- the portal's company-auto-create
        setting never sees an unassociated contact and cannot mint a junk
        company off the email domain in the create->associate gap
        (from a hardening pass).
        """
        email = (props.get("email") or "").strip().lower()
        one_input: dict[str, Any] = {"properties": props}
        if company_id:
            one_input["associations"] = [{
                "to": {"id": str(company_id)},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": _ASSOC_CONTACT_TO_COMPANY_PRIMARY,
                }],
            }]
        resp = self._request(
            "POST", "/crm/v3/objects/contacts/batch/create",
            json={"inputs": [one_input]},
        )
        results = (resp.json() or {}).get("results") or []
        if email:
            for record in results:
                echoed = ((record.get("properties") or {}).get("email") or "")
                if echoed.strip().lower() == email:
                    return _flatten(record)
            raise HubSpotError(
                "contact batch/create response did not echo the requested "
                "email -- refusing to guess which record was created"
            )
        if len(results) == 1:
            return _flatten(results[0])
        raise HubSpotError(
            f"contact batch/create returned {len(results)} results with no "
            "email to echo-match on"
        )

    def create_company(self, props: dict) -> dict:
        """Create ONE company via batch/create, echo-matched on domain.

        Same shape and same op_clay rationale as create_contact -- match on
        the echoed domain, never on result order.
        """
        domain = (props.get("domain") or "").strip().lower()
        resp = self._request(
            "POST", "/crm/v3/objects/companies/batch/create",
            json={"inputs": [{"properties": props}]},
        )
        results = (resp.json() or {}).get("results") or []
        if domain:
            for record in results:
                echoed = ((record.get("properties") or {}).get("domain") or "")
                if echoed.strip().lower() == domain:
                    return _flatten(record)
            raise HubSpotError(
                "company batch/create response did not echo the requested "
                "domain -- refusing to guess which record was created"
            )
        if len(results) == 1:
            return _flatten(results[0])
        raise HubSpotError(
            f"company batch/create returned {len(results)} results with no "
            "domain to echo-match on"
        )

    def update_contact(self, contact_id: str, props: dict) -> dict:
        """PATCH one contact's properties; returns the updated flat record."""
        resp = self._request(
            "PATCH", f"/crm/v3/objects/contacts/{contact_id}",
            json={"properties": props},
        )
        return _flatten(resp.json() or {})

    def update_company(self, company_id: str, props: dict) -> dict:
        """PATCH one company's properties; returns the updated flat record."""
        resp = self._request(
            "PATCH", f"/crm/v3/objects/companies/{company_id}",
            json={"properties": props},
        )
        return _flatten(resp.json() or {})

    def associate_contact_company(self, contact_id: str, company_id: str) -> None:
        """Associate a contact to a company with the v4 DEFAULT association.

        The default endpoint applies HubSpot's standard contact->company
        association (and primary-company semantics) without this code
        hardcoding type ids that differ per portal.
        """
        self._request(
            "PUT",
            f"/crm/v4/objects/contacts/{contact_id}"
            f"/associations/default/companies/{company_id}",
        )

    def companies_batch_read(self, ids: list[str]) -> dict[str, dict]:
        """Batch-read companies by id -> {id: flat record}.

        THE merged-away-id detector (op_clay pattern): HubSpot's batch read
        simply omits ids that no longer resolve -- deleted, or merged into
        another record (a merge mints a NEW canonical id; the old one goes
        dark). An id absent from the returned dict is therefore stale and
        must not be written against. Requested in chunks of 100 (the batch
        endpoint's input cap).
        """
        cleaned = [str(i).strip() for i in ids or [] if str(i or "").strip()]
        out: dict[str, dict] = {}
        for i in range(0, len(cleaned), _BATCH_LIMIT):
            chunk = cleaned[i:i + _BATCH_LIMIT]
            resp = self._request(
                "POST", "/crm/v3/objects/companies/batch/read",
                json={
                    "inputs": [{"id": company_id} for company_id in chunk],
                    "properties": COMPANY_SEARCH_PROPERTIES,
                },
            )
            for record in (resp.json() or {}).get("results") or []:
                flat = _flatten(record)
                out[flat["id"]] = flat
        return out

    def create_note(
        self,
        body_text: str,
        contact_id: str,
        company_id: str | None = None,
    ) -> str:
        """Create a note on a contact (and optionally its company); returns
        the note id.

        `body_text` is PLAIN TEXT built by writer.py -- this method never
        constructs or accepts HTML markup on purpose (free text into
        hs_note_body renders fine, and keeping markup out of the pipeline
        means nothing rep-typed can inject it). Association type ids are the
        HUBSPOT_DEFINED note->contact (202) and note->company (190) pairs.
        """
        associations: list[dict] = [{
            "to": {"id": str(contact_id)},
            "types": [{
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": _ASSOC_NOTE_TO_CONTACT,
            }],
        }]
        if company_id:
            associations.append({
                "to": {"id": str(company_id)},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": _ASSOC_NOTE_TO_COMPANY,
                }],
            })
        resp = self._request(
            "POST", "/crm/v3/objects/notes",
            json={
                "properties": {
                    # hs_timestamp is required on engagements; ms epoch.
                    "hs_timestamp": int(time.time() * 1000),
                    "hs_note_body": body_text,
                },
                "associations": associations,
            },
        )
        note_id = str((resp.json() or {}).get("id") or "")
        if not note_id:
            raise HubSpotError("note create response carried no id")
        return note_id

    # -- record links -----------------------------------------------------------

    def contact_hubspot_url(self, contact_id: str) -> str | None:
        """app.hubspot.com link for a contact, or None when no portal id is
        configured (the panel just omits the link)."""
        if not self._portal_id:
            return None
        return (f"https://app.hubspot.com/contacts/{self._portal_id}"
                f"/record/0-1/{contact_id}")

    def company_hubspot_url(self, company_id: str) -> str | None:
        """app.hubspot.com link for a company, or None when no portal id is
        configured."""
        if not self._portal_id:
            return None
        return (f"https://app.hubspot.com/contacts/{self._portal_id}"
                f"/record/0-2/{company_id}")

    def close(self) -> None:
        self._client.close()
