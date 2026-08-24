"""The HTTP surface: a stdlib ThreadingHTTPServer serving the extension.

Postures adapted from an earlier enrichment-waterfall service, moved
from one shared RUN_TOKEN to per-rep bearer tokens (prospector/auth.py):

  * a token anywhere in a URL is a 400, never an auth attempt: query
    strings land in proxy/access logs, so by the time we see it the
    credential is already burned -- treat the request as malformed and
    say so. (Exception: /healthz, which is public and takes no token.)
  * missing/bad bearer is a 401 after a 0.5s penalty, with NO
    WWW-Authenticate header -- an unauthenticated caller learns only that
    it was rejected, not what scheme to probe.
  * GET /healthz is public because a platform probe cannot send a header,
    and it answers with liveness ONLY: {"ok": true}. No version, no DB
    probe, no counts -- nothing for an anonymous scanner to fingerprint.

New here (the caller is a browser extension, not a cron):

  * CORS pinned to exactly ONE origin, config.extension_origin. Preflight
    (OPTIONS) answers 204 with the allow-headers a fetch() from the
    extension needs; every response echoes Access-Control-Allow-Origin
    when the request's Origin matches. A POST carrying a PRESENT Origin
    that does NOT match is a 403 before auth even runs -- CORS is a
    browser courtesy, this 403 is the server enforcing the same line for
    defense in depth. Requests with no Origin at all (curl, tests, other
    servers) fall through to auth: CORS only ever constrains browsers.
  * body cap is 16KB (413) because extension payloads are small JSON;
    anything bigger is a bug or an attack, and we refuse before reading.

Routes live in a plain dict {(method, path): handler} so later phases add
entries without touching the dispatch plumbing.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
import weakref
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

from . import recognize as recognize_mod
from .auth import Rep, RepRegistry
from .hubspot import HubSpotClient, company_page_slug

if TYPE_CHECKING:  # pragma: no cover
    from .config import AppConfig

logger = logging.getLogger(__name__)

VERSION = "1.0.0"
SERVICE = "prospecting_plugin"

_MAX_BODY_BYTES = 16 * 1024
# Enough to make credential stuffing tedious without holding a worker
# thread long enough to matter (ThreadingHTTPServer gives each request its
# own thread).
_AUTH_FAIL_DELAY_SECONDS = 0.5
_CORS_MAX_AGE = "600"
# Substring markers, deliberately broad: "?token=", "?bearer=", and
# anything Authorization-shaped all count. A false positive on a future
# legit param name is a cheap rename; a credential in an access log is not.
_URL_SECRET_MARKERS = ("token", "bearer", "authorization")

# A route handler receives (request, rep, body): the live Handler (for
# send_json and headers), the authenticated Rep, and the parsed JSON body
# (a dict for POST, None for GET). Registered per-server in build_server()
# and extensible afterwards via server.routes -- tests and later phases
# add entries to the same dict the dispatcher reads.
RouteHandler = Callable[[Any, Rep, "dict | None"], None]


class ProspectorServer(ThreadingHTTPServer):
    daemon_threads = True          # a SIGTERM must not wait on in-flight sockets
    allow_reuse_address = True
    routes: dict[tuple[str, str], RouteHandler]  # attached by build_server()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Connections that have already sent a status line (the Handler
        # adds them in send_response) -- consulted by handle_error so the
        # bare 500 is only ever written to a virgin socket, never appended
        # to a half-sent response.
        self._responses_started: "weakref.WeakSet[Any]" = weakref.WeakSet()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The stdlib default dumps the raw traceback straight to stderr,
        # bypassing the MaskedLogFilter entirely -- anything secret-shaped
        # in the exception text would land unredacted. Route it through
        # logging instead so the filter sees it.
        logging.getLogger(__name__).exception(
            "unhandled error while serving a request from %s",
            client_address[0] if isinstance(client_address, tuple) else "-",
        )
        # Best effort: if no response has started on this connection, hand
        # the client a bare 500 instead of a dropped socket. Any secondary
        # failure here is swallowed on purpose -- the error is already
        # logged and the connection is closing either way.
        try:
            if request not in self._responses_started:
                body = b'{"error": "internal"}'
                request.sendall(
                    b"HTTP/1.1 500 Internal Server Error\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
                    b"Connection: close\r\n"
                    b"\r\n" + body
                )
        except Exception:  # noqa: BLE001 - secondary failure, see above
            pass


def build_server(cfg: "AppConfig", db: Any, registry: RepRegistry) -> ProspectorServer:
    """Wire the routes to this cfg/db/registry and bind the socket. Split
    out from serve() so tests can stand the real server up on an ephemeral
    port with a stub db. `db` is unused by the Phase 0 routes but is part
    of the signature now so later phases plug in without re-plumbing."""
    _ = db  # reserved for Phase 1+ route handlers

    # EXTENSION_ORIGIN accepts a COMMA-SEPARATED list of origins: unpacked
    # extensions get a different id per machine, so the pre-Web-Store rep
    # rollout needs one origin per rep. Collapses back to a
    # single origin once the store listing (stable id) is live.
    cors_origin = (getattr(cfg, "extension_origin", "") or "").strip()
    cors_origins = frozenset(
        o.strip() for o in cors_origin.split(",") if o.strip()
    )
    if not cors_origin:
        # Disabled-open is for LOCAL DEV ONLY: with no pinned origin there
        # is no origin check and every Origin is echoed back. Deploying
        # like this hands any web page the right to call the API with a
        # logged-in rep's token, so shout once at build time.
        logger.warning(
            "EXTENSION_ORIGIN is empty -- CORS is DISABLED-OPEN (any Origin "
            "accepted). Fine for local dev, NEVER for a deployment."
        )

    routes: dict[tuple[str, str], RouteHandler] = {}

    def _route_status(request: Any, rep: Rep, body: dict | None) -> None:
        """Who am I, and what may I spend today. `caps` are the configured
        daily LIMITS from the roster row, so the extension can render the
        ceiling alongside spend."""
        request.send_json(200, {
            "rep": rep.display_name,
            "dry_run": bool(getattr(cfg, "dry_run", True)),
            "caps": {
                "credit": rep.daily_credit_cap,
                "promote": rep.daily_promote_cap,
                "t1": rep.daily_t1_cap,
            },
            "version": VERSION,
            "workspace_balance": _workspace_balance(),
            "spent_today": _spent_today(rep.id),
        })

    def _spent_today(rep_id: int) -> float:
        try:
            from . import budget as budget_mod
            return budget_mod.spent_today(db, rep_id)
        except Exception:
            return 0.0

    routes[("GET", "/status")] = _route_status

    # HubSpot client is built once per server, lazily: Phase 0 deploys (and
    # most tests) have no token yet, and /recognize is the only consumer so
    # far. `hubspot_client` may be injected by tests via server attribute.
    _hubspot_lock = threading.Lock()
    _hubspot_ref: list[Any] = [None]

    def _hubspot(request: Any) -> Any:
        injected = getattr(request.server, "hubspot_client", None)
        if injected is not None:
            return injected
        with _hubspot_lock:
            if _hubspot_ref[0] is None:
                token = (getattr(cfg, "hubspot_token", "") or "").strip()
                if not token:
                    return None
                _hubspot_ref[0] = HubSpotClient(
                    token, portal_id=getattr(cfg, "hubspot_portal_id", "") or ""
                )
            return _hubspot_ref[0]

    def _route_recognize(request: Any, rep: Rep, body: dict | None) -> None:
        """Classify the tab URL and answer from cache or live lookups.
        Free for the rep, so this route never counts against any cap."""
        body = body or {}
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            request.send_json(400, {"error": "url_required"})
            return
        surface = recognize_mod.classify_surface(url)
        if surface.kind in ("linkedin_other", "ignored", "sales_nav"):
            # No lookups, no HubSpot client needed.
            request.send_json(200, recognize_mod.recognize(db, None, surface))
            return
        hubspot = _hubspot(request)
        if hubspot is None:
            request.send_json(503, {
                "error": "hubspot_not_configured",
                "detail": "HubSpot is not configured on this deployment",
            })
            return
        # Tab title, for the name hint (a bare slug can autofill a garbled
        # first/last name). Validate, never reject: a garbage/oversized title
        # is silently ignored -- it is a best-effort hint, not a reason to
        # fail the lookup.
        page_title = body.get("page_title")
        if not isinstance(page_title, str) or len(page_title) > 300:
            page_title = None
        result = recognize_mod.recognize(
            db, hubspot, surface,
            force_refresh=bool(body.get("force_refresh")),
            page_title=page_title,
        )
        # Telemetry only -- v_usage counts these. Never fail a lookup over
        # a logging insert.
        if not result.get("cached"):
            try:
                db.execute(
                    "INSERT INTO prospector.events"
                    " (rep_id, action, status, target, dry_run)"
                    " VALUES (%s, 'recognize', 'done', %s, %s)",
                    (rep.id, json.dumps({"kind": surface.kind, "key": surface.key}),
                     bool(getattr(cfg, "dry_run", True))),
                )
            except Exception:
                logger.warning("recognize event insert failed", exc_info=True)
        request.send_json(200, result)

    routes[("POST", "/recognize")] = _route_recognize

    # ---- Phase 2: enrichment ------------------------------------------------
    # Registry/waterfalls/runner are built lazily on first use (tests inject
    # server.enrich_runner / server.fullenrich_adapter). In DRY_RUN the real
    # runner is replaced by a zero-cost simulator that exercises the exact
    # reserve->run->settle path: reps can click every button, nothing bills.
    _enrich_lock = threading.Lock()
    _enrich_ref: dict[str, Any] = {}

    def _enrich_stack(request: Any) -> tuple[Any, Any] | None:
        """Return (runner, fullenrich_adapter_or_None) or None if unconfigured."""
        injected = getattr(request.server, "enrich_runner", None)
        if injected is not None:
            return injected, getattr(request.server, "fullenrich_adapter", None)
        with _enrich_lock:
            if "runner" not in _enrich_ref:
                from . import providers as providers_mod
                from . import waterfall as waterfall_mod
                registry = providers_mod.build_registry(db, cfg)
                waterfalls = waterfall_mod.load_waterfalls(db)
                if bool(getattr(cfg, "dry_run", True)):
                    _enrich_ref["runner"] = _make_dry_run_runner()
                else:
                    if not registry:
                        return None
                    _enrich_ref["runner"] = waterfall_mod.make_runner(db, registry, waterfalls)
                _enrich_ref["fullenrich"] = registry.get("fullenrich")
                _enrich_ref["waterfalls"] = waterfalls
                _enrich_ref["registry"] = registry
            return _enrich_ref["runner"], _enrich_ref.get("fullenrich")

    def _make_dry_run_runner() -> Any:
        def _runner(job_row: dict) -> tuple[dict, float]:
            inp = job_row.get("input") or {}
            first = (inp.get("first_name") or "pat").lower() or "pat"
            fields = list(job_row.get("fields") or [])
            emails, phones = [], []
            if "work_email" in fields:
                emails.append({"address": f"{first}@example-dryrun.invalid",
                               "type": "work", "status": "unknown",
                               "provider": "dry_run", "cost_credits": 0})
            if "personal_email" in fields:
                emails.append({"address": f"{first}@personal-dryrun.invalid",
                               "type": "personal", "status": "unknown",
                               "provider": "dry_run", "cost_credits": 0})
            if "mobile" in fields:
                phones.append({"number": "+1 555 0100", "type": "mobile",
                               "status": "unknown", "dnc_flag": None,
                               "provider": "dry_run", "cost_credits": 0})
            time.sleep(2)  # let the panel show its "finding..." state honestly
            return ({"emails": emails, "phones": phones, "profile": {}, "company": {},
                     "fields_requested": fields, "fields_found": fields,
                     "fields_missed": [], "dry_run": True}, 0.0)
        return _runner

    def _worst_cost(fields: list[str]) -> float:
        # Worst case per field = the max cost any enabled leg could bill.
        # MULTI-LEG GATE (a review flagged this): max-over-legs is only
        # correct while each field's waterfall has ONE billable leg. The
        # moment multi-leg waterfalls land (additional legs behind the
        # fullenrich leg), a walk can bill SEVERAL legs for one field (risky
        # hit at leg 1, verified at leg 2 -- both billed), so this must
        # become sum-over-billable-legs per field or reservations
        # undersize and the daily cap goes soft. Do not add multi-leg
        # waterfalls without changing this.
        waterfalls = _enrich_ref.get("waterfalls") or {}
        registry = _enrich_ref.get("registry") or {}
        total = 0.0
        for f in fields:
            legs = waterfalls.get(f) or []
            leg_costs = [registry[l["provider_id"]].cost([f])
                         for l in legs if l["provider_id"] in registry]
            # No configured leg (dry-run with empty registry): fall back to
            # list price so the reservation is still honest.
            total += max(leg_costs) if leg_costs else {"work_email": 1.0,
                                                       "personal_email": 3.0,
                                                       "mobile": 10.0}[f]
        return total

    _balance_cache: dict[str, Any] = {"at": 0.0, "value": None}

    def _workspace_balance() -> float | None:
        adapter = _enrich_ref.get("fullenrich")
        if adapter is None:
            return None
        now = time.monotonic()
        if now - _balance_cache["at"] > 300:
            try:
                _balance_cache["value"] = adapter.get_balance()
            except Exception:
                _balance_cache["value"] = None
            _balance_cache["at"] = now
        return _balance_cache["value"]

    def _route_enrich(request: Any, rep: Rep, body: dict | None) -> None:
        from . import budget as budget_mod
        from . import jobs as jobs_mod
        body = body or {}
        url = body.get("linkedin_url")
        fields = body.get("fields")
        if not isinstance(url, str) or not url.strip():
            request.send_json(400, {"error": "url_required"})
            return
        # Every item must be a str BEFORE any set() math -- an unhashable
        # item (dict/list) in a set literal is a TypeError, and garbage
        # input must be a 400, never a 500 (a review found this).
        if not isinstance(fields, list) or not fields or \
                not all(isinstance(f, str) for f in fields) or \
                not set(fields) <= set(("work_email", "mobile", "personal_email")):
            request.send_json(400, {"error": "fields_invalid"})
            return
        # Dedupe before costing/enqueue: ["mobile", "mobile"] must reserve
        # (and bill) one mobile, not two (a review found this).
        fields = sorted(set(fields))
        # Identity fields ride into jobs.input and on to the vendor: if
        # present they must be short strings -- anything else is garbage
        # in, and better a 400 here than a TypeError five layers down.
        for key in ("first_name", "last_name", "company_domain", "company_name"):
            value = body.get(key)
            if value is not None and (
                    not isinstance(value, str) or len(value) > 200):
                request.send_json(400, {"error": "input_invalid"})
                return
        surface = recognize_mod.classify_surface(url)
        if surface.kind == "sales_nav":
            request.send_json(400, {"error": "sales_nav_url"})
            return
        if surface.kind != "linkedin_profile":
            request.send_json(400, {"error": "not_a_profile_url"})
            return
        stack = _enrich_stack(request)
        if stack is None or stack[0] is None:
            request.send_json(503, {"error": "enrichment_not_configured"})
            return
        runner = stack[0]
        jobs_mod.expire_stale(db)

        # Same-key replay: a double-clicked button must not double-reserve.
        idem = body.get("idempotency_key")
        if isinstance(idem, str) and idem:
            try:
                prior = db.query(
                    "SELECT id FROM prospector.jobs WHERE rep_id = %s"
                    " AND input->>'idempotency_key' = %s"
                    " AND created_at > now() - interval '24 hours'"
                    " ORDER BY created_at DESC LIMIT 1", (rep.id, idem))
            except Exception:
                prior = []
            if prior:
                request.send_json(202, {"job_id": str(prior[0]["id"]),
                                        "replayed": True,
                                        "reserved_credits": 0,
                                        "workspace_balance": _workspace_balance(),
                                        "rep_spent_week": _spent_week(rep.id)})
                return

        # Route-level replay of a recent FOUND (a review found this): if any
        # done job for this profile in the last 30
        # days already found every requested field, hand back THAT job id
        # -- the panel polls /result and renders instantly, and nobody
        # re-pays for what the team already bought. (The waterfall's
        # never-re-buy cache only covers misses; a prior found used to
        # re-run the legs and bill again.) Best-effort: any hiccup here
        # falls through to a normal create.
        replayed = _find_recent_found(surface.key, list(fields))
        if replayed is not None:
            request.send_json(202, {"job_id": str(replayed),
                                    "replayed": True,
                                    "reserved_credits": 0,
                                    "workspace_balance": _workspace_balance(),
                                    "rep_spent_week": _spent_week(rep.id)})
            return

        input_payload = {
            "linkedin_url": surface.key,
            "first_name": body.get("first_name") or "",
            "last_name": body.get("last_name") or "",
            "company_domain": body.get("company_domain") or "",
            "company_name": body.get("company_name") or "",
            "idempotency_key": idem or "",
        }
        # FullEnrich requires first+last (live smoke: empty names ->
        # error.enrichment.data.empty). When the panel
        # didn't know them, borrow the recognize cache's title-derived
        # name_hint and account context for this profile -- best-effort,
        # never fatal.
        if not (input_payload["first_name"] and input_payload["last_name"]):
            try:
                rows = db.query(
                    "SELECT payload FROM prospector.recognize_cache"
                    " WHERE cache_key = %(key)s AND expires_at > now()",
                    {"key": f"li:{surface.key}"})
                cached = rows[0]["payload"] if rows else {}
                if isinstance(cached, str):
                    cached = json.loads(cached)
                hint = (cached or {}).get("name_hint") or {}
                input_payload["first_name"] = (
                    input_payload["first_name"] or hint.get("first_name") or "")
                input_payload["last_name"] = (
                    input_payload["last_name"] or hint.get("last_name") or "")
                acct = (cached or {}).get("account") or {}
                input_payload["company_domain"] = (
                    input_payload["company_domain"] or acct.get("domain") or "")
                input_payload["company_name"] = (
                    input_payload["company_name"] or acct.get("name") or "")
            except Exception:
                logger.warning("name-hint backfill failed", exc_info=True)
        worst = _worst_cost(list(fields))
        try:
            job = jobs_mod.create_job(db, rep, surface.key, list(fields),
                                      input_payload, worst)
        except budget_mod.CapExceeded as exc:
            _log_event(rep, "blocked_cap", "rejected",
                       {"kind": "enrich", "key": surface.key})
            request.send_json(402, {"error": "daily_credit_cap",
                                    "cap": exc.cap, "spent": exc.spent})
            return
        except jobs_mod.InFlightConflict as exc:
            request.send_json(409, {"error": "in_flight",
                                    "by": getattr(exc, "holder_display_name", None) or "another rep",
                                    "job_id": getattr(exc, "job_id", None)})
            return
        jobs_mod.run_job_async(db, str(job["id"]), runner)
        # status 'attempt', not 'done': this event marks the ENQUEUE -- the
        # job has not run yet (a review found this). Completion
        # accounting reads prospector.jobs (v_usage), not this row.
        _log_event(rep, "enrich", "attempt",
                   {"kind": "enrich", "key": surface.key, "fields": list(fields)},
                   cost=worst)
        request.send_json(202, {"job_id": str(job["id"]),
                                "reserved_credits": worst,
                                "workspace_balance": _workspace_balance(),
                                "rep_spent_week": _spent_week(rep.id)})

    def _route_result(request: Any, rep: Rep, body: dict | None) -> None:
        from . import jobs as jobs_mod
        _ = body
        query = urlsplit(request.path).query
        job_id = ""
        for part in query.split("&"):
            if part.startswith("job_id="):
                job_id = part[len("job_id="):]
        if not job_id:
            request.send_json(400, {"error": "job_id_required"})
            return
        jobs_mod.expire_stale(db)
        job = jobs_mod.get_job(db, job_id)
        if job is None:
            request.send_json(404, {"error": "job_not_found"})
            return
        result = job.get("result") or None
        out = {"state": job.get("state"),
               "result": result if job.get("state") == "done" else None,
               "credits_billed": float(job.get("credits_billed") or 0)}
        if job.get("state") == "failed" and isinstance(result, dict):
            out["error"] = result.get("error", "enrichment failed")
        if job.get("state") == "expired":
            out["error"] = "job expired before the provider answered; the hold was released"
        request.send_json(200, out)

    def _find_recent_found(norm_url: str, fields: list[str]) -> str | None:
        """Most recent done job (30 days) for this profile whose
        fields_found covers every requested field, or None. Reads a
        handful of candidates because the newest done job may have asked
        for FEWER fields than this request wants."""
        try:
            rows = db.query(
                "SELECT id, result FROM prospector.jobs"
                " WHERE norm_linkedin_url = %s AND state = 'done'"
                " AND created_at > now() - interval '30 days'"
                " ORDER BY created_at DESC LIMIT 5", (norm_url,))
        except Exception:
            logger.warning("recent-found lookup failed -- creating a fresh "
                           "job instead", exc_info=True)
            return None
        wanted = set(fields)
        for row in rows or []:
            result = row.get("result")
            if isinstance(result, str):  # psycopg2 may hand jsonb back as text
                try:
                    result = json.loads(result)
                except ValueError:
                    continue
            if not isinstance(result, dict):
                continue
            found = result.get("fields_found")
            if isinstance(found, list) and wanted <= set(
                    f for f in found if isinstance(f, str)):
                return str(row["id"])
        return None

    def _spent_week(rep_id: int) -> float:
        try:
            rows = db.query(
                "SELECT COALESCE(SUM(credits_billed), 0) AS s FROM prospector.jobs"
                " WHERE rep_id = %s AND created_at > now() - interval '7 days'",
                (rep_id,))
            return float(rows[0]["s"]) if rows else 0.0
        except Exception:
            return 0.0

    def _log_event(rep: Rep, action: str, status: str, target: dict,
                   cost: float | None = None) -> None:
        try:
            db.execute(
                "INSERT INTO prospector.events"
                " (rep_id, action, status, target, cost_credits, dry_run)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (rep.id, action, status, json.dumps(target), cost,
                 bool(getattr(cfg, "dry_run", True))))
        except Exception:
            logger.warning("event insert failed (%s)", action, exc_info=True)

    routes[("POST", "/enrich")] = _route_enrich
    routes[("GET", "/result")] = _route_result

    # ---- Phase 3: resolve + commit -------------------------------------
    def _route_resolve(request: Any, rep: Rep, body: dict | None) -> None:
        """The anti-duplicate step: live candidate matches before any write.
        Free (reads only), so no caps. Contact rows gain hubspot_url so the
        panel's 'Already exists' rows are clickable."""
        from . import resolve as resolve_mod
        body = body or {}
        contact_in = body.get("contact") or {}
        company_in = body.get("company") or {}
        for section in (contact_in, company_in):
            if not isinstance(section, dict) or any(
                not isinstance(v, str) or len(v) > 200
                for v in section.values() if v is not None
            ):
                request.send_json(400, {"error": "input_invalid"})
                return
        hubspot = _hubspot(request)
        if hubspot is None:
            request.send_json(503, {
                "error": "hubspot_not_configured",
                "detail": "HubSpot is not configured on this deployment",
            })
            return
        contact_res = resolve_mod.resolve_contact(
            hubspot,
            linkedin_url=contact_in.get("linkedin_url") or "",
            email=contact_in.get("email") or "",
            first_name=contact_in.get("first_name") or "",
            last_name=contact_in.get("last_name") or "",
            company_name=contact_in.get("company_name") or "",
        )
        # The panel sends the FULL company-page URL, but the slug chain
        # needs the bare /company/<slug> slug -- the exact-slug post-filter
        # in find_company_by_linkedin_slug compares canonical slugs, so a
        # full URL would match nothing (dead chain in production, caught by
        # a hardening pass). Extract via hubspot.py's
        # company_page_slug -- the ONE parser both sides share -- and let
        # bare-slug inputs (no /company/ segment -> '') pass through as-is.
        raw_company_li = (company_in.get("linkedin_slug")
                          or company_in.get("linkedin_url") or "")
        company_slug = company_page_slug(raw_company_li) or raw_company_li
        company_res = resolve_mod.resolve_company(
            hubspot,
            domain=company_in.get("domain") or "",
            linkedin_slug=company_slug,
            name=company_in.get("name") or "",
            state=company_in.get("state") or "",
        )
        for row in contact_res.get("matches", []):
            row["hubspot_url"] = hubspot.contact_hubspot_url(row["hs_contact_id"])
        for row in company_res.get("matches", []):
            row["hubspot_url"] = hubspot.company_hubspot_url(row["hs_company_id"])
        _log_event(rep, "resolve", "done", {
            "contact_matches": len(contact_res.get("matches", [])),
            "company_matches": len(company_res.get("matches", [])),
        })
        request.send_json(200, {
            "contact_matches": contact_res.get("matches", []),
            "company_matches": company_res.get("matches", []),
            "flags": company_res.get("flags", {}),
        })

    def _route_commit(request: Any, rep: Rep, body: dict | None) -> None:
        """The write. All decisions live in writer.commit's six-step
        pipeline; this route only maps CommitRejected onto HTTP."""
        from . import writer as writer_mod
        body = body or {}
        hubspot = _hubspot(request)
        if hubspot is None:
            request.send_json(503, {
                "error": "hubspot_not_configured",
                "detail": "HubSpot is not configured on this deployment",
            })
            return
        try:
            result = writer_mod.commit(db, hubspot, rep, cfg, body)
        except writer_mod.CommitRejected as exc:
            payload = {"error": exc.code}
            payload.update(exc.detail if isinstance(exc.detail, dict) else {})
            request.send_json(exc.http_status, payload)
            return
        request.send_json(200, result)

    routes[("POST", "/resolve")] = _route_resolve
    routes[("POST", "/commit")] = _route_commit

    # ---- record detail (read-only) ---------------------------------------

    def _owner_name(hubspot: Any, owner_id: Any) -> str | None:
        """Owner display name via the HubSpot owners API; falls back to the
        raw id so ownership is never hidden just because the lookup missed
        (the deactivated-rep pattern -- same posture as recognize.py's
        _hubspot_owner_name, replicated here rather than importing a
        private helper)."""
        if not owner_id:
            return None
        owner = hubspot.get_owner(owner_id)
        if owner:
            return " ".join(
                part for part in (owner.get("firstName"), owner.get("lastName"))
                if part
            ).strip() or owner.get("email")
        return str(owner_id)

    def _valid_record_id(value: Any) -> bool:
        # Digits-only, ASCII-only (str.isdigit alone accepts unicode digits
        # like superscripts), and bounded -- the id goes straight into a
        # HubSpot URL path.
        return (isinstance(value, str) and 0 < len(value) <= 20
                and value.isascii() and value.isdigit())

    def _route_record(request: Any, rep: Rep, body: dict | None) -> None:
        """Read-only record detail for a recognized company/contact: the
        basics a rep would otherwise open HubSpot for (a pilot review found
        the account card showed only name+domain+chip and nothing else).
        Free (reads only), so no caps -- and no caching:
        these reads are cheap and the card must show what HubSpot holds
        NOW."""
        body = body or {}
        record_type = body.get("type")
        record_id = body.get("id")
        if record_type not in ("company", "contact") \
                or not _valid_record_id(record_id):
            request.send_json(400, {"error": "input_invalid"})
            return
        hubspot = _hubspot(request)
        if hubspot is None:
            request.send_json(503, {
                "error": "hubspot_not_configured",
                "detail": "HubSpot is not configured on this deployment",
            })
            return

        if record_type == "company":
            record = hubspot.get_company(record_id)
            if record is None:
                request.send_json(404, {"error": "record_not_found"})
                return
            payload = {
                "record": record,
                "owner_name": _owner_name(hubspot, record.get("hubspot_owner_id")),
                "contacts": hubspot.get_company_contacts(record_id),
                "hubspot_url": hubspot.company_hubspot_url(record_id),
            }
        else:
            record = hubspot.get_contact_detail(record_id)
            if record is None:
                request.send_json(404, {"error": "record_not_found"})
                return
            company = None
            company_url = None
            associated_id = str(record.get("associatedcompanyid") or "").strip()
            if associated_id:
                full = hubspot.get_company(associated_id)
                if full:
                    # Mini-card only: name/domain/tier/target. The rep can
                    # ask for the company's own /record if they want more.
                    company = {
                        "id": str(full["id"]),
                        "name": full.get("name"),
                        "domain": full.get("domain"),
                        "tier": full.get("hs_ideal_customer_profile") or None,
                        "is_target_account": full.get("hs_is_target_account"),
                    }
                    company_url = hubspot.company_hubspot_url(full["id"])
            payload = {
                "record": record,
                "owner_name": _owner_name(hubspot, record.get("hubspot_owner_id")),
                "company": company,
                "hubspot_url": hubspot.contact_hubspot_url(record_id),
                "company_hubspot_url": company_url,
            }

        # Telemetry only -- never fail the read over it (_log_event already
        # swallows insert failures).
        _log_event(rep, "record_view", "done",
                   {"type": record_type, "id": record_id})
        # Round-trip through JSON so dates/Decimals from the account view
        # become strings here, same as the recognize path.
        request.send_json(200, json.loads(json.dumps(payload, default=str)))

    routes[("POST", "/record")] = _route_record

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"   # keep-alive; every response sets Content-Length
        timeout = 30                    # socket timeout: a stalled client cannot camp
        server_version = f"{SERVICE}/{VERSION}"
        sys_version = ""                # don't advertise the Python version

        # -- plumbing ---------------------------------------------------------

        def log_request(self, code: Any = "-", size: Any = "-") -> None:
            # Per-rep tokens are unknown to the process in plaintext (only
            # sha256 digests are stored), so the MaskedLogFilter can NEVER
            # redact one that lands in a query string -- the only safe log
            # line is the bare path (from a hardening pass). Never log
            # self.requestline.
            if isinstance(code, HTTPStatus):
                code = code.value
            logger.info(
                "http %s %s %s",
                getattr(self, "command", "-"),
                urlsplit(getattr(self, "path", "") or "").path,
                code,
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            # The stdlib routes log_error()/log_request() through here with
            # the RAW requestline in args -- drop fmt and args entirely and
            # log only the bare path (same reasoning as log_request above).
            _ = fmt, args
            logger.info(
                "http %s %s",
                getattr(self, "command", "-"),
                urlsplit(getattr(self, "path", "") or "").path,
            )

        def send_response(self, code: int, message: str | None = None) -> None:
            # Mark this connection so handle_error never writes a second
            # status line onto a response that already started.
            try:
                self.server._responses_started.add(self.connection)
            except TypeError:  # a non-weakref-able test double
                pass
            super().send_response(code, message)

        def send_json(self, status: int, payload: dict, *,
                      close: bool = False,
                      headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload).encode("utf-8")
            if close:
                self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.send_header("Connection", "close")
            self._send_cors_allow_origin()
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: int, message: str, *, close: bool = False) -> None:
            self.send_json(status, {"error": message}, close=close)

        # -- CORS -------------------------------------------------------------

        def _origin_allowed(self, origin: str) -> bool:
            # Empty cors_origin == disabled-open (local dev, warned above).
            return not cors_origins or origin in cors_origins

        def _send_cors_allow_origin(self) -> None:
            """Echo Access-Control-Allow-Origin on EVERY response whose
            request Origin matches the pinned origin -- errors included,
            or the extension could never read an error body."""
            origin = self.headers.get("Origin")
            if origin and self._origin_allowed(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib naming
            if self._reject_transfer_encoding():
                return
            # Preflight is answered before auth on any path: a browser
            # preflight carries no credentials by spec, and the response
            # reveals nothing but the CORS policy itself. A mismatched
            # Origin still gets a 204 -- just without the allow headers,
            # which is the browser's cue to block.
            self.send_response(204)
            origin = self.headers.get("Origin")
            if origin and self._origin_allowed(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Max-Age", _CORS_MAX_AGE)
            self.send_header("Content-Length", "0")
            self.end_headers()

        # -- auth -------------------------------------------------------------

        def _secret_in_query(self, query: str) -> bool:
            lowered = (query or "").lower()
            return any(marker in lowered for marker in _URL_SECRET_MARKERS)

        def _bearer_token(self) -> str | None:
            scheme, _, token = self.headers.get("Authorization", "").partition(" ")
            if scheme.strip().lower() != "bearer":
                return None
            return token.strip() or None

        def _deny(self, *, close: bool = False) -> None:
            # Constant-ish cost on failure, and no WWW-Authenticate: an
            # unauthenticated caller learns only that it was rejected.
            time.sleep(_AUTH_FAIL_DELAY_SECONDS)
            logger.warning(
                "auth_failed method=%s path=%s xff=%s "
                "(X-Forwarded-For is client-supplied and untrusted -- do not gate on it)",
                self.command,
                urlsplit(self.path).path,
                self.headers.get("X-Forwarded-For", "-"),
            )
            self._send_error(401, "unauthorized", close=close)

        # -- dispatch ---------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            self._dispatch("POST")

        def _reject_transfer_encoding(self) -> bool:
            """True if the request was refused. A transfer-encoded
            (chunked) body has no Content-Length, so the 16KB pre-read cap
            cannot see it coming -- and left unread it would poison
            keep-alive by being parsed as the NEXT request. Refuse on ANY
            method (mirrors waterfall's require-Content-Length posture)
            and close."""
            if self.headers.get("Transfer-Encoding") is None:
                return False
            self._send_error(400, "transfer_encoding_unsupported", close=True)
            return True

        def _dispatch(self, method: str) -> None:
            if self._reject_transfer_encoding():
                return

            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"

            # POST error paths below may leave an unread request body on
            # the socket, which would poison HTTP/1.1 keep-alive -- so any
            # response sent before the body is read closes the connection.
            close_unread = method == "POST"

            # Before auth AND before the public /healthz shortcut: a token
            # in the query string is already in somebody's access log, so
            # treat the request as malformed -- /healthz?token=x gets the
            # same 400 as everywhere else.
            if self._secret_in_query(parsed.query):
                logger.warning("rejected a credential in the query string on %s", path)
                self._send_error(
                    400,
                    "do not put credentials in the URL (they land in proxy "
                    "logs); use Authorization: Bearer <token>",
                    close=close_unread,
                )
                return

            if method == "GET" and path == "/healthz":
                # PUBLIC: a platform health probe cannot send a header.
                # Liveness only -- no version, no DB probe, no counts.
                self.send_json(200, {"ok": True})
                return

            # Defense in depth for state-changing requests: a browser that
            # SENDS an Origin we did not pin gets refused server-side too,
            # not just starved of CORS headers. No Origin at all (curl,
            # server-to-server, tests) falls through -- CORS only ever
            # constrains browsers.
            origin = self.headers.get("Origin")
            if (method == "POST" and origin is not None
                    and not self._origin_allowed(origin)):
                logger.warning("rejected POST %s from disallowed origin", path)
                self._send_error(403, "origin not allowed", close=close_unread)
                return

            rep = registry.authenticate(self._bearer_token())
            if rep is None:
                self._deny(close=close_unread)
                return

            handler = routes.get((method, path))
            if handler is None:
                self._send_error(404, "not found", close=close_unread)
                return

            body: dict | None = None
            if method == "POST":
                body, error_status, error = self._read_json_body()
                if error is not None:
                    self._send_error(error_status or 400, error, close=True)
                    return

            handler(self, rep, body)

        # -- request body -------------------------------------------------------

        def _read_json_body(self) -> tuple[dict | None, int | None, str | None]:
            """Returns (payload, error_status, error). The 16KB cap is
            enforced on Content-Length BEFORE reading a byte: an oversize
            body is refused (413), never buffered."""
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                # Mirrors waterfall server.py: a POST with no Content-Length
                # would read as an EMPTY body, and an empty body means "all
                # defaults" -- on a future spend endpoint that silent
                # default is exactly the wrong failure mode. Demand the
                # header instead.
                return (None, 400, "content_length_required")
            try:
                length = int((raw_length or "0").strip())
            except (TypeError, ValueError):
                return (None, 400, "Content-Length must be an integer")
            if length < 0:
                return (None, 400, "Content-Length must not be negative")
            if length > _MAX_BODY_BYTES:
                return (None, 413, f"body must be at most {_MAX_BODY_BYTES} bytes")

            raw = self.rfile.read(length) if length else b""
            if not raw.strip():
                return ({}, None, None)  # empty body means "all defaults"
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return (None, 400, "body must be valid UTF-8 JSON")
            if not isinstance(parsed, dict):
                return (None, 400, "body must be a JSON object")
            return (parsed, None, None)

    server = ProspectorServer((cfg.host, int(cfg.port)), Handler)
    server.routes = routes
    return server


def install_signal_handlers(server: ProspectorServer) -> None:
    """SIGTERM/SIGINT: shut the listener down cleanly. Phase 0 has no
    long-running work to drain -- requests are short and transactional."""

    def _handle(signum: int, _frame: Any) -> None:
        logger.warning("signal %s received: shutting down", signum)
        # shutdown() blocks until serve_forever() returns, and this handler
        # runs ON the serve_forever thread -- so call it from another one.
        threading.Thread(target=server.shutdown, name="shutdown", daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):  # not the main thread (tests)
            logger.debug("could not install handler for signal %s", sig)


def serve(cfg: "AppConfig", db: Any) -> None:
    """Construct the rep registry, bind, and serve until signalled. Startup
    logging names NO secrets -- var names and states only."""
    registry = RepRegistry(db)
    server = build_server(cfg, db, registry)
    install_signal_handlers(server)

    host, port = server.server_address[:2]
    logger.info("%s %s listening on http://%s:%s", SERVICE, VERSION, host, port)
    if cfg.dry_run:
        logger.warning("DRY_RUN=TRUE -- nothing will write to HubSpot or spend credits")
    else:
        logger.warning("DRY_RUN=FALSE -- this deployment is LIVE: writes and spend are real")

    try:
        server.serve_forever()
    finally:
        server.server_close()
        logger.info("%s stopped", SERVICE)
