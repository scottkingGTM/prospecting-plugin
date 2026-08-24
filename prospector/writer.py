"""The commit write-pipeline: the ONLY path from a rep's click to a HubSpot
write.

The design follows a guarded-write pipeline we've used before: the steps are
replicated one for one and enforced server-side, so a mis-built panel (or a
mis-prompted caller) cannot skip a step:

    confirm/echo -> idempotency -> cap -> [AUDIT attempt] -> [DRY_RUN stop]
        -> side effect -> [AUDIT outcome]

AUDIT-BEFORE-ACTION INVARIANT: the 'attempt' events row is written BEFORE the
side effect. If that insert throws, commit() aborts and the side effect never
runs -- the audit log can therefore never miss a real write. prospector.events
is append-only, so the attempt and the outcome are two separate immutable
rows: the attempt row stays 'attempt' forever and the outcome lands as its own
'done' / 'failed' / 'rejected' row. The idempotency index (UNIQUE(rep_id,
action, idempotency_key) WHERE status='done') matches only the outcome row,
which is exactly right -- a failed attempt never burns the key.

Two deliberate choices:

  * Guard holds precede the audit attempt. A blocking hold means NOTHING was
    tried -- there is no intent-to-write to audit, only a refusal, and the
    refusal is visible to the rep in the 422 body. (Cap rejections DO write a
    'blocked_cap' row, mirroring the guarded-write pipeline's rejected-phase
    audit, because a cap rejection is evidence of demand the caps report needs
    to see.)

  * Phone lands on the 'phone' property, hardcoded. By design:
    guards.phone_field_guard exists for CALLERS that pass a property name
    around; this writer never lets a property name travel through the wire
    contract at all, so the guard has nothing to catch here.

Sibling modules (guards, and owners' resolve_owner) are imported lazily
inside the functions that use them, the house pattern from jobs.py: tests
can inject fakes via sys.modules, and an import problem in a sibling
surfaces on the write path instead of taking down read-only routes.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .filters import extract_domain
from .hubspot import HubSpotError
from .matching import norm_linkedin
from .owners import TRIAGE_SENTINEL, normalize_state

logger = logging.getLogger(__name__)

# Every string field in the wire contract is capped at this length.
_MAX_LEN = 200

# Format-lite: something@something.tld. Deliverability is the providers'
# problem; this only rejects strings that cannot be an address at all.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# tier_1 is a plain dropdown option like the others -- see _check_caps'
# docstring for the ruling that put it here.
_ALLOWED_TIERS = ("tier_1", "tier_2", "tier_3")

# Daily LIVE commit ceiling per rep -- a blast-radius default mirroring a
# promote-cap pattern (from a hardening pass): one runaway rep (or a
# runaway panel loop) can create at most this many contacts/companies in a
# UTC day. Module constant on purpose; becomes a prospector.reps column if
# it ever needs per-rep tuning. Dry-run commits never consume it.
DAILY_COMMIT_CAP = 50

# Rep-facing success line -- the panel shows it verbatim.
DONE_MESSAGE = "Created in HubSpot."

# -- SQL ---------------------------------------------------------------------

_INSERT_EVENT_SQL = """
    INSERT INTO prospector.events
        (rep_id, action, status, target, idempotency_key, reason,
         cost_credits, dry_run, detail)
    VALUES (%(rep_id)s, %(action)s, %(status)s, %(target)s,
            %(idempotency_key)s, %(reason)s, %(cost_credits)s,
            %(dry_run)s, %(detail)s);
"""

# The idempotency lookup: the partial unique index events_idem guarantees at
# most one LIVE 'done' row per (rep, action, key), so this returns 0 or 1
# rows. NOT dry_run is load-bearing (from a hardening pass): a
# dry-run rehearsal must never satisfy a LIVE commit's replay lookup -- the
# dry-run-confirm -> flip-live -> re-confirm-the-same-flow sequence must
# WRITE, not replay the rehearsal report.
_IDEM_SQL = """
    SELECT detail
      FROM prospector.events
     WHERE rep_id = %(rep_id)s
       AND action = %(action)s
       AND idempotency_key = %(key)s
       AND status = 'done'
       AND NOT dry_run
     LIMIT 1;
"""

# Promotions consumed today (UTC day, house convention). Only LIVE
# promotions count -- dry-run rows rehearse, they never spend the cap.
_PROMOTE_COUNT_SQL = """
    SELECT count(*) AS n
      FROM prospector.events
     WHERE rep_id = %(rep_id)s
       AND action = 'promote_t2'
       AND status = 'done'
       AND NOT dry_run
       AND (created_at AT TIME ZONE 'UTC')::date
           = (now() AT TIME ZONE 'UTC')::date;
"""

# LIVE commits consumed today (UTC day, house convention) -- the
# DAILY_COMMIT_CAP counter. Dry-run rehearsals never consume the cap.
_COMMIT_COUNT_SQL = """
    SELECT count(*) AS n
      FROM prospector.events
     WHERE rep_id = %(rep_id)s
       AND action = 'commit'
       AND status = 'done'
       AND NOT dry_run
       AND (created_at AT TIME ZONE 'UTC')::date
           = (now() AT TIME ZONE 'UTC')::date;
"""

# Owner display name for the plan/note -- reps read names, not raw ids. One
# cheap lookup covering every enrolled rep; the owners API is the fallback
# for owners outside the roster. hubspot_owner_id is text in the schema.
_REP_NAME_SQL = """
    SELECT display_name
      FROM prospector.reps
     WHERE hubspot_owner_id = %(owner_id)s
     LIMIT 1;
"""


class CommitRejected(Exception):
    """The pipeline refused (or could not complete) a commit.

    `http_status` maps straight to the route's response code, `code` is the
    machine-readable reason, `detail` the JSON body payload. The message is
    code-only on purpose -- str(exc) can end up in logs and must never carry
    rep-typed PII.
    """

    def __init__(self, http_status: int, code: str,
                 detail: dict | None = None) -> None:
        self.http_status = int(http_status)
        self.code = str(code)
        self.detail = dict(detail or {})
        super().__init__(f"{self.code} ({self.http_status})")


# -- validation (pipeline step 1: echo) ---------------------------------------


def _validate(body: Any) -> dict:
    """Echo/validate the wire body into a normalized intent dict.

    Collects EVERY problem and raises one CommitRejected(400, 'validation')
    listing all of them (the config.py fail-fast convention), so the panel
    shows the whole punch list instead of one field at a time.
    """
    if not isinstance(body, dict):
        raise CommitRejected(400, "validation",
                             {"errors": ["body must be a JSON object"]})
    errors: list[str] = []

    def clean_str(value: Any, field: str, *, required: bool = False) -> str:
        if value is None:
            value = ""
        if not isinstance(value, str):
            errors.append(f"{field} must be a string")
            return ""
        value = value.strip()
        if required and not value:
            errors.append(f"{field} is required")
        if len(value) > _MAX_LEN:
            errors.append(f"{field} is longer than {_MAX_LEN} characters")
        return value

    def clean_bool(value: Any, field: str, default: bool = False) -> bool:
        if value is None:
            return default
        if not isinstance(value, bool):
            errors.append(f"{field} must be a boolean")
            return default
        return value

    key = clean_str(body.get("idempotency_key"), "idempotency_key",
                    required=True)
    confirm = clean_bool(body.get("confirm"), "confirm")

    # ---- the one-click backfill variant -------------------------------------
    link = body.get("link_linkedin")
    if link is not None:
        if not isinstance(link, dict):
            errors.append("link_linkedin must be an object")
            link = {}
        contact_id = clean_str(link.get("hs_contact_id"),
                               "link_linkedin.hs_contact_id", required=True)
        raw_url = clean_str(link.get("linkedin_url"),
                            "link_linkedin.linkedin_url", required=True)
        norm_url = norm_linkedin(raw_url)
        if raw_url and not norm_url:
            errors.append("link_linkedin.linkedin_url is not a usable "
                          "LinkedIn URL")
        if errors:
            raise CommitRejected(400, "validation", {"errors": errors})
        return {
            "variant": "link_linkedin",
            "action": "link_linkedin",
            "idempotency_key": key,
            "confirm": confirm,
            "hs_contact_id": contact_id,
            "linkedin_url": norm_url,
            "tier": None,
            "target_account": False,
            "target": {"hs_contact_id": contact_id, "linkedin_url": norm_url},
        }

    # ---- the full commit variant --------------------------------------------
    contact_in = body.get("contact")
    if not isinstance(contact_in, dict):
        errors.append("contact must be an object")
        contact_in = {}
    first = clean_str(contact_in.get("first_name"), "contact.first_name",
                      required=True)
    last = clean_str(contact_in.get("last_name"), "contact.last_name",
                     required=True)
    # Email is OPTIONAL (from a hardening pass): HubSpot allows
    # contacts keyed on name, the panel's email picker can legitimately be
    # empty, and the guards already handle email-less (no email = no
    # auto-create risk). Present-but-malformed is still a 400.
    email = clean_str(contact_in.get("email"), "contact.email").lower()
    if email and not _EMAIL_RE.match(email):
        errors.append("contact.email is not a valid email address")
    jobtitle = clean_str(contact_in.get("jobtitle"), "contact.jobtitle")
    phone = clean_str(contact_in.get("phone"), "contact.phone")
    email_status = clean_str(contact_in.get("email_status"),
                             "contact.email_status")
    raw_li = clean_str(contact_in.get("linkedin_url"), "contact.linkedin_url")
    linkedin_url = norm_linkedin(raw_li)
    if raw_li and not linkedin_url:
        errors.append("contact.linkedin_url is not a usable LinkedIn URL")

    company_in = body.get("company")
    company_id = ""
    new_company: dict | None = None
    if not isinstance(company_in, dict):
        errors.append("company must be an object")
    else:
        has_id = bool(company_in.get("hs_company_id"))
        has_new = isinstance(company_in.get("new"), dict)
        if has_id == has_new:
            errors.append("company must carry exactly one of hs_company_id "
                          "or new")
        if has_id:
            company_id = clean_str(company_in.get("hs_company_id"),
                                   "company.hs_company_id", required=True)
        if has_new:
            new_in = company_in["new"]
            name = clean_str(new_in.get("name"), "company.new.name",
                             required=True)
            raw_domain = clean_str(new_in.get("domain"), "company.new.domain",
                                   required=True)
            domain = extract_domain(raw_domain)
            if raw_domain and not domain:
                errors.append("company.new.domain is not a usable domain")
            state_raw = clean_str(new_in.get("state"), "company.new.state")
            state_code = normalize_state(state_raw)
            # An unnormalizable state is NOT a 400: owner resolution routes
            # it to triage rather than guessing (owners.py contract).
            company_li = clean_str(new_in.get("linkedin_url"),
                                   "company.new.linkedin_url")
            new_company = {
                "name": name,
                "domain": domain,
                "state": state_raw,
                "state_code": state_code,
                "linkedin_url": company_li,
            }

    tier = body.get("tier")
    if tier is not None and tier not in _ALLOWED_TIERS:
        errors.append(f"tier must be one of {list(_ALLOWED_TIERS)} or null")
        tier = None
    target_account = clean_bool(body.get("target_account"), "target_account")
    # A "reason" key from a stale client is accepted and IGNORED -- never
    # validated, never a 400 (the reason field was removed from the create
    # flow by a later design decision; see _check_caps).
    alternate_confirmed = clean_bool(body.get("alternate_domain_confirmed"),
                                     "alternate_domain_confirmed")

    if errors:
        raise CommitRejected(400, "validation", {"errors": errors})

    return {
        "variant": "commit",
        "action": "commit",
        "idempotency_key": key,
        "confirm": confirm,
        "contact": {
            "first_name": first,
            "last_name": last,
            "jobtitle": jobtitle,
            "email": email,
            "email_status": email_status,
            "phone": phone,
            "linkedin_url": linkedin_url,
        },
        "company_id": company_id,
        "new": new_company,
        "tier": tier,
        "target_account": target_account,
        "alternate_confirmed": alternate_confirmed,
        # Passthrough: which provider returned what + cost, straight from
        # the panel; rendered into the note, never interpreted.
        "provenance": body.get("provenance"),
        "target": {
            "email": email,
            "company": company_id or (new_company or {}).get("domain"),
        },
    }


# -- events (the audit trail) --------------------------------------------------


def _insert_event(db: Any, rep: Any, action: str, status: str, target: dict,
                  detail: dict, dry_run: bool, *,
                  idempotency_key: str | None = None,
                  reason: str | None = None) -> None:
    """Append one events row. Raises on failure -- callers on the
    audit-attempt path MUST let that propagate (the invariant); callers on
    rejection/outcome paths use _safe_insert_event instead."""
    with db.cursor() as cur:
        cur.execute(_INSERT_EVENT_SQL, {
            "rep_id": rep.id,
            "action": action,
            "status": status,
            "target": json.dumps(target),
            "idempotency_key": idempotency_key,
            "reason": reason or None,
            "cost_credits": None,  # commits spend no enrichment credits
            "dry_run": bool(dry_run),
            "detail": json.dumps(detail),
        })


def _safe_insert_event(db: Any, rep: Any, action: str, status: str,
                       target: dict, detail: dict, dry_run: bool, *,
                       idempotency_key: str | None = None,
                       reason: str | None = None) -> None:
    """Safe-audit pattern from the guarded-write pipeline: an audit hiccup on
    a REJECTION or outcome path must never mask the actual result -- log it
    and move on."""
    try:
        _insert_event(db, rep, action, status, target, detail, dry_run,
                      idempotency_key=idempotency_key, reason=reason)
    except Exception:
        logger.error("events insert failed on a %s/%s path (non-fatal)",
                     action, status, exc_info=True)


def _idempotent_replay(db: Any, rep: Any, action: str, key: str) -> dict | None:
    """Prior 'done' outcome for this (rep, action, key), or None. psycopg2
    hands jsonb back as a dict; a stub may hand back the raw JSON string."""
    rows = db.query(_IDEM_SQL, {"rep_id": rep.id, "action": action,
                                "key": key})
    if not rows:
        return None
    detail = rows[0].get("detail")
    if isinstance(detail, str):
        detail = json.loads(detail)
    return dict(detail or {})


# -- caps (pipeline step 3) ----------------------------------------------------


def _check_caps(db: Any, rep: Any, cfg: Any, intent: dict) -> None:
    """The daily commit cap + the daily promote cap. Both caps count LIVE
    done rows only (the guarded-write pipeline counts non-dry-run done rows
    the same way), so dry-run rehearsals never consume either. NOTE the
    deliberate asymmetry: a tier_2 or tier_3 commit is gated by the promote
    cap, but only a tier_2 commit writes the 'promote_t2' row that consumes
    it -- a tier_3 tag is bookkeeping, not a promotion.

    TIER_1 HANDLING: an earlier gated T1 path was consciously replaced by a
    plain dropdown, no gates; re-add as config if the T1 book balloons.
    Concretely: tier_1 needs no reason, is never checked against (and never
    consumes) the promote cap, and has no cap of its own -- a live tier_1
    commit only writes an audit-only 'promote_t1' done row (v_usage counts
    it). The reason field itself was removed from the create flow by the same
    decision."""
    if intent["variant"] != "commit":
        return
    # Daily commit cap (from a hardening pass): blast-radius
    # limiter on contact/company creation itself, promote-cap pattern.
    rows = db.query(_COMMIT_COUNT_SQL, {"rep_id": rep.id})
    commits_used = int(rows[0]["n"]) if rows else 0
    if commits_used >= DAILY_COMMIT_CAP:
        _safe_insert_event(
            db, rep, "blocked_cap", "rejected", intent["target"],
            {"cap": DAILY_COMMIT_CAP, "used": commits_used,
             "blocked_action": "commit"},
            cfg.dry_run,
        )
        raise CommitRejected(402, "daily_commit_cap",
                             {"used": commits_used,
                              "cap": DAILY_COMMIT_CAP})
    # tier_1 is deliberately absent here -- no gates, no cap (see the
    # docstring's TIER_1 HANDLING note).
    if intent["tier"] in ("tier_2", "tier_3"):
        rows = db.query(_PROMOTE_COUNT_SQL, {"rep_id": rep.id})
        used = int(rows[0]["n"]) if rows else 0
        cap = int(rep.daily_promote_cap)
        if used >= cap:
            _safe_insert_event(
                db, rep, "blocked_cap", "rejected", intent["target"],
                {"cap": cap, "used": used, "blocked_action": "commit",
                 "tier": intent["tier"]},
                cfg.dry_run,
            )
            raise CommitRejected(402, "daily_promote_cap",
                                 {"used": used, "cap": cap})


# -- guards (pipeline step 4) ----------------------------------------------------


def _hold_dict(hold: Any) -> dict:
    return {
        "code": getattr(hold, "code", ""),
        "blocking": bool(getattr(hold, "blocking", False)),
        "message": getattr(hold, "message", ""),
        "detail": getattr(hold, "detail", None),
    }


def _collect_holds(hubspot: Any, intent: dict) -> list:
    """All guard holds for this intent, blocking or not.

    commit variant: guards.collect_commit_holds over the contact's email
    facts. company_domain is only known for a NEW company -- for an existing
    hs_company_id the domain is deliberately NOT pre-fetched here (guards
    run before the audit attempt; the company read belongs to the verified
    side-effect stage), so the domain-mismatch guard simply has nothing to
    compare against and the existing record's identity stands as the rep
    chose it.

    link_linkedin variant: LIVE re-read of the contact (never the panel's
    cached copy), then linkedin_link_guard against what HubSpot holds now.
    """
    from . import guards  # lazy on purpose -- see module docstring

    if intent["variant"] == "link_linkedin":
        contact = hubspot.get_contact(intent["hs_contact_id"])
        if contact is None:
            raise CommitRejected(404, "contact_not_found",
                                 {"hs_contact_id": intent["hs_contact_id"]})
        hold = guards.linkedin_link_guard(
            contact.get("hs_linkedin_url"), intent["linkedin_url"])
        return [hold] if hold is not None else []

    domain = intent["new"]["domain"] if intent["new"] else None
    return list(guards.collect_commit_holds(
        email=intent["contact"]["email"],
        email_status=intent["contact"]["email_status"],
        company_domain=domain,
        alternate_confirmed=intent["alternate_confirmed"],
    ) or [])


# -- the plan (reads only; shared by preview, dry-run, and live) ----------------


def _build_plan(db: Any, hubspot: Any, rep: Any, intent: dict) -> dict:
    """Everything the side effect WOULD do, computed from reads alone.

    Raises the verification 409s (company_id_stale / company_appeared /
    contact_exists) -- they are read-time facts, so preview and dry-run
    surface them exactly as a live commit would.
    """
    if intent["variant"] == "link_linkedin":
        return {
            "contact_id": intent["hs_contact_id"],
            "contact_props": {"hs_linkedin_url": intent["linkedin_url"]},
        }

    company_id = intent["company_id"]
    new = intent["new"]
    company_props: dict | None = None
    company_owner = ""
    # Rep-facing identity of the EXISTING company, straight off the live
    # batch-read verify (already paid for) -- the preview must show which
    # company the contact lands on (a pilot review found this was needed).
    company_name = ""
    company_domain = ""
    # The tier this plan will actually write -- may drop to None below when
    # the existing company already carries the same value (a no-op PATCH is
    # not a promotion and must not consume the promote ledger).
    effective_tier = intent["tier"]

    if company_id:
        # Existing company: live batch-read verify. An id absent from the
        # response is stale or merged-away (companies_batch_read docstring)
        # -- writing against it would land the contact on a ghost.
        found = hubspot.companies_batch_read([company_id])
        record = found.get(str(company_id))
        if record is None:
            raise CommitRejected(409, "company_id_stale",
                                 {"hs_company_id": company_id})
        company_owner = str(record.get("hubspot_owner_id") or "")
        company_name = str(record.get("name") or "")
        company_domain = str(record.get("domain") or "")
        # Tier-demotion guard: a company that ALREADY carries an ICP tier
        # never has it changed by this pipeline -- silently demoting a
        # tier_1 company to tier_2 on a contact-create click would undo a
        # deliberate prioritization. Same value -> no-op (don't PATCH tier);
        # empty current tier -> setting one is allowed.
        current_tier = str(record.get("hs_ideal_customer_profile")
                           or "").strip()
        if intent["tier"] and current_tier:
            if current_tier != intent["tier"]:
                raise CommitRejected(422, "tier_change_blocked", {
                    "current": current_tier,
                    "requested": intent["tier"],
                    "message": "this company already has an ICP tier -- "
                               "change it in HubSpot directly, not by "
                               "creating a contact",
                })
            effective_tier = None  # same value: nothing to write
    else:
        # New company: live domain re-check FIRST. A company that appeared
        # since resolve ran means someone else created it -- hand its id
        # back instead of minting a duplicate.
        existing = hubspot.find_companies_by_domain(new["domain"])
        if existing:
            raise CommitRejected(409, "company_appeared", {
                "hs_company_id": existing[0].get("id"),
                "matches": len(existing),
            })
        company_props = {"name": new["name"], "domain": new["domain"]}
        if new["state_code"]:
            company_props["state"] = new["state_code"]  # 2-letter, always
        if new["linkedin_url"]:
            company_props["linkedin_company_page"] = new["linkedin_url"]

    # Live email re-check: the resolve-time answer is stale by definition.
    # Skipped entirely for an email-less commit (from a hardening pass) --
    # there is no address to collide on, and the contact is created from its
    # LinkedIn identity alone.
    if intent["contact"]["email"]:
        hits = hubspot.find_contacts_by_emails([intent["contact"]["email"]])
        if hits:
            raise CommitRejected(409, "contact_exists", {
                "hs_contact_id": hits[0].get("id"),
                "matched_email": hits[0].get("matched_email"),
            })

    owner_id, owner_source, owner_why = _resolve_owner(
        rep, company_id, company_owner)
    needs_triage = owner_id == TRIAGE_SENTINEL
    # Human name for the resolved owner (a pilot review found reps read
    # names, not raw HubSpot ids). Best-effort -- never fails a
    # preview or commit; falls back to the raw id string.
    owner_name = (None if needs_triage
                  else _owner_display_name(db, hubspot, owner_id))

    contact = intent["contact"]
    contact_props: dict = {
        "firstname": contact["first_name"],
        "lastname": contact["last_name"],
    }
    if contact["email"]:
        # Optional (from a hardening pass): HubSpot allows
        # contacts keyed on name alone; an email-less create carries no
        # email property at all rather than an empty string.
        contact_props["email"] = contact["email"]
    if contact["jobtitle"]:
        contact_props["jobtitle"] = contact["jobtitle"]
    if contact["phone"]:
        # Hardcoded 'phone' -- by design (see module docstring):
        # guards.phone_field_guard protects callers that pass a property
        # name; this writer never lets one travel, so there is nothing for
        # the guard to catch.
        contact_props["phone"] = contact["phone"]
    if contact["linkedin_url"]:
        contact_props["hs_linkedin_url"] = contact["linkedin_url"]
    if not needs_triage:
        contact_props["hubspot_owner_id"] = owner_id
    # else: created UNOWNED on purpose (by design) -- never silently the
    # clicking rep; the needs_triage flag makes the gap queryable.

    company_update_props: dict = {}
    if effective_tier:
        company_update_props["hs_ideal_customer_profile"] = effective_tier
    if intent["target_account"]:
        company_update_props["hs_is_target_account"] = "true"

    plan: dict = {
        "contact_props": contact_props,
        "owner": {"id": None if needs_triage else owner_id,
                  # Rep-facing display name (from a pilot fix); the
                  # raw id when no name could be resolved, None on triage.
                  "name": owner_name,
                  "source": owner_source,
                  # Rep-facing: WHY no rule matched (a pilot review found
                  # a bare TRIAGE badge just makes reps ask).
                  "why": owner_why},
        "needs_triage": needs_triage,
        # The EFFECTIVE tier (None on a same-value no-op) -- commit's
        # promote-ledger row keys off this, so a no-op never consumes the
        # promote cap.
        "tier": effective_tier,
        "target_account": intent["target_account"],
        "company_update_props": company_update_props,
        "note_preview": _note_body(rep, intent, owner_id, owner_name,
                                   owner_source, needs_triage),
    }
    if company_id:
        plan["company_id"] = str(company_id)
        # Identity for the preview's COMPANY section, when the live verify
        # had it in hand (from a pilot fix) -- no extra read.
        if company_name:
            plan["company_name"] = company_name
        if company_domain:
            plan["company_domain"] = company_domain
    else:
        plan["company_props"] = company_props
        # Rep-facing copy of the create payload (from a pilot fix):
        # the preview/dry-run report must SHOW the company that would be
        # created -- {name, domain, state (2-letter), linkedin_company_page?}.
        plan["company_new"] = dict(company_props or {})
    return plan


def _resolve_owner(rep: Any, company_id: str,
                   company_owner: str) -> tuple[str, str, str | None]:
    """Owner priority: a contact inherits the EXISTING company's current
    owner when there is one; otherwise it is assigned to the committing
    rep; otherwise it routes to triage (never silently to a default rep).

    owners.resolve_owner is imported lazily so tests can stub the module.
    Swap owners.resolve_owner for your own routing (territory, round-robin,
    a CRM lookup) -- see prospector/owners.py."""
    from . import owners  # lazy on purpose -- see module docstring

    if company_id and str(company_owner or "").strip():
        return str(company_owner).strip(), "company_owner", None
    owner_id, source = owners.resolve_owner(rep.hubspot_owner_id)
    return owner_id, source, (_triage_why() if source == "triage" else None)


def _owner_display_name(db: Any, hubspot: Any, owner_id: Any) -> str | None:
    """Best-effort human name for a HubSpot owner id (from a pilot fix: the
    preview showed raw owner ids).

    Resolution order: (a) prospector.reps.display_name -- one query, covers
    every enrolled rep; (b) the HubSpot owners API (firstName + lastName);
    (c) the raw id string. Never raises -- a name lookup must never fail a
    preview or a commit. One call per plan build, so no cache is needed.
    """
    if not owner_id:
        return None
    owner_id = str(owner_id)
    try:
        rows = db.query(_REP_NAME_SQL, {"owner_id": owner_id})
        name = str((rows[0].get("display_name") or "")).strip() if rows else ""
        if name:
            return name
    except Exception:
        logger.warning("reps display_name lookup failed for owner %s "
                       "(non-fatal)", owner_id, exc_info=True)
    try:
        owner = hubspot.get_owner(owner_id)
        if owner:
            name = " ".join(
                part for part in (owner.get("firstName"),
                                  owner.get("lastName"))
                if part and str(part).strip()
            ).strip()
            if name:
                return name
    except Exception:
        logger.warning("HubSpot owner lookup failed for owner %s "
                       "(non-fatal)", owner_id, exc_info=True)
    return owner_id


def _triage_why() -> str:
    """One rep-facing sentence explaining WHY ownership could not be
    assigned automatically (a bare TRIAGE badge just makes reps ask)."""
    return ("The rep has no HubSpot owner id on file, so ownership can't be "
            "assigned automatically -- set hubspot_owner_id on the rep in "
            "prospector.reps, or route it in your CRM.")


# -- the note -------------------------------------------------------------------


def _esc(value: Any) -> str:
    """Escape &, <, > in text bound for hs_note_body. HubSpot renders note
    bodies as rich text, so an unescaped '<b>' -- or a crafted tag -- in a
    vendor-supplied or panel-derived string would render as markup
    instead of text (from a hardening pass). Applied to EVERY
    user/vendor-derived string interpolated into the note; & first so
    entities are not double-escaped."""
    return (str(value).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _provenance_lines(provenance: Any) -> list[str]:
    """Render the panel's provenance passthrough (which provider returned
    what, at what cost) as plain-text bullet lines. Tolerant of any shape:
    dicts get the known keys pulled out, anything else is str()'d. Every
    value is vendor/panel-derived, so every value is _esc()'d."""
    if not isinstance(provenance, list):
        return []
    lines: list[str] = []
    for item in provenance[:20]:
        if isinstance(item, dict):
            provider = _esc(item.get("provider")
                            or item.get("provider_id") or "unknown")
            field = _esc(item.get("field") or "")
            status = _esc(item.get("status") or item.get("result") or "")
            cost = item.get("cost", item.get("cost_credits"))
            line = f"  - {provider}"
            if field:
                line += f" {field}"
            if status:
                line += f": {status}"
            if cost is not None:
                line += f" (cost {_esc(cost)})"
            lines.append(line)
        else:
            lines.append(f"  - {_esc(str(item)[:_MAX_LEN])}")
    return lines


def _note_body(rep: Any, intent: dict, owner_id: str,
               owner_name: str | None, owner_source: str,
               needs_triage: bool) -> str:
    """The audit note: who clicked, the LinkedIn URL, and provider
    provenance + cost. (No reason line -- the reason field was removed
    from the create flow by a later design decision.) Every
    user/vendor-derived string is _esc()'d -- HubSpot renders hs_note_body
    as rich text (from a hardening pass); the literal text of this
    function is the only markup-free surface."""
    lines = [f"Prospected via Prospecting Plugin by {_esc(rep.display_name)}."]
    if intent["contact"]["linkedin_url"]:
        lines.append(f"LinkedIn: {_esc(intent['contact']['linkedin_url'])}")
    if not intent["contact"]["email"]:
        lines.append("No email -- created from LinkedIn identity.")
    flags = []
    if intent["tier"]:
        flags.append(intent["tier"])
    if intent["target_account"]:
        flags.append("target account")
    if flags:
        lines.append("Flags: " + ", ".join(flags))
    if needs_triage:
        lines.append("Owner: UNRESOLVED -- routed to RevOps triage "
                     "(never silently assigned to the clicking rep).")
    else:
        # Name first, id in parens (from a pilot fix) -- unless the
        # name lookup fell all the way back to the raw id.
        shown = (f"{owner_name} ({owner_id})"
                 if owner_name and str(owner_name) != str(owner_id)
                 else str(owner_id))
        lines.append(f"Owner: {_esc(shown)} ({owner_source})")
    provenance = _provenance_lines(intent.get("provenance"))
    if provenance:
        lines.append("Enrichment provenance:")
        lines.extend(provenance)
    return "\n".join(lines)


# -- the side effect (live only) -------------------------------------------------


def _execute(hubspot: Any, intent: dict, plan: dict) -> dict:
    """The real HubSpot writes, in order: company (create if new) ->
    contact -> associate -> tier/target update -> note. Runs ONLY after the
    attempt row committed and ONLY when DRY_RUN is off."""
    if intent["variant"] == "link_linkedin":
        contact_id = str(plan["contact_id"])
        hubspot.update_contact(contact_id, plan["contact_props"])
        return {
            "contact_id": contact_id,
            "hubspot_url": hubspot.contact_hubspot_url(contact_id),
            "hs_linkedin_url": plan["contact_props"]["hs_linkedin_url"],
            "dry_run": False,
            "message": DONE_MESSAGE,
        }

    if "company_id" in plan:
        company_id = str(plan["company_id"])
    else:
        created = hubspot.create_company(plan["company_props"])
        company_id = str(created["id"])

    # The contact is BORN associated: company_id rides inline in the create
    # payload (from a hardening pass), so the portal's
    # company-auto-create setting never sees an unassociated contact and
    # cannot mint a junk company off a confirmed-alternate email domain in
    # the create->associate gap (the auto-created-junk-companies incident).
    contact = hubspot.create_contact(plan["contact_props"],
                                     company_id=company_id)
    contact_id = str(contact["id"])

    # Belt and suspenders: the explicit associate stays. It is idempotent
    # when the inline association already landed, and covers a HubSpot
    # regression in inline-create associations; since the association
    # already exists, a failure HERE never fails the commit -- log and
    # continue.
    try:
        hubspot.associate_contact_company(contact_id, company_id)
    except HubSpotError as exc:
        logger.warning(
            "explicit associate after inline-associated create failed "
            "(contact %s -> company %s): %s -- continuing; the inline "
            "association at create is authoritative",
            contact_id, company_id, exc,
        )

    if plan["company_update_props"]:
        hubspot.update_company(company_id, plan["company_update_props"])

    note_id = hubspot.create_note(plan["note_preview"], contact_id,
                                  company_id)

    return {
        "contact_id": contact_id,
        "company_id": company_id,
        "hubspot_url": hubspot.contact_hubspot_url(contact_id),
        "note_id": note_id,
        "dry_run": False,
        "owner": plan["owner"],
        "needs_triage": plan["needs_triage"],
        "tier": plan["tier"],
        "target_account": plan["target_account"],
        "message": DONE_MESSAGE,
    }


# -- public surface ---------------------------------------------------------------


def preview(db: Any, hubspot: Any, rep: Any, cfg: Any, body: dict) -> dict:
    """Everything commit would compute -- NO writes, NO audit rows.

    Reads are allowed (guard re-reads, company verify, owner resolution);
    the verification 409s raise here exactly as they would on commit.
    Guard holds are LISTED, not raised: showing the rep what would block is
    the entire point of a preview.

    Wire contract (pinned): confirm:false -> {"preview": {<plan>}}. The
    panel renders data.preview, so the plan MUST live under that key --
    an earlier shape ("preview": True with the plan under "would") put a
    boolean where the panel expected the plan and rendered "(empty
    preview)" (a pilot review caught this).
    """
    intent = _validate(body)
    holds = _collect_holds(hubspot, intent)
    plan = _build_plan(db, hubspot, rep, intent)
    return {
        "preview": plan,
        "dry_run": bool(cfg.dry_run),
        "holds": [_hold_dict(h) for h in holds],
    }


def commit(db: Any, hubspot: Any, rep: Any, cfg: Any, body: dict) -> dict:
    """The six-step guarded write (guarded-write pipeline lineage -- see the
    module docstring for the order and the audit-before-action invariant).
    """
    # 1. Validate/echo. Unconfirmed = preview, verbatim -- no audit rows.
    intent = _validate(body)
    if not intent["confirm"]:
        return preview(db, hubspot, rep, cfg, body)
    action = intent["action"]
    key = intent["idempotency_key"]

    # 2. Idempotency: a prior 'done' with this key short-circuits -- the
    #    stored outcome is returned and NOTHING runs again.
    stored = _idempotent_replay(db, rep, action, key)
    if stored is not None:
        stored["idempotent"] = True
        return stored

    # 3. Caps. A cap rejection writes a blocked_cap row.
    _check_caps(db, rep, cfg, intent)

    # 4. Guards. Blocking hold -> 422 BEFORE the audit attempt: nothing was
    #    tried, so there is no intent-to-write to audit (documented choice,
    #    module docstring).
    holds = _collect_holds(hubspot, intent)
    blocking = [h for h in holds if getattr(h, "blocking", False)]
    if blocking:
        first = blocking[0]
        raise CommitRejected(422, getattr(first, "code", "guard_hold"), {
            "message": getattr(first, "message", ""),
            "detail": getattr(first, "detail", None),
            "holds": [_hold_dict(h) for h in holds],
        })

    # 5. AUDIT attempt BEFORE any side effect. If this insert throws, the
    #    side effect never runs -- the audit-before-action invariant.
    try:
        _insert_event(db, rep, action, "attempt", intent["target"],
                      {"intent": _echo_of_intent(intent)}, cfg.dry_run,
                      idempotency_key=key)
    except Exception as exc:
        logger.error("audit-attempt insert failed -- side effect will NOT "
                     "run", exc_info=True)
        raise CommitRejected(500, "audit_write_failed",
                             {"error": type(exc).__name__}) from exc

    try:
        # Verified plan: company verify, email re-check, owner resolution.
        plan = _build_plan(db, hubspot, rep, intent)

        # 6. DRY_RUN stop: the full would-do report is the outcome. THE
        #    REAL WRITE NEVER RUNS IN DRY-RUN -- cfg.dry_run is env-pinned
        #    at boot (config.py); nothing in the body can flip it.
        if cfg.dry_run:
            report = {"dry_run": True, "would": plan}
            _insert_event(db, rep, action, "done", intent["target"], report,
                          True, idempotency_key=key)
            return report

        # 7. The side effect.
        result = _execute(hubspot, intent, plan)
    except CommitRejected as exc:
        # A verification 409 after the attempt row: record the refusal as a
        # 'rejected' row (never 'done' -- the idempotency key is not
        # burned; the partial index only matches done rows).
        _safe_insert_event(db, rep, action, "rejected", intent["target"],
                           {"code": exc.code, **exc.detail}, cfg.dry_run)
        raise
    except Exception as exc:
        # Side-effect failure AFTER the attempt row: 'failed' row with SAFE
        # error text -- str() of our own HubSpotError only (built from
        # status/method/path, never the token); any other exception type is
        # reduced to its class name (jobs.py convention).
        message = (str(exc) if isinstance(exc, HubSpotError)
                   else f"internal: {type(exc).__name__}")
        _safe_insert_event(db, rep, action, "failed", intent["target"],
                           {"error": message}, cfg.dry_run)
        raise CommitRejected(502, "hubspot_write_failed",
                             {"error": message}) from exc

    # 8. AUDIT outcome. events is append-only, so the outcome is its own
    #    'done' row (the attempt row stays 'attempt' forever); the
    #    idempotency index matches exactly this row. If THIS insert fails
    #    the write already happened -- log loudly, flag the response, never
    #    pretend the write failed.
    try:
        _insert_event(db, rep, action, "done", intent["target"], result,
                      False, idempotency_key=key)
    except Exception:
        logger.error("audit-outcome insert failed AFTER a live write "
                     "(contact/company created; idempotency key %r not "
                     "recorded)", key, exc_info=True)
        result = dict(result)
        result["audit_warning"] = "outcome event write failed"

    # The promote ledger rows. promote_t2 is the row the daily promote cap
    # counts (live tier_2 only -- dry-run rehearsals and tier_3 tags never
    # consume the cap). promote_t1 is TELEMETRY, not a gate: an audit-only
    # done row (v_usage counts promotes_t1) that no cap ever reads --
    # a later design decision kept the log and dropped the gates. Both
    # key off the plan's EFFECTIVE tier, not the request: a same-value
    # tier no-op (F1's tier guard) wrote nothing and logs nothing.
    if action == "commit" and plan["tier"] in ("tier_1", "tier_2"):
        promote_action = ("promote_t1" if plan["tier"] == "tier_1"
                          else "promote_t2")
        _safe_insert_event(db, rep, promote_action, "done", intent["target"],
                           {"tier": plan["tier"], "commit_key": key}, False)
    return result


def _echo_of_intent(intent: dict) -> dict:
    """What the attempt row records: the normalized intent, minus bulky
    passthrough (provenance lands in the note, not the audit row)."""
    echo = {k: v for k, v in intent.items()
            if k not in ("provenance", "confirm")}
    return echo
