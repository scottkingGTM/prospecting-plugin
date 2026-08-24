"""Integration tests for POST /enrich + GET /result — real server on an
ephemeral port, stub Database with working jobs-table emulation, and an
injected runner (server.enrich_runner) so no provider code runs.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest

from prospector.auth import RepRegistry, hash_token
from prospector.server import build_server

TOKEN = "e" * 40


class StubDb:
    """Emulates reps, jobs (incl. the partial unique in-flight index),
    events, and the budget SUM — enough for the enrich routes end-to-end."""

    def __init__(self, daily_credit_cap: float = 50) -> None:
        self.cap = daily_credit_cap
        self.jobs: dict[str, dict] = {}
        self.events: list[tuple] = []
        self.lock = threading.Lock()

    # -- query (jobs.py uses named dicts; budget/status use tuples) ---------
    def query(self, sql: str, params=None) -> list[dict]:
        if "FROM prospector.reps" in sql and "JOIN" not in sql:
            return [{
                "id": 1, "email": "rep@example.com", "display_name": "Rep",
                "hubspot_owner_id": "42", "token_hash": hash_token(TOKEN),
                "daily_credit_cap": self.cap, "daily_promote_cap": 25,
                "daily_t1_cap": 3, "daily_research_cap": 3,
            }]
        if "SUM(credits_reserved)" in sql:
            return [{"spent": self._spent()}]
        if "SUM(credits_billed)" in sql:
            return [{"s": sum(j["credits_billed"] for j in self.jobs.values())}]
        if "input->>'idempotency_key'" in sql:
            _rep_id, idem = params
            hits = [j for j in self.jobs.values()
                    if j["input"].get("idempotency_key") == idem]
            return [{"id": h["id"]} for h in hits[:1]]
        if "JOIN prospector.reps" in sql:  # _HOLDER_SQL
            norm_url = params["norm_url"]
            for j in self.jobs.values():
                if j["norm_linkedin_url"] == norm_url and \
                        j["state"] in ("queued", "running"):
                    return [{"job_id": j["id"], "display_name": "Rep"}]
            return []
        if "FROM prospector.jobs WHERE id" in sql:  # _GET_JOB_SQL
            j = self.jobs.get(str(params["job_id"]))
            return [dict(j)] if j else []
        if "state = 'done'" in sql:  # route-level replay lookup (F5)
            (norm_url,) = params
            hits = [j for j in self.jobs.values()
                    if j["norm_linkedin_url"] == norm_url
                    and j["state"] == "done"]
            return [{"id": j["id"], "result": j["result"]} for j in hits]
        return []

    def execute(self, sql: str, params=None) -> int:
        if "INSERT INTO prospector.events" in sql:
            self.events.append((sql, params))
            return 1
        if "GREATEST(credits_billed" in sql:  # budget._SETTLE_SQL (tuple)
            billed, _b2, jid = params
            j = self.jobs.get(str(jid))
            if j is None:
                return 0
            j["credits_billed"] = max(j["credits_billed"], float(billed))
            j["credits_reserved"] = j["credits_billed"]
            return 1
        if "SET credits_reserved = 0" in sql:  # budget._EXPIRE_REFUND_SQL
            return 0
        return 0

    def _spent(self) -> float:
        return sum(j["credits_reserved"] for j in self.jobs.values()
                   if j["state"] != "expired")

    # -- cursor: one transaction per with-block ------------------------------
    class _Cur:
        def __init__(self, outer: "StubDb") -> None:
            self.outer = outer
            self.rows: list[dict] = []
            self.rowcount = -1

        def execute(self, sql: str, params=None) -> None:
            o = self.outer
            if "pg_advisory_xact_lock" in sql:
                self.rows = [{"pg_advisory_xact_lock": None}]
                return
            if "SUM(credits_reserved)" in sql:
                self.rows = [{"spent": o._spent()}]
                return
            if "INSERT INTO prospector.jobs" in sql:  # named dict
                norm_url = params["norm_url"]
                for j in o.jobs.values():
                    if j["norm_linkedin_url"] == norm_url and \
                            j["state"] in ("queued", "running"):
                        import psycopg2.errors
                        raise psycopg2.errors.UniqueViolation(
                            "duplicate key value violates unique constraint "
                            '"jobs_inflight_one_per_profile"')
                jid = str(uuid.uuid4())
                row = {"id": jid, "rep_id": params["rep_id"],
                       "norm_linkedin_url": norm_url,
                       "fields": list(params["fields"]),
                       "state": "queued",
                       "input": json.loads(params["input"]),
                       "credits_reserved": float(params["credits_reserved"]),
                       "credits_billed": 0.0, "result": None}
                o.jobs[jid] = row
                self.rows = [dict(row)]
                return
            if "SET state = 'running'" in sql:  # _MARK_RUNNING_SQL
                j = o.jobs.get(str(params["job_id"]))
                if j and j["state"] == "queued":
                    j["state"] = "running"
                    self.rows = [dict(j)]
                else:
                    self.rows = []
                return
            if "SET state = %(state)s" in sql:  # _FINISH_SQL (running guard)
                j = o.jobs.get(str(params["job_id"]))
                if j and j["state"] == "running":
                    j["state"] = params["state"]
                    j["result"] = json.loads(params["result"])
                    self.rowcount = 1
                else:
                    self.rowcount = 0
                self.rows = []
                return
            if "SET state = 'expired'" in sql:  # _EXPIRE_SQL — nothing stale here
                self.rows = []
                return
            if "GREATEST(credits_billed" in sql:  # settle via cursor, just in case
                o.execute(sql, params)
                self.rows = []
                return
            self.rows = []

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return list(self.rows)

    from contextlib import contextmanager

    @contextmanager
    def cursor(self):
        with self.lock:
            yield StubDb._Cur(self)


class _Cfg:
    extension_origin = ""
    dry_run = True
    hubspot_token = ""
    hubspot_portal_id = ""
    fullenrich_api_key = ""
    host = "127.0.0.1"
    port = 0


def _instant_runner(job_row: dict):
    return ({"emails": [{"address": "x@y.com", "type": "work",
                         "status": "verified", "provider": "fake",
                         "cost_credits": 1.0}],
             "phones": [], "profile": {}, "company": {},
             "fields_requested": job_row["fields"],
             "fields_found": job_row["fields"], "fields_missed": []}, 1.0)


@pytest.fixture()
def rig():
    db = StubDb()
    server = build_server(_Cfg(), db, RepRegistry(db))
    server.enrich_runner = _instant_runner
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}", server, db
    server.shutdown()
    server.server_close()


def _call(url, path, body=None, method=None, token=TOKEN):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data,
                                 method=method or ("POST" if body is not None else "GET"),
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


PROFILE = "https://www.linkedin.com/in/dana-ops/"


def _wait_done(url, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = _call(url, f"/result?job_id={job_id}")
        if body.get("state") in ("done", "failed"):
            return status, body
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_enrich_happy_path_202_then_done(rig):
    url, server, db = rig
    status, body = _call(url, "/enrich", {
        "linkedin_url": PROFILE, "fields": ["work_email"],
        "idempotency_key": str(uuid.uuid4())})
    assert status == 202
    assert body["reserved_credits"] == 1.0
    status, body = _wait_done(url, body["job_id"])
    assert body["state"] == "done"
    assert body["result"]["emails"][0]["address"] == "x@y.com"
    assert body["credits_billed"] == 1.0


def test_enrich_sales_nav_400(rig):
    url, server, db = rig
    status, body = _call(url, "/enrich", {
        "linkedin_url": "https://www.linkedin.com/sales/lead/ACwAA,x,y",
        "fields": ["work_email"]})
    assert (status, body["error"]) == (400, "sales_nav_url")
    assert not db.jobs


def test_enrich_cap_402_and_blocked_event(rig):
    url, server, db = rig
    status, body = _call(url, "/enrich", {
        "linkedin_url": PROFILE,
        "fields": ["work_email", "mobile", "personal_email"] })
    assert status == 202  # 14 worst-case, within 50
    # burn the rest of the cap with fake settled jobs
    for j in db.jobs.values():
        j["credits_reserved"] = 49.5
    status, body = _call(url, "/enrich", {
        "linkedin_url": "https://www.linkedin.com/in/someone-else/",
        "fields": ["mobile"]})
    assert (status, body["error"]) == (402, "daily_credit_cap")
    assert any("blocked_cap" in (p[1][1] if p[1] else "") for p in db.events)


def test_enrich_in_flight_409(rig):
    url, server, db = rig
    # a queued job for the same profile already exists
    _call(url, "/enrich", {"linkedin_url": PROFILE, "fields": ["work_email"]})
    # freeze it in queued state so the second call collides
    for j in db.jobs.values():
        j["state"] = "queued"
    status, body = _call(url, "/enrich", {"linkedin_url": PROFILE,
                                          "fields": ["mobile"]})
    assert (status, body["error"]) == (409, "in_flight")


def test_enrich_idempotency_replay(rig):
    url, server, db = rig
    key = str(uuid.uuid4())
    s1, b1 = _call(url, "/enrich", {"linkedin_url": PROFILE,
                                    "fields": ["work_email"],
                                    "idempotency_key": key})
    _wait_done(url, b1["job_id"])
    s2, b2 = _call(url, "/enrich", {"linkedin_url": PROFILE,
                                    "fields": ["work_email"],
                                    "idempotency_key": key})
    assert s2 == 202 and b2["job_id"] == b1["job_id"] and b2.get("replayed") is True
    assert len(db.jobs) == 1


def test_enrich_field_validation(rig):
    url, server, db = rig
    status, body = _call(url, "/enrich", {"linkedin_url": PROFILE,
                                          "fields": ["ssn"]})
    assert (status, body["error"]) == (400, "fields_invalid")


def test_enrich_replays_recent_found_without_repaying(rig):
    """A done job within 30 days whose fields_found covers the request is
    handed back as-is -- 202 with the OLD job id, replayed=true, zero
    reserved -- nobody re-pays for what the team already bought."""
    url, server, db = rig
    s1, b1 = _call(url, "/enrich", {"linkedin_url": PROFILE,
                                    "fields": ["work_email"]})
    assert s1 == 202
    _wait_done(url, b1["job_id"])

    # Different rep click, no idempotency key -- only the route replay
    # can catch this one.
    s2, b2 = _call(url, "/enrich", {"linkedin_url": PROFILE,
                                    "fields": ["work_email"]})
    assert s2 == 202
    assert b2["job_id"] == b1["job_id"]
    assert b2["replayed"] is True
    assert b2["reserved_credits"] == 0
    assert len(db.jobs) == 1  # no second job, no second bill

    # ...and the panel can poll that id for the full stored result.
    status, body = _call(url, f"/result?job_id={b2['job_id']}")
    assert body["state"] == "done"
    assert body["result"]["emails"][0]["address"] == "x@y.com"


def test_enrich_replay_needs_every_requested_field(rig):
    """A prior done job that found only work_email must NOT satisfy a
    request that also wants mobile -- that creates a fresh job."""
    url, server, db = rig
    s1, b1 = _call(url, "/enrich", {"linkedin_url": PROFILE,
                                    "fields": ["work_email"]})
    _wait_done(url, b1["job_id"])

    s2, b2 = _call(url, "/enrich", {"linkedin_url": PROFILE,
                                    "fields": ["work_email", "mobile"]})
    assert s2 == 202
    assert b2["job_id"] != b1["job_id"]
    assert b2.get("replayed") is not True
    assert len(db.jobs) == 2


def test_enrich_dedupes_fields_before_costing(rig):
    """F6a: ['mobile', 'mobile'] reserves ONE mobile (10), not two."""
    url, server, db = rig
    status, body = _call(url, "/enrich", {"linkedin_url": PROFILE,
                                          "fields": ["mobile", "mobile"]})
    assert status == 202
    assert body["reserved_credits"] == 10.0
    (job,) = db.jobs.values()
    assert job["fields"] == ["mobile"]


def test_enrich_non_string_field_items_are_400_not_500(rig):
    """F6b: an unhashable item used to TypeError inside set() -> 500.
    Garbage input is a 400."""
    url, server, db = rig
    status, body = _call(url, "/enrich", {"linkedin_url": PROFILE,
                                          "fields": [{"evil": True}]})
    assert (status, body["error"]) == (400, "fields_invalid")
    assert not db.jobs


@pytest.mark.parametrize("key", ["first_name", "last_name",
                                 "company_domain", "company_name"])
@pytest.mark.parametrize("bad", [123, ["x"], {"x": 1}, "y" * 201])
def test_enrich_identity_fields_must_be_short_strings(rig, key, bad):
    """F6b: identity fields, if present, must be str <= 200 chars."""
    url, server, db = rig
    status, body = _call(url, "/enrich", {"linkedin_url": PROFILE,
                                          "fields": ["work_email"],
                                          key: bad})
    assert (status, body["error"]) == (400, "input_invalid")
    assert not db.jobs


def test_enqueue_event_status_is_attempt_not_done(rig):
    """F6c: the enqueue-time event marks an ATTEMPT -- the job has not
    run yet. v_usage reads prospector.jobs for completions."""
    url, server, db = rig
    status, _ = _call(url, "/enrich", {"linkedin_url": PROFILE,
                                       "fields": ["work_email"]})
    assert status == 202
    enrich_events = [p for _sql, p in db.events if p and p[1] == "enrich"]
    assert enrich_events, "no enrich event was logged"
    assert all(p[2] == "attempt" for p in enrich_events)


def test_result_requires_job_id_and_404s(rig):
    url, server, db = rig
    status, body = _call(url, "/result")
    assert (status, body["error"]) == (400, "job_id_required")
    status, body = _call(url, f"/result?job_id={uuid.uuid4()}")
    assert status == 404


def test_status_carries_spend_fields(rig):
    url, server, db = rig
    status, body = _call(url, "/status")
    assert status == 200
    assert "workspace_balance" in body and "spent_today" in body
