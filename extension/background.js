// Prospecting Plugin — MV3 service worker (classic script, no modules).
//
// Three jobs, deliberately small:
//   1. Watch tab activity and tell the side panel "the rep is now looking
//      at <url>" (debounced, noise filtered).
//   2. Own every backend fetch. The Authorization header is attached in
//      exactly one place, and the token never reaches panel DOM code.
//   3. Own enrichment-job polling. Jobs outlive the panel (and this worker:
//      chrome.alarms re-wakes it every 30s per outstanding job), results
//      land in chrome.storage.session, and completion fires a desktop
//      notification — so the rep can close the panel and keep browsing.
//
// Real surface classification is SERVER-side. The classifier below exists
// only to (a) skip obvious noise like chrome:// pages, (b) give the panel a
// human label, and (c) let the panel short-circuit Sales Nav pages locally
// (enrichment can't use Sales Nav URLs, so there is nothing to ask about).

const DEBOUNCE_MS = 400;
const FETCH_TIMEOUT_MS = 20000;
// Production default (enterprise force-install build): reps only paste
// their token. Local dev still reachable by typing the 127.0.0.1 URL in
// options — both origins are on the allowlist.
const DEFAULT_BACKEND = "https://YOUR-BACKEND.example.com";

// Exact origins the worker will ever send the rep token to. Mirror of
// manifest.json host_permissions — keep the two in lockstep.
//
// Why this exists (defense in depth; from a hardening pass): MV3
// host_permissions does NOT stop a service-worker fetch from sending the
// Authorization header to an arbitrary origin once that origin's CORS
// preflight consents. host_permissions only *waives* CORS for the listed
// origins — a fetch to any other origin still goes out, and a malicious
// backend URL (e.g. planted in chrome.storage) could harvest the token.
// So we hard-refuse to attach the token to any origin outside this list.
const ALLOWED_BACKEND_ORIGINS = [
  "http://127.0.0.1:8080",
  "https://YOUR-BACKEND.example.com",
];

// True only when the URL parses and its exact origin is on the allowlist.
function isAllowedBackend(url) {
  try {
    return ALLOWED_BACKEND_ORIGINS.includes(new URL(url).origin);
  } catch (e) {
    return false;
  }
}

// Open the panel when the toolbar icon is clicked. Set on every SW start,
// not just onInstalled, so a crashed/restarted worker keeps the behavior.
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch(() => {});

// ---------------------------------------------------------------- surfaces

function classifySurface(url) {
  if (!url || !/^https?:\/\//i.test(url)) return "noise";
  let u;
  try {
    u = new URL(url);
  } catch (e) {
    return "noise";
  }
  const host = u.hostname.toLowerCase();
  if (host === "linkedin.com" || host.endsWith(".linkedin.com")) {
    const p = u.pathname;
    if (p.startsWith("/sales/")) return "sales_nav";
    if (p.startsWith("/in/")) return "linkedin_profile";
    if (p.startsWith("/company/")) return "linkedin_company";
    // /school/ is NOT a company surface: the server treats school pages as
    // idle, and the panel header must not claim "LinkedIn company".
    return "linkedin_other";
  }
  return "website";
}

// ------------------------------------------------------------- tab watcher

// Tab TITLE, alongside the URL. No content scripts by design (LinkedIn-risk
// posture), so the title — plain tab metadata the existing `tabs` permission
// already exposes, same risk posture as the URL — is the only place the
// person's real display name is visible to us. The slug alone would autofill
// a garbled first/last name. Parsing happens SERVER-side
// (prospector/recognize.py); here it is just truncated and passed along.
const PAGE_TITLE_MAX = 300;

function pageTitle(tab) {
  return String((tab && tab.title) || "").slice(0, PAGE_TITLE_MAX);
}

const debounceTimers = new Map(); // tabId -> timeout id

function scheduleNotify(tabId) {
  clearTimeout(debounceTimers.get(tabId));
  debounceTimers.set(
    tabId,
    setTimeout(() => {
      debounceTimers.delete(tabId);
      notifyPanel(tabId);
    }, DEBOUNCE_MS)
  );
}

function notifyPanel(tabId) {
  chrome.tabs.get(tabId, (tab) => {
    if (chrome.runtime.lastError || !tab || !tab.active) return;
    const url = tab.url || "";
    chrome.runtime
      .sendMessage({
        type: "tab_changed",
        tabId: tabId,
        url: url,
        page_title: pageTitle(tab),
        surface: classifySurface(url),
      })
      .catch(() => {
        // Panel closed — no receiver. Expected; swallow.
      });
  });
}

chrome.tabs.onActivated.addListener((activeInfo) => {
  scheduleNotify(activeInfo.tabId);
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  // `title` too: on LinkedIn's SPA navigation the title often lands a beat
  // AFTER the URL/complete events, and the title is where the name hint
  // comes from. The debounce collapses the burst; the server's recognize
  // cache makes the re-ask nearly free (a cached hit just gains the hint).
  if (changeInfo.status === "complete" || changeInfo.url || changeInfo.title) {
    scheduleNotify(tabId);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  clearTimeout(debounceTimers.get(tabId));
  debounceTimers.delete(tabId);
});

// ---------------------------------------------------------------- backend

function getSettings() {
  return chrome.storage.local.get(["backendUrl", "token"]).then((stored) => ({
    backendUrl: String(stored.backendUrl || DEFAULT_BACKEND).replace(/\/+$/, ""),
    token: String(stored.token || ""),
  }));
}

// Shared fetch wrapper. NEVER puts the token in the URL (the server
// hard-400s any request with a token in the query string, by design), and
// NEVER attaches the token — or even fetches — outside the origin
// allowlist above.
async function backendFetch(base, path, token, init) {
  if (!isAllowedBackend(base)) {
    return {
      ok: false,
      status: 0,
      error: "URL not in the allowed backend list",
    };
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const headers = Object.assign({}, (init && init.headers) || {});
    if (token) headers["Authorization"] = "Bearer " + token;
    const resp = await fetch(base + path, {
      method: (init && init.method) || "GET",
      headers: headers,
      body: (init && init.body) || undefined,
      signal: controller.signal,
    });
    const text = await resp.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (e) {
      // Non-JSON body; leave data null.
    }
    if (!resp.ok) {
      const msg =
        (data && (data.error || data.message)) || "HTTP " + resp.status;
      // `data` rides along on errors too: 402/409 carry structured fields
      // (cap/spent, by/job_id) the panel needs to render honest banners.
      return { ok: false, status: resp.status, error: String(msg), data: data };
    }
    return { ok: true, status: resp.status, data: data };
  } catch (err) {
    const aborted = err && err.name === "AbortError";
    return {
      ok: false,
      status: 0,
      error: aborted
        ? "Request timed out"
        : "Cannot reach the backend — is it running?",
    };
  } finally {
    clearTimeout(timer);
  }
}

async function handleRecognize(msg) {
  const settings = await getSettings();
  if (!settings.token) {
    return { ok: false, status: 401, error: "No rep token set" };
  }
  const body = {
    surface: msg.surface,
    url: msg.url,
    force_refresh: !!msg.force,
  };
  // Tab title = the name-hint source (see the tab-watcher comment). Titles
  // are metadata, not secrets — no filtering beyond the length cap.
  if (msg.page_title) {
    body.page_title = String(msg.page_title).slice(0, PAGE_TITLE_MAX);
  }
  return backendFetch(settings.backendUrl, "/recognize", settings.token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// Options page "Test connection": uses the form's CURRENT values (passed in
// the message) so the rep can verify before saving.
async function handleTestConnection(msg) {
  const base = String(msg.backendUrl || DEFAULT_BACKEND).replace(/\/+$/, "");
  return backendFetch(base, "/status", String(msg.token || ""), {
    method: "GET",
  });
}

// -------------------------------------------------------------- enrichment
//
// The SW is the single owner of job state. A job is born on POST /enrich
// (or by ATTACHING to someone else's 409 job), lives in
// chrome.storage.session under "enrichjob:<job_id>", and is polled two ways:
//   - chrome.alarms "job:<job_id>" every 30s — suspension-proof baseline.
//   - the panel's 3s fast-poll ("poll_job" messages) while it's open.
// On done/failed the result is merged under `enrich` into the SAME
// per-URL session cache entry the recognize cache uses, a notification
// fires, and the panel (if open) is messaged to repaint.

const JOB_ALARM_PREFIX = "job:";
const JOB_STORE_PREFIX = "enrichjob:";

// Session-cache key derivation. LOCKSTEP with urlKey() in sidepanel.js —
// the SW must write enrichment results into the exact entry the panel
// reads for a given tab URL.
const CACHE_PREFIX = "recog:";

function swUrlKey(url) {
  let base = String(url).split("#")[0];
  try {
    const u = new URL(base);
    const host = u.hostname.toLowerCase();
    if (host === "linkedin.com" || host.endsWith(".linkedin.com")) {
      base = u.origin + u.pathname;
    }
  } catch (e) {
    // Not parseable — fall through with the hash-stripped string.
  }
  return CACHE_PREFIX + base;
}

const ENRICH_FIELDS = ["work_email", "mobile", "personal_email"];

const FIELD_LABELS = {
  work_email: "Work email",
  mobile: "Mobile",
  personal_email: "Personal email",
};

// Merge a per-field enrichment patch into the per-URL cache entry, creating
// the entry (response: null) if recognize hasn't cached this URL yet.
async function mergeEnrichEntry(url, field, patch) {
  try {
    const key = swUrlKey(url);
    const stored = await chrome.storage.session.get(key);
    const entry = stored[key] || { response: null, ts: Date.now() };
    if (!entry.enrich || typeof entry.enrich !== "object") entry.enrich = {};
    entry.enrich[field] = Object.assign({}, entry.enrich[field] || {}, patch);
    await chrome.storage.session.set({ [key]: entry });
  } catch (e) {
    // Storage hiccup — polling continues; the next merge attempt retries.
  }
}

function jobStoreKey(jobId) {
  return JOB_STORE_PREFIX + jobId;
}

async function registerJob(job) {
  await chrome.storage.session.set({ [jobStoreKey(job.job_id)]: job });
  chrome.alarms.create(JOB_ALARM_PREFIX + job.job_id, {
    periodInMinutes: 0.5,
  });
}

async function getJob(jobId) {
  try {
    const stored = await chrome.storage.session.get(jobStoreKey(jobId));
    return stored[jobStoreKey(jobId)] || null;
  } catch (e) {
    return null;
  }
}

async function unregisterJob(jobId) {
  // ORDER MATTERS: remove the storage record FIRST. A surviving alarm with
  // no record self-cleans on its next fire (pollJob clears it); a surviving
  // record with no alarm would strand a spinner forever.
  try {
    await chrome.storage.session.remove(jobStoreKey(jobId));
  } catch (e) {
    // Best effort.
  }
  try {
    await chrome.alarms.clear(JOB_ALARM_PREFIX + jobId);
  } catch (e) {
    // Best effort.
  }
}

// Broadcast to the panel; swallowed when no panel is open.
function broadcast(msg) {
  chrome.runtime.sendMessage(msg).catch(() => {});
}

async function handleEnrich(msg) {
  const settings = await getSettings();
  if (!settings.token) {
    return { ok: false, status: 401, error: "No rep token set" };
  }
  const field = String(msg.field || "");
  if (!ENRICH_FIELDS.includes(field)) {
    return { ok: false, status: 0, error: "Unknown enrichment field" };
  }
  const url = String(msg.url || msg.linkedin_url || "");
  const profileName = msg.profile_name ? String(msg.profile_name) : "";

  // ---- double-spend guard (from a hardening pass) ----------------------
  // While an attempt for this (url, field) is pending (POST on the wire),
  // queued, or running, REFUSE to start another: respond {busy:true} so the
  // panel just keeps its spinner and no second POST goes out. Staleness
  // escapes keep a crashed attempt from stranding the button forever — a
  // pending POST resolves within the 20s fetch timeout, a job within the
  // 30-minute TTL below.
  const cacheKey = swUrlKey(url);
  let prev = {};
  try {
    const stored = await chrome.storage.session.get(cacheKey);
    const entry = stored[cacheKey];
    prev = (entry && entry.enrich && entry.enrich[field]) || {};
  } catch (e) {
    // Storage hiccup — treat as no prior state.
  }
  const prevState = prev.state || null;
  const age = typeof prev.ts === "number" ? Date.now() - prev.ts : Infinity;
  const busy =
    (prevState === "pending" && age < 2 * 60 * 1000) ||
    ((prevState === "queued" || prevState === "running") &&
      age < 35 * 60 * 1000);
  if (busy) {
    return { ok: false, busy: true, status: 0, error: "Already in flight" };
  }

  // Deterministic idempotency key per ATTEMPT. The counter lives in the
  // session entry and only increments once the prior attempt reached a
  // terminal state (done/failed) — so a duplicate POST for the same attempt
  // (double message, SW restart mid-flight, lost response retried) carries
  // the SAME key and the server replays the job instead of double-reserving
  // credits. A cleared/never-landed attempt reuses its number on retry.
  let attempt = Number(prev.attempt) || 0;
  if (attempt < 1 || prevState === "done" || prevState === "failed") {
    attempt += 1;
  }
  const idemKey =
    cacheKey.slice(CACHE_PREFIX.length) + "|" + field + "|" + attempt;

  // Mark the attempt pending BEFORE the POST so a concurrent click is
  // refused above even while the request is still on the wire.
  await mergeEnrichEntry(url, field, {
    state: "pending",
    attempt: attempt,
    ts: Date.now(),
  });

  const resp = await backendFetch(settings.backendUrl, "/enrich", settings.token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      linkedin_url: String(msg.linkedin_url || ""),
      fields: [field],
      idempotency_key: idemKey,
      // FullEnrich requires first+last (live smoke); pass
      // whatever identity the panel already knows. The server backfills
      // from the recognize cache's name_hint when these are empty.
      first_name: String(msg.first_name || ""),
      last_name: String(msg.last_name || ""),
      company_domain: String(msg.company_domain || ""),
      company_name: String(msg.company_name || ""),
    }),
  });

  if (resp.ok && resp.data && resp.data.job_id) {
    // 202 accepted — this is now OUR job to track.
    const job = {
      job_id: String(resp.data.job_id),
      url: url,
      field: field,
      profile_name: profileName,
      created_ts: Date.now(),
    };
    await registerJob(job);
    await mergeEnrichEntry(url, field, {
      job_id: job.job_id,
      state: "queued",
      by: null,
      result: null,
      credits_billed: null,
      error: null,
      ts: Date.now(),
    });
    return resp;
  }

  if (resp.status === 409 && resp.data && resp.data.job_id) {
    // Someone else is already enriching this profile — ATTACH: track their
    // job_id exactly as if it were ours. Both reps see the result. The
    // starter's name is persisted on the job record so the notification and
    // result rows can attribute it (and omit any billing claim — the
    // credits are theirs, not ours).
    const job = {
      job_id: String(resp.data.job_id),
      url: url,
      field: field,
      profile_name: profileName,
      attached: true,
      attached_by: resp.data.by ? String(resp.data.by) : null,
      created_ts: Date.now(),
    };
    await registerJob(job);
    await mergeEnrichEntry(url, field, {
      job_id: job.job_id,
      state: "queued",
      by: job.attached_by,
      result: null,
      credits_billed: null,
      error: null,
      ts: Date.now(),
    });
    return resp;
  }

  // 400/402/503/network/409-without-job_id/other — nothing to track. Clear
  // the pending mark so a genuinely failed POST re-enables the button
  // (restoring a prior terminal result if this was a refresh attempt).
  await mergeEnrichEntry(url, field, {
    state: prevState === "done" || prevState === "failed" ? prevState : null,
    ts: Date.now(),
  });
  return resp;
}

// In-memory guard so the alarm poll and the fast poll can't both finalize
// (double-notify) the same job. Backed by the existence check on the
// stored job record, which survives SW restarts.
const finalizedJobs = new Set();

async function pollJob(jobId) {
  const job = await getJob(jobId);
  if (!job) {
    // Unknown job (session storage cleared, or already finalized) — stop
    // the alarm so it doesn't fire forever.
    try {
      await chrome.alarms.clear(JOB_ALARM_PREFIX + jobId);
    } catch (e) {
      // Best effort.
    }
    return;
  }
  // Hygiene cap: a job "usually under a minute" that is still unresolved
  // after 30 minutes is dead — stop the alarm instead of polling forever.
  if (job.created_ts && Date.now() - job.created_ts > 30 * 60 * 1000) {
    await finalizeJob(job, {
      state: "failed",
      result: null,
      credits_billed: null,
      error: "Timed out waiting for the result",
    });
    return;
  }

  const settings = await getSettings();
  if (!settings.token) return;

  const resp = await backendFetch(
    settings.backendUrl,
    "/result?job_id=" + encodeURIComponent(jobId),
    settings.token,
    { method: "GET" }
  );

  if (!resp.ok) {
    if (resp.status === 401) {
      // Bad/expired rep token. Do NOT finalize (the job may well be running
      // fine server-side — finalizing would lie about its fate). Surface the
      // set-your-token banner; the alarm keeps ticking so polling resumes by
      // itself once the token is fixed, and the 30-minute TTL above still
      // caps a job that never recovers.
      broadcast({ type: "enrich_auth_error" });
      return;
    }
    if (resp.status === 404) {
      // The job row is gone server-side — nothing will ever answer. This is
      // a client-side finalization: we never saw a terminal billing number,
      // so credits_billed stays null (renders as "billing unknown").
      await finalizeJob(job, {
        state: "failed",
        result: null,
        credits_billed: null,
        error: "The job is no longer on the server",
      });
      return;
    }
    if (resp.status >= 400 && resp.status < 500) {
      // Other 4xx: could be a transient middlebox/deploy artifact — don't
      // kill polling on a single one. Finalize (billing unknown) only after
      // 3 CONSECUTIVE 4xx polls; the counter persists on the job record so
      // it survives SW restarts, and any successful poll resets it.
      const errs = (Number(job.err4xx) || 0) + 1;
      if (errs >= 3) {
        await finalizeJob(job, {
          state: "failed",
          result: null,
          credits_billed: null,
          error: resp.error || "Lookup failed",
        });
      } else {
        job.err4xx = errs;
        try {
          await chrome.storage.session.set({ [jobStoreKey(jobId)]: job });
        } catch (e) {
          // Best effort — worst case the counter restarts.
        }
      }
      return;
    }
    // status 0 (network blip) or 5xx (transient server error): leave the
    // alarm running; it will retry in 30s.
    return;
  }

  // Successful poll — reset the consecutive-4xx counter if it was climbing.
  if (job.err4xx) {
    delete job.err4xx;
    try {
      await chrome.storage.session.set({ [jobStoreKey(jobId)]: job });
    } catch (e) {
      // Best effort.
    }
  }

  const data = resp.data || {};
  const state = String(data.state || "");

  if (state === "queued" || state === "running") {
    await mergeEnrichEntry(job.url, job.field, { state: state });
    broadcast({ type: "enrich_update", url: job.url, field: job.field });
    return;
  }

  if (state === "done" || state === "failed") {
    await finalizeJob(job, {
      state: state,
      result: data.result || null,
      credits_billed:
        typeof data.credits_billed === "number" ? data.credits_billed : null,
      error: data.error ? String(data.error) : null,
    });
  }
}

async function finalizeJob(job, final) {
  // Single-entry: claim the job SYNCHRONOUSLY (no await between the check
  // and the add) so the 30s alarm poll and the panel's 3s fast poll can't
  // both slip past the guard and double-run the side effects below. The
  // stored-record check still covers a job finalized by a previous SW life
  // (finalizedJobs is in-memory only).
  if (finalizedJobs.has(job.job_id)) return;
  finalizedJobs.add(job.job_id);
  const still = await getJob(job.job_id);
  if (!still) return; // an earlier SW life already finalized it

  await unregisterJob(job.job_id);
  await mergeEnrichEntry(
    job.url,
    job.field,
    Object.assign({ ts: Date.now() }, final)
  );
  broadcast({ type: "enrich_update", url: job.url, field: job.field });
  notifyJobFinished(job, final);
  refreshStatusAndBroadcast();
}

function countHits(result) {
  let n = 0;
  if (result && Array.isArray(result.emails)) n += result.emails.length;
  if (result && Array.isArray(result.phones)) n += result.phones.length;
  return n;
}

// Notification icon. This extension ships no image files, but
// chrome.notifications requires an iconUrl, so we hand it a 1x1 transparent
// PNG (a data: URL is a valid iconUrl). Drop in your own icon here if you
// want branding on the completion notification.
const NOTIF_ICON =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";

function getNotifIcon() {
  return Promise.resolve(NOTIF_ICON);
}

async function notifyJobFinished(job, final) {
  const label = FIELD_LABELS[job.field] || "Enrichment";
  const who = job.profile_name ? " for " + job.profile_name : "";
  // Billing honesty: "not billed" is a claim, so it renders ONLY when the
  // server said credits_billed === 0 on a terminal state. Client-side
  // finalizations (30-min TTL, vanished job row, repeated 4xx) never saw a
  // billing number → say the billing is unknown instead. Attached jobs
  // (started by a teammate) attribute the starter and make no billing claim
  // at all — the credits are theirs.
  const startedBy = job.attached
    ? " — started by " + (job.attached_by || "a teammate")
    : "";
  function billingSuffix() {
    if (job.attached) return startedBy;
    if (final.credits_billed === 0) return " — not billed";
    return " — billing unknown (check your credit balance)";
  }
  let message;
  if (final.state === "failed") {
    message =
      label + " lookup failed" + (final.error ? " — " + final.error : "");
    message += billingSuffix();
  } else if (countHits(final.result) === 0) {
    message = "No " + label.toLowerCase() + " found" + who + billingSuffix();
  } else if (job.attached) {
    message = label + " found" + who + startedBy;
  } else {
    const billed = final.credits_billed;
    const cost =
      typeof billed === "number"
        ? " — " + billed + " credit" + (billed === 1 ? "" : "s")
        : " — billing unknown (check your credit balance)";
    message = label + " found" + who + cost;
  }

  const iconUrl = await getNotifIcon();
  if (!iconUrl) return; // canvas unavailable — skip; the panel still shows it
  try {
    chrome.notifications.create(
      "enrichjob:" + job.job_id,
      {
        type: "basic",
        iconUrl: iconUrl,
        title: "Prospecting Plugin",
        message: message,
      },
      () => {
        void chrome.runtime.lastError; // e.g. notifications disabled at OS level
      }
    );
  } catch (e) {
    // Notification is best-effort; the result is already in session storage.
  }
}

// Clicking the notification opens the side panel in the last-focused window
// (sidePanel.open needs a windowId; the click counts as a user gesture).
chrome.notifications.onClicked.addListener((notifId) => {
  if (!String(notifId).startsWith("enrichjob:")) return;
  chrome.notifications.clear(notifId);
  chrome.windows.getLastFocused((win) => {
    if (chrome.runtime.lastError || !win || win.id === undefined) return;
    chrome.sidePanel.open({ windowId: win.id }).catch(() => {});
  });
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm && alarm.name && alarm.name.startsWith(JOB_ALARM_PREFIX)) {
    pollJob(alarm.name.slice(JOB_ALARM_PREFIX.length));
  }
});

// --------------------------------------------------------- resolve / commit
// Phases 3-4. Both are authenticated pass-throughs: the PANEL builds the
// JSON bodies (they contain no token and no backend URL), the SW attaches
// the Authorization header and enforces the origin allowlist exactly like
// every other backend call. Idempotency keys are minted panel-side, one
// per flow, so preview + confirm of the same flow share a key.

async function handleResolve(msg) {
  const settings = await getSettings();
  if (!settings.token) {
    return { ok: false, status: 401, error: "No rep token set" };
  }
  const body = msg && msg.body && typeof msg.body === "object" ? msg.body : {};
  return backendFetch(settings.backendUrl, "/resolve", settings.token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function handleCommit(msg) {
  const settings = await getSettings();
  if (!settings.token) {
    return { ok: false, status: 401, error: "No rep token set" };
  }
  const body = msg && msg.body && typeof msg.body === "object" ? msg.body : {};
  const resp = await backendFetch(settings.backendUrl, "/commit", settings.token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  // A confirmed commit can change server-side counts (creates, promotes) —
  // refresh the header numbers. Previews (confirm:false) change nothing.
  if (resp.ok && body.confirm === true) refreshStatusAndBroadcast();
  return resp;
}

// Record view: POST /record for a full company/contact read. Pure
// authenticated pass-through — the panel sends {record_type, id}, the SW
// forwards {"type", "id"} with the Authorization header attached here.
async function handleRecord(msg) {
  const settings = await getSettings();
  if (!settings.token) {
    return { ok: false, status: 401, error: "No rep token set" };
  }
  const rtype = String(msg.record_type || "");
  if (rtype !== "company" && rtype !== "contact") {
    return { ok: false, status: 0, error: "Unknown record type" };
  }
  return backendFetch(settings.backendUrl, "/record", settings.token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: rtype, id: String(msg.id || "") }),
  });
}

async function handleGetStatus() {
  const settings = await getSettings();
  if (!settings.token) {
    return { ok: false, status: 401, error: "No rep token set" };
  }
  return backendFetch(settings.backendUrl, "/status", settings.token, {
    method: "GET",
  });
}

// After a job completes, re-fetch /status so the panel header's credit
// counts stay honest (workspace_balance + spent_today).
async function refreshStatusAndBroadcast() {
  const resp = await handleGetStatus();
  if (resp.ok) broadcast({ type: "status_update", data: resp.data });
}

// ---------------------------------------------------------------- messages

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;

  if (msg.type === "get_active_tab") {
    chrome.tabs.query({ active: true, lastFocusedWindow: true }, (tabs) => {
      const tab = tabs && tabs[0];
      const url = (tab && tab.url) || "";
      sendResponse({
        tabId: tab ? tab.id : null,
        url: url,
        page_title: pageTitle(tab),
        surface: classifySurface(url),
      });
    });
    return true; // async sendResponse
  }

  if (msg.type === "recognize") {
    handleRecognize(msg).then(sendResponse);
    return true;
  }

  if (msg.type === "test_connection") {
    handleTestConnection(msg).then(sendResponse);
    return true;
  }

  if (msg.type === "enrich") {
    handleEnrich(msg).then(sendResponse);
    return true;
  }

  if (msg.type === "resolve") {
    handleResolve(msg).then(sendResponse);
    return true;
  }

  if (msg.type === "commit") {
    handleCommit(msg).then(sendResponse);
    return true;
  }

  if (msg.type === "record") {
    handleRecord(msg).then(sendResponse);
    return true;
  }

  if (msg.type === "get_status") {
    handleGetStatus().then(sendResponse);
    return true;
  }

  // Panel fast-poll tick: poll one outstanding job NOW (the 30s alarm keeps
  // running as the suspension-proof baseline). Results flow back via the
  // "enrich_update" broadcast, not via this response.
  if (msg.type === "poll_job") {
    pollJob(String(msg.job_id || "")).then(() => sendResponse({ ok: true }));
    return true;
  }

  return false;
});
