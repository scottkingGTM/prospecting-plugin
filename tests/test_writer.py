"""Unit tests for prospector.writer -- the commit write-pipeline.

No live network or DB. The Database is a StubDb routing the writer's four
SQL shapes (event insert, idempotency lookup, promote count, active roster)
plus owners.py's territory lookup against in-memory state; HubSpot is a
call-recording StubHubSpot; the guards module -- being written concurrently
against a pinned interface -- is a fake injected via sys.modules AND as a
package attribute, which covers both resolution paths of the writer's lazy
`from . import guards`.

StubDb and StubHubSpot share ONE ordered log so the write pipeline
order (attempt-event before any HubSpot touch, done-event after the last)
is asserted directly, not inferred.
"""

from __future__ import annotations

import json
import sys
import types
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import prospector
from prospector.hubspot import HubSpotError
from prospector.writer import (
    DAILY_COMMIT_CAP,
    DONE_MESSAGE,
    CommitRejected,
    commit,
    preview,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubCursor:
    """The writer only ever uses a cursor to INSERT events rows."""

    def __init__(self, db: "StubDb") -> None:
        self.db = db

    def execute(self, sql: str, params=None) -> None:
        assert "INSERT INTO prospector.events" in sql, f"unexpected SQL: {sql}"
        if self.db.fail_event_insert:
            raise RuntimeError("db down")
        event = dict(params)
        event["target"] = json.loads(event["target"])
        event["detail"] = json.loads(event["detail"])
        self.db.events.append(event)
        self.db.log.append(f"event:{event['status']}")


class StubDb:
    def __init__(self, log: list | None = None) -> None:
        self.log = log if log is not None else []
        self.events: list[dict] = []
        self.territory: dict[str, str] = {}
        self.active_owner_rows: list[dict] = [{"hubspot_owner_id": "901"}]
        # Enrolled-rep display names for the owner-name lookup:
        # hubspot_owner_id -> display_name.
        self.rep_names: dict[str, str] = {"901": "TJ"}
        self.fail_event_insert = False
        self.fail_name_lookup = False

    def query(self, sql: str, params=None) -> list[dict]:
        if "prospector.gtm_territory" in sql:
            owner = self.territory.get(params[0])
            return [{"hubspot_owner_id": owner}] if owner else []
        if "display_name" in sql:
            # The owner-name lookup -- routed BEFORE the roster query
            # (both mention prospector.reps).
            if self.fail_name_lookup:
                raise RuntimeError("db down")
            name = self.rep_names.get(str(params["owner_id"]))
            return [{"display_name": name}] if name else []
        if "prospector.reps" in sql:
            return list(self.active_owner_rows)
        if "action = 'promote_t2'" in sql:
            n = sum(1 for e in self.events
                    if e["action"] == "promote_t2" and e["status"] == "done"
                    and not e["dry_run"])
            return [{"n": n}]
        if "action = 'commit'" in sql:
            # The daily commit cap counter: LIVE done commits only.
            n = sum(1 for e in self.events
                    if e["action"] == "commit" and e["status"] == "done"
                    and not e["dry_run"])
            return [{"n": n}]
        if "prospector.events" in sql:
            for e in self.events:
                # Mirrors _IDEM_SQL's AND NOT dry_run: a dry-run rehearsal
                # never satisfies a replay lookup.
                if (e["rep_id"] == params["rep_id"]
                        and e["action"] == params["action"]
                        and e["idempotency_key"] == params["key"]
                        and e["status"] == "done"
                        and not e["dry_run"]):
                    return [{"detail": e["detail"]}]
            return []
        raise AssertionError(f"unrouted SQL: {sql}")

    @contextmanager
    def cursor(self):
        yield StubCursor(self)

    def events_by_status(self) -> list[str]:
        return [e["status"] for e in self.events]


class StubHubSpot:
    """Records every call in order; write methods are the set the DRY_RUN
    and preview tests assert stayed at zero."""

    WRITES = frozenset({
        "create_contact", "create_company", "update_contact",
        "update_company", "associate_contact_company", "create_note",
    })

    def __init__(self, log: list | None = None) -> None:
        self.log = log if log is not None else []
        self.calls: list[tuple] = []
        self.companies: dict[str, dict] = {}
        self.email_hits: list[dict] = []
        self.domain_hits: list[dict] = []
        self.closed_lost_owner: str | None = None
        self.contacts: dict[str, dict] = {}
        self.owners: dict[str, dict] = {}  # owners-API stub for the owner fallback
        self.get_owner_error: Exception | None = None
        self.create_contact_error: Exception | None = None
        self.associate_error: Exception | None = None

    def _rec(self, name: str, *args) -> None:
        self.calls.append((name,) + args)
        self.log.append(name)

    def write_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in self.WRITES]

    # -- reads --
    def companies_batch_read(self, ids):
        self._rec("companies_batch_read", tuple(ids))
        return {i: self.companies[i] for i in ids if i in self.companies}

    def find_companies_by_domain(self, domain):
        self._rec("find_companies_by_domain", domain)
        return list(self.domain_hits)

    def find_contacts_by_emails(self, emails):
        self._rec("find_contacts_by_emails", tuple(emails))
        return list(self.email_hits)

    def get_latest_closed_lost_owner(self, company_id):
        self._rec("get_latest_closed_lost_owner", company_id)
        return self.closed_lost_owner

    def get_contact(self, contact_id):
        self._rec("get_contact", contact_id)
        return self.contacts.get(contact_id)

    def get_owner(self, owner_id):
        self._rec("get_owner", owner_id)
        if self.get_owner_error is not None:
            raise self.get_owner_error
        return self.owners.get(str(owner_id))

    # -- writes --
    def create_company(self, props):
        self._rec("create_company", dict(props))
        return {"id": "777", **props}

    def create_contact(self, props, company_id=None):
        self._rec("create_contact", dict(props), company_id)
        if self.create_contact_error is not None:
            raise self.create_contact_error
        return {"id": "888", **props}

    def associate_contact_company(self, contact_id, company_id):
        self._rec("associate_contact_company", contact_id, company_id)
        if self.associate_error is not None:
            raise self.associate_error

    def update_company(self, company_id, props):
        self._rec("update_company", company_id, dict(props))
        return {"id": company_id, **props}

    def update_contact(self, contact_id, props):
        self._rec("update_contact", contact_id, dict(props))
        return {"id": contact_id, **props}

    def create_note(self, body_text, contact_id, company_id=None):
        self._rec("create_note", body_text, contact_id, company_id)
        return "note-1"

    # -- links --
    def contact_hubspot_url(self, contact_id):
        return f"https://app.hubspot.com/contacts/0/record/0-1/{contact_id}"

    def company_hubspot_url(self, company_id):
        return f"https://app.hubspot.com/contacts/0/record/0-2/{company_id}"


class FakeGuardHold:
    def __init__(self, code, blocking=True, message="", detail=None):
        self.code = code
        self.blocking = blocking
        self.message = message
        self.detail = detail or {}


@pytest.fixture
def fake_guards(monkeypatch):
    """Inject a fake prospector.guards (pinned sibling interface) via
    sys.modules AND the package attribute -- `from . import guards`
    resolves through the attribute when the real module was already
    imported by another test file, and through sys.modules otherwise."""
    mod = types.ModuleType("prospector.guards")
    mod.GuardHold = FakeGuardHold
    mod.commit_holds = []
    mod.commit_calls = []
    mod.link_hold = None
    mod.link_calls = []

    def collect_commit_holds(*, email, email_status, company_domain,
                             alternate_confirmed):
        mod.commit_calls.append({
            "email": email, "email_status": email_status,
            "company_domain": company_domain,
            "alternate_confirmed": alternate_confirmed,
        })
        return list(mod.commit_holds)

    def linkedin_link_guard(existing_url, new_url):
        mod.link_calls.append((existing_url, new_url))
        return mod.link_hold

    mod.collect_commit_holds = collect_commit_holds
    mod.linkedin_link_guard = linkedin_link_guard
    mod.phone_field_guard = lambda prop: None

    monkeypatch.setitem(sys.modules, "prospector.guards", mod)
    monkeypatch.setattr(prospector, "guards", mod, raising=False)
    return mod


@pytest.fixture
def env(fake_guards):
    log: list = []
    db = StubDb(log)
    hubspot = StubHubSpot(log)
    hubspot.companies["555"] = {
        "id": "555", "name": "Acme Services", "domain": "acme.com",
        "state": "Texas", "hubspot_owner_id": "",
    }
    db.territory = {"TX": "901"}
    rep = SimpleNamespace(
        id=1, email="tj@example.com", display_name="TJ",
        hubspot_owner_id="901", daily_promote_cap=25,
    )
    cfg = SimpleNamespace(dry_run=False)
    return SimpleNamespace(db=db, hubspot=hubspot, rep=rep, cfg=cfg,
                           log=log, guards=fake_guards)


def commit_body(**over) -> dict:
    body = {
        "idempotency_key": "key-1",
        "confirm": True,
        "contact": {
            "first_name": "Jane",
            "last_name": "Doe",
            "jobtitle": "COO",
            "email": "jane@acme.com",
            "email_status": "verified",
            "phone": "+1 555 0100",
            "linkedin_url": "https://www.linkedin.com/in/jane-doe/",
        },
        "company": {"hs_company_id": "555"},
        "tier": None,
        "target_account": False,
        "alternate_domain_confirmed": False,
    }
    body.update(over)
    return body


def new_company_body(**over) -> dict:
    body = commit_body()
    body["company"] = {"new": {
        "name": "Frost HVAC",
        "domain": "frosthvac.com",
        "state": "Alaska",
        "linkedin_url": "https://www.linkedin.com/company/frost-hvac/",
    }}
    body.update(over)
    return body


def link_body(**over) -> dict:
    body = {
        "idempotency_key": "link-1",
        "confirm": True,
        "link_linkedin": {
            "hs_contact_id": "888",
            "linkedin_url": "https://www.linkedin.com/in/jane-doe/",
        },
    }
    body.update(over)
    return body


def run_commit(env, body):
    return commit(env.db, env.hubspot, env.rep, env.cfg, body)


# ---------------------------------------------------------------------------
# preview / confirm echo
# ---------------------------------------------------------------------------


def test_preview_returns_would_do_without_writes_or_events(env):
    result = preview(env.db, env.hubspot, env.rep, env.cfg, commit_body())

    # The wire contract is {"preview": {<plan>}} -- the panel renders
    # data.preview, and the old shape (preview: True + the plan under
    # "would") rendered "(empty preview)".
    plan = result["preview"]
    assert isinstance(plan, dict)
    assert "would" not in result
    assert plan["contact_props"]["email"] == "jane@acme.com"
    assert plan["contact_props"]["phone"] == "+1 555 0100"
    assert plan["contact_props"]["hs_linkedin_url"] == "linkedin.com/in/jane-doe"
    assert plan["company_id"] == "555"
    assert plan["owner"] == {"id": "901", "name": "TJ",
                             "source": "rep", "why": None}
    assert "TJ" in plan["note_preview"]
    assert result["dry_run"] is False
    # NO writes, NO audit rows -- reads only.
    assert env.hubspot.write_calls() == []
    assert env.db.events == []


def test_commit_without_confirm_returns_preview_verbatim(env):
    body = commit_body(confirm=False)
    assert run_commit(env, body) == preview(
        env.db, env.hubspot, env.rep, env.cfg, body)
    assert env.db.events == []
    assert env.hubspot.write_calls() == []


def test_validation_collects_every_problem(env):
    body = commit_body(tier="tier_9")  # tier_1..tier_3 are all valid now
    # Email is OPTIONAL now -- absent is fine, malformed is not.
    body["contact"]["email"] = "not-an-address"
    body["contact"]["first_name"] = ""

    with pytest.raises(CommitRejected) as exc_info:
        run_commit(env, body)

    exc = exc_info.value
    assert (exc.http_status, exc.code) == (400, "validation")
    joined = " | ".join(exc.detail["errors"])
    assert "contact.email" in joined
    assert "contact.first_name" in joined
    assert "tier" in joined
    assert env.db.events == []


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_idempotent_replay_returns_stored_outcome_and_runs_nothing(env):
    stored = {"contact_id": "888", "company_id": "555",
              "message": DONE_MESSAGE}
    env.db.events.append({
        "rep_id": 1, "action": "commit", "status": "done",
        "idempotency_key": "key-1", "dry_run": False, "reason": None,
        "target": {}, "detail": stored,
    })

    result = run_commit(env, commit_body())

    assert result == {**stored, "idempotent": True}
    assert env.hubspot.calls == []
    assert len(env.db.events) == 1  # nothing new written


def test_dry_run_rehearsal_never_satisfies_a_live_replay(env):
    """The rehearsal-vs-live scenario: confirm in DRY_RUN
    (rehearsal), flip the deployment live, re-confirm the SAME flow with
    the SAME idempotency key -- it must WRITE for real, never replay the
    rehearsal report."""
    env.cfg.dry_run = True
    rehearsal = run_commit(env, commit_body())
    assert rehearsal["dry_run"] is True
    assert env.hubspot.write_calls() == []

    env.cfg.dry_run = False
    result = run_commit(env, commit_body())
    assert "idempotent" not in result
    assert result["contact_id"] == "888"
    assert any(c[0] == "create_contact" for c in env.hubspot.calls)


def test_live_done_row_still_replays(env):
    first = run_commit(env, commit_body())
    writes_after_first = len(env.hubspot.write_calls())
    replay = run_commit(env, commit_body())
    assert replay == {**first, "idempotent": True}
    assert len(env.hubspot.write_calls()) == writes_after_first


# ---------------------------------------------------------------------------
# audit-before-action invariant
# ---------------------------------------------------------------------------


def test_audit_attempt_failure_means_side_effect_never_runs(env):
    env.db.fail_event_insert = True

    with pytest.raises(CommitRejected) as exc_info:
        run_commit(env, commit_body())

    assert (exc_info.value.http_status, exc_info.value.code) == (
        500, "audit_write_failed")
    # NOTHING was tried against HubSpot -- not even the verify reads.
    assert env.hubspot.calls == []
    assert env.db.events == []


# ---------------------------------------------------------------------------
# DRY_RUN
# ---------------------------------------------------------------------------


def test_dry_run_writes_report_and_zero_hubspot_writes(env):
    env.cfg.dry_run = True

    result = run_commit(env, commit_body())

    assert result["dry_run"] is True
    would = result["would"]
    assert would["contact_props"]["hubspot_owner_id"] == "901"
    assert would["company_id"] == "555"
    assert would["owner"] == {"id": "901", "name": "TJ",
                              "source": "rep", "why": None}
    assert "note_preview" in would
    # attempt + done rows, both flagged dry_run.
    assert env.db.events_by_status() == ["attempt", "done"]
    assert all(e["dry_run"] for e in env.db.events)
    assert env.db.events[1]["detail"] == result
    # THE REAL WRITE NEVER RUNS IN DRY-RUN.
    assert env.hubspot.write_calls() == []


# ---------------------------------------------------------------------------
# live pipeline
# ---------------------------------------------------------------------------


def test_live_happy_path_order_and_response(env):
    result = run_commit(env, commit_body())

    # The write-pipeline order, asserted on the shared log:
    # attempt-event < company verify < email re-check < create < associate
    # < note < done-event.
    order = ["event:attempt", "companies_batch_read",
             "find_contacts_by_emails", "create_contact",
             "associate_contact_company", "create_note", "event:done"]
    positions = [env.log.index(step) for step in order]
    assert positions == sorted(positions), env.log

    assert result["contact_id"] == "888"
    assert result["company_id"] == "555"
    assert result["note_id"] == "note-1"
    assert result["dry_run"] is False
    assert result["owner"] == {"id": "901", "name": "TJ",
                               "source": "rep", "why": None}
    assert result["hubspot_url"].endswith("/0-1/888")
    assert result["message"] == DONE_MESSAGE

    # attempt row before, done row after; done detail = the response.
    assert env.db.events_by_status() == ["attempt", "done"]
    assert env.db.events[1]["detail"] == result
    assert not any(e["dry_run"] for e in env.db.events)

    # No tier requested -> no company update, no promote row.
    assert not any(c[0] == "update_company" for c in env.hubspot.calls)
    assert not any(e["action"] == "promote_t2" for e in env.db.events)


def test_company_owner_wins_owner_resolution(env):
    env.hubspot.companies["555"]["hubspot_owner_id"] = "444"
    # int in the roster row vs str on the record: the owners.py lesson --
    # both sides are str-coerced before comparing.
    env.db.active_owner_rows = [{"hubspot_owner_id": 901},
                                {"hubspot_owner_id": 444}]

    # 444 is not an enrolled prospector rep -- the name comes from the
    # HubSpot owners API fallback (chain: reps -> get_owner -> raw id).
    env.hubspot.owners["444"] = {"id": "444", "firstName": "Nick",
                                 "lastName": "Rodriguez",
                                 "email": "nick@example.com"}

    result = run_commit(env, commit_body())

    assert result["owner"] == {"id": "444", "name": "Nick Rodriguez",
                               "source": "company_owner", "why": None}
    # The closed-lost lookup is skipped when the company already has an
    # ACTIVE owner.
    assert not any(c[0] == "get_latest_closed_lost_owner"
                   for c in env.hubspot.calls)


def test_stale_company_id_rejects_before_any_create(env):
    env.hubspot.companies.clear()

    with pytest.raises(CommitRejected) as exc_info:
        run_commit(env, commit_body())

    exc = exc_info.value
    assert (exc.http_status, exc.code) == (409, "company_id_stale")
    assert exc.detail["hs_company_id"] == "555"
    # attempt row + rejected row -- never a done row.
    assert env.db.events_by_status() == ["attempt", "rejected"]
    assert not any(c[0] == "create_contact" for c in env.hubspot.calls)


def test_company_appeared_since_resolve_rejects_with_found_id(env):
    env.hubspot.domain_hits = [{"id": "444", "domain": "frosthvac.com"}]

    with pytest.raises(CommitRejected) as exc_info:
        run_commit(env, new_company_body())

    exc = exc_info.value
    assert (exc.http_status, exc.code) == (409, "company_appeared")
    assert exc.detail["hs_company_id"] == "444"
    assert not any(c[0] == "create_company" for c in env.hubspot.calls)


def test_live_email_recheck_hit_rejects_with_existing_contact(env):
    env.hubspot.email_hits = [{"id": "321", "matched_email": "jane@acme.com"}]

    with pytest.raises(CommitRejected) as exc_info:
        run_commit(env, commit_body())

    exc = exc_info.value
    assert (exc.http_status, exc.code) == (409, "contact_exists")
    assert exc.detail["hs_contact_id"] == "321"
    assert not any(c[0] == "create_contact" for c in env.hubspot.calls)


def test_side_effect_failure_writes_failed_event_and_502(env):
    env.hubspot.create_contact_error = HubSpotError(
        "HubSpot POST /crm/v3/objects/contacts/batch/create returned "
        "HTTP 500 after 4 attempts")

    with pytest.raises(CommitRejected) as exc_info:
        run_commit(env, commit_body())

    assert (exc_info.value.http_status, exc_info.value.code) == (
        502, "hubspot_write_failed")
    assert env.db.events_by_status() == ["attempt", "failed"]
    assert "HTTP 500" in env.db.events[1]["detail"]["error"]
    # Downstream writes never ran.
    assert not any(c[0] in ("associate_contact_company", "create_note")
                   for c in env.hubspot.calls)


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def test_blocking_guard_rejects_before_the_audit_attempt(env):
    env.guards.commit_holds = [FakeGuardHold(
        "inferred_email", blocking=True, message="email was inferred")]

    with pytest.raises(CommitRejected) as exc_info:
        run_commit(env, commit_body())

    exc = exc_info.value
    assert (exc.http_status, exc.code) == (422, "inferred_email")
    assert exc.detail["message"] == "email was inferred"
    # Holds precede the attempt: nothing tried, nothing audited.
    assert env.db.events == []
    assert env.hubspot.calls == []


def test_nonblocking_hold_passes_through_and_is_listed_in_preview(env):
    env.guards.commit_holds = [FakeGuardHold(
        "risky_email", blocking=False, message="catch-all domain")]

    shown = preview(env.db, env.hubspot, env.rep, env.cfg, commit_body())
    assert shown["holds"][0]["code"] == "risky_email"
    assert shown["holds"][0]["blocking"] is False

    result = run_commit(env, commit_body())
    assert result["contact_id"] == "888"


def test_guard_facts_come_from_the_body(env):
    env.guards.commit_holds = []
    run_commit(env, new_company_body())
    (call,) = env.guards.commit_calls[-1:]
    assert call == {
        "email": "jane@acme.com",
        "email_status": "verified",
        "company_domain": "frosthvac.com",
        "alternate_confirmed": False,
    }


# ---------------------------------------------------------------------------
# caps
# ---------------------------------------------------------------------------


def test_promote_cap_exceeded_rejects_402_with_blocked_cap_event(env):
    env.rep.daily_promote_cap = 1
    env.db.events.append({
        "rep_id": 1, "action": "promote_t2", "status": "done",
        "idempotency_key": None, "dry_run": False, "reason": None,
        "target": {}, "detail": {},
    })

    with pytest.raises(CommitRejected) as exc_info:
        run_commit(env, commit_body(tier="tier_2"))

    exc = exc_info.value
    assert (exc.http_status, exc.code) == (402, "daily_promote_cap")
    assert exc.detail == {"used": 1, "cap": 1}
    blocked = env.db.events[-1]
    assert (blocked["action"], blocked["status"]) == ("blocked_cap", "rejected")
    # Never reached the audit attempt or HubSpot.
    assert env.hubspot.calls == []


def test_no_reason_needed_for_tier_or_target(env):
    """The reason field is gone from the create flow -- tier and
    target-account are plain choices, no reason gate."""
    result = run_commit(env, commit_body(tier="tier_2", target_account=True))
    assert result["tier"] == "tier_2"
    assert result["target_account"] is True


def test_stale_reason_key_is_accepted_and_ignored(env):
    """A stale client still sending a reason key must never get a 400 --
    the value is dropped: not in the note, not in the audit echo. Even a
    non-string reason is ignored rather than rejected."""
    result = run_commit(env, commit_body(tier="tier_2",
                                         reason="expanding TX"))
    assert result["contact_id"] == "888"
    note = next(c for c in env.hubspot.calls if c[0] == "create_note")[1]
    assert "expanding TX" not in note and "Reason:" not in note
    attempt = env.db.events[0]
    assert "reason" not in attempt["detail"]["intent"]

    result2 = run_commit(env, commit_body(idempotency_key="key-2",
                                          reason={"not": "a string"}))
    assert result2["contact_id"] == "888"


def test_tier_promotion_updates_company_and_logs_promote_row(env):
    result = run_commit(env, commit_body(
        tier="tier_2", target_account=True,
        provenance=[{"provider": "fullenrich", "field": "work_email",
                     "status": "verified", "cost": 0.5}],
    ))

    assert result["tier"] == "tier_2"
    update = next(c for c in env.hubspot.calls if c[0] == "update_company")
    assert update[1] == "555"
    assert update[2] == {"hs_ideal_customer_profile": "tier_2",
                         "hs_is_target_account": "true"}
    # The promote ledger row the daily cap counts.
    promote = [e for e in env.db.events if e["action"] == "promote_t2"]
    assert len(promote) == 1
    assert promote[0]["status"] == "done" and not promote[0]["dry_run"]
    # The note carries provenance + who clicked (no reason line -- the
    # field is gone from the create flow).
    note = next(c for c in env.hubspot.calls if c[0] == "create_note")[1]
    assert "fullenrich" in note and "cost 0.5" in note
    assert "TJ" in note
    assert "Reason:" not in note


def test_dry_run_never_consumes_the_promote_cap(env):
    env.cfg.dry_run = True
    run_commit(env, commit_body(tier="tier_2"))
    assert not any(e["action"] == "promote_t2" for e in env.db.events)


# ---------------------------------------------------------------------------
# tier_1 -- plain dropdown option
# (no gates, no warnings, no cap, no reason)
# ---------------------------------------------------------------------------


def test_tier_1_commits_with_no_reason_and_logs_promote_t1(env):
    result = run_commit(env, commit_body(tier="tier_1"))

    assert result["tier"] == "tier_1"
    update = next(c for c in env.hubspot.calls if c[0] == "update_company")
    assert update[2]["hs_ideal_customer_profile"] == "tier_1"
    # Telemetry, exactly like tier_2's promote_t2 row (v_usage counts it).
    promote = [e for e in env.db.events if e["action"] == "promote_t1"]
    assert len(promote) == 1
    assert promote[0]["status"] == "done" and not promote[0]["dry_run"]
    assert promote[0]["detail"] == {"tier": "tier_1", "commit_key": "key-1"}
    assert not any(e["action"] == "promote_t2" for e in env.db.events)


def test_tier_1_ignores_an_exhausted_promote_cap(env):
    """tier_1 has NO cap and never reads the daily promote cap: with the
    tier_2 cap fully spent, a tier_1 commit still goes through."""
    env.rep.daily_promote_cap = 0

    result = run_commit(env, commit_body(tier="tier_1"))

    assert result["tier"] == "tier_1"
    # The promote-cap counter was never even consulted.
    assert not any(e["action"] == "blocked_cap" for e in env.db.events)


def test_tier_1_promote_row_never_consumes_the_t2_cap(env):
    """A day of tier_1 promotes leaves the tier_2 promote cap untouched --
    promote_t1 rows are audit-only and the cap counter reads promote_t2."""
    env.rep.daily_promote_cap = 1
    run_commit(env, commit_body(tier="tier_1"))

    result = run_commit(env, commit_body(idempotency_key="key-2",
                                         tier="tier_2"))
    assert result["tier"] == "tier_2"


def test_dry_run_tier_1_never_logs_promote_t1(env):
    env.cfg.dry_run = True
    run_commit(env, commit_body(tier="tier_1"))
    assert not any(e["action"] == "promote_t1" for e in env.db.events)


# ---------------------------------------------------------------------------
# tier-demotion guard
# ---------------------------------------------------------------------------


def test_tier_change_on_already_tiered_company_rejects_422(env):
    env.hubspot.companies["555"]["hs_ideal_customer_profile"] = "tier_1"

    with pytest.raises(CommitRejected) as exc_info:
        run_commit(env, commit_body(tier="tier_2"))

    exc = exc_info.value
    assert (exc.http_status, exc.code) == (422, "tier_change_blocked")
    assert exc.detail["current"] == "tier_1"
    assert exc.detail["requested"] == "tier_2"
    assert "already has an ICP tier" in exc.detail["message"]
    # NOTHING was written -- no company PATCH, no contact create.
    assert env.hubspot.write_calls() == []
    # Raised inside the verified plan (after the attempt row), so it is
    # audited like the other read-time refusals: attempt + rejected.
    assert env.db.events_by_status() == ["attempt", "rejected"]


def test_tier_1_on_an_already_tiered_company_still_rejects_422(env):
    """The demotion guard is data protection, NOT one of the gates the
    ruling removed: tier_1 requested on a company that already carries a
    different tier is still blocked."""
    env.hubspot.companies["555"]["hs_ideal_customer_profile"] = "tier_2"

    with pytest.raises(CommitRejected) as exc_info:
        run_commit(env, commit_body(tier="tier_1"))

    exc = exc_info.value
    assert (exc.http_status, exc.code) == (422, "tier_change_blocked")
    assert exc.detail["current"] == "tier_2"
    assert exc.detail["requested"] == "tier_1"
    assert env.hubspot.write_calls() == []


def test_same_tier_request_is_a_noop_not_a_patch(env):
    env.hubspot.companies["555"]["hs_ideal_customer_profile"] = "tier_2"

    result = run_commit(env, commit_body(tier="tier_2"))

    assert result["contact_id"] == "888"
    assert result["tier"] is None  # effective tier: nothing was written
    assert not any(c[0] == "update_company" for c in env.hubspot.calls)
    # A no-op is not a promotion: the promote ledger stays untouched.
    assert not any(e["action"] == "promote_t2" for e in env.db.events)


def test_untiered_company_accepts_a_tier_as_before(env):
    # companies["555"] carries no hs_ideal_customer_profile at all.
    result = run_commit(env, commit_body(tier="tier_2"))
    assert result["tier"] == "tier_2"
    update = next(c for c in env.hubspot.calls if c[0] == "update_company")
    assert update[2]["hs_ideal_customer_profile"] == "tier_2"


# ---------------------------------------------------------------------------
# inline association at create
# ---------------------------------------------------------------------------


def test_contact_is_born_associated_to_the_company(env):
    run_commit(env, commit_body())
    created = next(c for c in env.hubspot.calls if c[0] == "create_contact")
    # company_id rides inline in the create call -- the portal's
    # auto-create setting never sees an unassociated contact.
    assert created[2] == "555"


def test_new_company_id_is_passed_inline_too(env):
    run_commit(env, new_company_body())
    created = next(c for c in env.hubspot.calls if c[0] == "create_contact")
    assert created[2] == "777"  # the id create_company just returned


def test_associate_failure_after_inline_association_never_fails_commit(env):
    env.hubspot.associate_error = HubSpotError(
        "HubSpot PUT /crm/v4/... returned HTTP 409: association already "
        "exists", status_code=409)

    result = run_commit(env, commit_body())

    # The inline association at create is authoritative; the redundant
    # explicit call failing is logged and swallowed.
    assert result["contact_id"] == "888"
    assert result["message"] == DONE_MESSAGE
    assert env.db.events_by_status() == ["attempt", "done"]
    # Downstream writes still ran.
    assert any(c[0] == "create_note" for c in env.hubspot.calls)


# ---------------------------------------------------------------------------
# daily commit cap
# ---------------------------------------------------------------------------


def _seed_done_commits(db, n, *, dry_run):
    for i in range(n):
        db.events.append({
            "rep_id": 1, "action": "commit", "status": "done",
            "idempotency_key": f"prior-{i}", "dry_run": dry_run,
            "reason": None, "target": {}, "detail": {},
        })


def test_daily_commit_cap_rejects_402_with_blocked_cap_event(env):
    _seed_done_commits(env.db, DAILY_COMMIT_CAP, dry_run=False)

    with pytest.raises(CommitRejected) as exc_info:
        run_commit(env, commit_body())

    exc = exc_info.value
    assert (exc.http_status, exc.code) == (402, "daily_commit_cap")
    assert exc.detail == {"used": DAILY_COMMIT_CAP, "cap": DAILY_COMMIT_CAP}
    blocked = env.db.events[-1]
    assert (blocked["action"], blocked["status"]) == ("blocked_cap",
                                                      "rejected")
    assert blocked["detail"]["blocked_action"] == "commit"
    # Never reached the audit attempt or HubSpot.
    assert env.hubspot.calls == []


def test_dry_run_commits_never_consume_the_commit_cap(env):
    _seed_done_commits(env.db, DAILY_COMMIT_CAP, dry_run=True)
    result = run_commit(env, commit_body())
    assert result["contact_id"] == "888"


# ---------------------------------------------------------------------------
# optional email
# ---------------------------------------------------------------------------


def test_email_less_commit_creates_from_linkedin_identity(env):
    body = commit_body()
    body["contact"]["email"] = ""
    body["contact"]["email_status"] = ""

    result = run_commit(env, body)

    assert result["contact_id"] == "888"
    # No live email re-check -- there is no address to collide on.
    assert not any(c[0] == "find_contacts_by_emails"
                   for c in env.hubspot.calls)
    created = next(c for c in env.hubspot.calls if c[0] == "create_contact")
    assert "email" not in created[1]  # no empty-string email property
    note = next(c for c in env.hubspot.calls if c[0] == "create_note")[1]
    assert "No email -- created from LinkedIn identity." in note


# ---------------------------------------------------------------------------
# note-body hygiene
# ---------------------------------------------------------------------------


def test_note_escapes_markup_in_vendor_text(env):
    result = run_commit(env, commit_body(
        tier="tier_2",
        provenance=[{"provider": "full<en>rich", "field": "work_email",
                     "status": "verified"}],
    ))

    assert result["contact_id"] == "888"
    note = next(c for c in env.hubspot.calls if c[0] == "create_note")[1]
    # HubSpot renders hs_note_body as rich text: vendor-supplied strings
    # must arrive entity-escaped, never as markup.
    assert "full&lt;en&gt;rich" in note
    assert "<en>" not in note


# ---------------------------------------------------------------------------
# owner display names
# ---------------------------------------------------------------------------


def test_owner_name_lookup_failures_never_fail_the_preview(env):
    """Both name sources down -> the raw id string, and the preview still
    succeeds (a name is decoration, never a gate)."""
    env.db.fail_name_lookup = True
    env.hubspot.get_owner_error = HubSpotError("HubSpot GET /crm/v3/owners "
                                               "returned HTTP 500")

    result = preview(env.db, env.hubspot, env.rep, env.cfg, commit_body())

    assert result["preview"]["owner"] == {"id": "901", "name": "901",
                                          "source": "rep", "why": None}


def test_owner_missing_from_owners_api_falls_back_to_raw_id(env):
    """An owner outside the roster whose owners-API lookup 404s (None):
    the name falls back to the raw id -- ownership is never hidden."""
    env.hubspot.companies["555"]["hubspot_owner_id"] = "444"
    env.db.active_owner_rows = [{"hubspot_owner_id": "901"},
                                {"hubspot_owner_id": "444"}]
    # env.hubspot.owners has no "444" -> get_owner returns None.

    result = run_commit(env, commit_body())

    assert result["owner"] == {"id": "444", "name": "444",
                               "source": "company_owner", "why": None}


def test_note_body_names_the_owner_with_id(env):
    """The audit note reads 'Owner: <name> (<id>) (<source>)', not a
    bare id."""
    result = run_commit(env, commit_body())

    assert result["contact_id"] == "888"
    note = next(c for c in env.hubspot.calls if c[0] == "create_note")[1]
    assert "Owner: TJ (901) (rep)" in note


def test_note_body_skips_name_when_it_fell_back_to_the_id(env):
    """No '901 (901)' silliness: when the name lookup fell all the way back
    to the raw id, the note shows the id once."""
    env.db.rep_names = {}
    result = run_commit(env, commit_body())

    assert result["contact_id"] == "888"
    note = next(c for c in env.hubspot.calls if c[0] == "create_note")[1]
    assert "Owner: 901 (rep)" in note
    assert "901 (901)" not in note


# ---------------------------------------------------------------------------
# company in the plan
# ---------------------------------------------------------------------------


def test_plan_carries_company_new_for_a_new_company(env):
    result = preview(env.db, env.hubspot, env.rep, env.cfg,
                     new_company_body())

    plan = result["preview"]
    # The rep-facing copy of the create payload: name, domain, NORMALIZED
    # 2-letter state, LinkedIn page.
    assert plan["company_new"] == {
        "name": "Frost HVAC",
        "domain": "frosthvac.com",
        "state": "AK",
        "linkedin_company_page": "https://www.linkedin.com/company/frost-hvac/",
    }
    assert plan["company_props"] == plan["company_new"]
    assert "company_id" not in plan


def test_plan_carries_existing_company_identity_from_the_live_verify(env):
    result = preview(env.db, env.hubspot, env.rep, env.cfg, commit_body())

    plan = result["preview"]
    assert plan["company_id"] == "555"
    # Straight off the batch-read verify -- no extra HubSpot call.
    assert plan["company_name"] == "Acme Services"
    assert plan["company_domain"] == "acme.com"
    assert "company_new" not in plan
    reads = [c[0] for c in env.hubspot.calls]
    assert reads.count("companies_batch_read") == 1


# ---------------------------------------------------------------------------
# owner triage
# ---------------------------------------------------------------------------


def test_unresolvable_owner_creates_unowned_contact_flagged_for_triage(env):
    # New company (no existing owner to inherit) AND the committing rep has
    # no HubSpot owner id on file -> ownership can't be resolved, so the
    # contact is created UNOWNED and flagged for triage rather than silently
    # assigned to the clicking rep.
    env.rep.hubspot_owner_id = ""

    result = run_commit(env, new_company_body())

    assert result["needs_triage"] is True
    assert result["owner"]["id"] is None
    assert result["owner"]["name"] is None  # no owner -> no name
    assert result["owner"]["source"] == "triage"
    # The rep-facing WHY: a bare TRIAGE
    # badge just makes reps ask. Must be a non-empty explanation.
    assert isinstance(result["owner"]["why"], str) and result["owner"]["why"]
    assert result["company_id"] == "777"  # the new company was created
    created = next(c for c in env.hubspot.calls if c[0] == "create_contact")
    # Created UNOWNED -- never silently assigned to the clicking rep.
    assert "hubspot_owner_id" not in created[1]
    company = next(c for c in env.hubspot.calls if c[0] == "create_company")
    assert company[1]["state"] == "AK"  # normalized 2-letter on the record
    assert company[1]["domain"] == "frosthvac.com"


# ---------------------------------------------------------------------------
# link_linkedin (one-click backfill)
# ---------------------------------------------------------------------------


def test_link_linkedin_patches_normalized_url_after_live_reread(env):
    env.hubspot.contacts["888"] = {"id": "888", "hs_linkedin_url": ""}

    result = run_commit(env, link_body())

    # Guard compared against the LIVE record, not a cached one.
    assert env.guards.link_calls == [("", "linkedin.com/in/jane-doe")]
    patch = next(c for c in env.hubspot.calls if c[0] == "update_contact")
    assert patch[1] == "888"
    assert patch[2] == {"hs_linkedin_url": "linkedin.com/in/jane-doe"}
    assert result["contact_id"] == "888"
    assert result["hubspot_url"].endswith("/0-1/888")
    # Its own action keyspace in the audit log.
    assert [(e["action"], e["status"]) for e in env.db.events] == [
        ("link_linkedin", "attempt"), ("link_linkedin", "done")]


def test_link_linkedin_conflict_rejects_422_with_no_patch(env):
    env.hubspot.contacts["888"] = {
        "id": "888", "hs_linkedin_url": "linkedin.com/in/someone-else"}
    env.guards.link_hold = FakeGuardHold(
        "linkedin_conflict", blocking=True,
        message="contact already carries a different LinkedIn URL")

    with pytest.raises(CommitRejected) as exc_info:
        run_commit(env, link_body())

    assert (exc_info.value.http_status, exc_info.value.code) == (
        422, "linkedin_conflict")
    assert not any(c[0] == "update_contact" for c in env.hubspot.calls)
    assert env.db.events == []


def test_link_linkedin_missing_contact_is_404(env):
    with pytest.raises(CommitRejected) as exc_info:
        run_commit(env, link_body())
    assert (exc_info.value.http_status, exc_info.value.code) == (
        404, "contact_not_found")
    assert env.db.events == []
