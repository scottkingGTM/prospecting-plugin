// Prospecting Plugin — side panel (ES module).
//
// Rendering hygiene: EVERYTHING that came from the server is CRM/user data
// and is treated as untrusted. All DOM is built with createElement +
// textContent. No innerHTML with server data, anywhere.
//
// The panel never touches the rep token: all backend calls go through the
// service worker, which attaches the Authorization header.

const main = document.getElementById("main");
const dot = document.getElementById("dot");
const surfaceLabel = document.getElementById("surface-label");
const creditsLabel = document.getElementById("credits");

// Current context. reqSeq guards against stale responses overwriting a
// newer tab's render (rep flips tabs faster than the network).
let currentUrl = "";
let currentSurface = "noise";
// Tab title for the current tab — forwarded to /recognize so the SERVER
// can parse the person's real name out of it (prospector/recognize.py).
// The slug alone would autofill a garbled first/last name; the title is
// where the real name lives.
let currentTitle = "";
let reqSeq = 0;

// Enrichment state for the CURRENT url. Mirrors the `enrich` key the
// service worker merges into the per-URL session cache entry:
//   { work_email: {job_id, state, by, result, credits_billed, error, ts}, ... }
let currentEnrich = {};
let enrichBanner = null; // transient {cls, text} shown inside the enrich card
let lastData = null; // last recognize payload rendered (for cheap re-renders)
let fastPollTimer = null; // 3s fast-poll while the panel is open

// Open record sheet for the CURRENT url (null = normal view). Persisted
// under `recordView` in the same per-URL session entry as recognize/enrich/
// draft state, so a tab flip away and back restores the open record.
//   { type: "company"|"contact", id, state: "loading"|"done"|"error",
//     status, error, data, descExpanded }
let recordView = null;

// Mirror of prospector/guards.py CONSUMER_EMAIL_DOMAINS — keep in
// lockstep. Used only to avoid PREFILLING a consumer domain as a company
// domain; the server's guard remains the enforcement layer.
const CONSUMER_EMAIL_DOMAINS = new Set([
  "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
  "icloud.com", "aol.com", "proton.me", "protonmail.com", "msn.com",
  "live.com", "me.com", "comcast.net", "att.net", "verizon.net",
]);

const SURFACE_LABELS = {
  linkedin_profile: "LinkedIn profile",
  linkedin_company: "LinkedIn company",
  linkedin_other: "LinkedIn",
  sales_nav: "Sales Navigator",
  website: "Website",
  noise: "",
};

// ------------------------------------------------------------- tiny helpers

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clearMain() {
  while (main.firstChild) main.removeChild(main.firstChild);
}

function kvRow(label, value) {
  const kv = el("div", "kv");
  kv.appendChild(el("span", "k", label));
  kv.appendChild(el("span", null, value));
  return kv;
}

function setDot(state, title) {
  dot.className = "dot dot-" + state;
  dot.title = title || state;
}

// Read the first present, non-null key from an object (the backend's field
// names may evolve; be forgiving on read, strict on render).
function pick(obj, ...keys) {
  if (!obj) return null;
  for (const k of keys) {
    if (obj[k] !== undefined && obj[k] !== null && obj[k] !== "") return obj[k];
  }
  return null;
}

function tierChipText(tier) {
  if (tier === null || tier === undefined || tier === "") return "Untiered";
  return /^tier/i.test(String(tier)) ? String(tier) : "Tier " + tier;
}

function fmtMoney(n) {
  if (n === null || n === undefined || n === "") return null;
  const v = Number(n);
  if (!isFinite(v)) return null;
  if (Math.abs(v) >= 1e6) return "$" + (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (Math.abs(v) >= 1e3) return "$" + Math.round(v / 1e3) + "k";
  return "$" + Math.round(v);
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Date-only strings ("2020-03-31") parse as UTC MIDNIGHT under new Date(),
// which getMonth()/getDate() then read back in local time — off by one day
// anywhere west of Greenwich. Append a local-midnight
// time part so bare dates render as the calendar day the server meant.
function parseDateish(iso) {
  let s = String(iso);
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) s += "T00:00:00";
  return new Date(s);
}

function fmtMonthYear(iso) {
  if (!iso) return null;
  const d = parseDateish(iso);
  if (isNaN(d.getTime())) return String(iso);
  return MONTHS[d.getMonth()] + " " + d.getFullYear();
}

function fmtDate(iso) {
  if (!iso) return null;
  const d = parseDateish(iso);
  if (isNaN(d.getTime())) return String(iso);
  return MONTHS[d.getMonth()] + " " + d.getDate() + ", " + d.getFullYear();
}

function fmtAge(seconds) {
  const s = Number(seconds);
  if (!isFinite(s) || s < 0) return "";
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  return Math.round(s / 3600) + "h ago";
}

function hostnameOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch (e) {
    return "";
  }
}

function openUrl(url) {
  // Only open http(s) URLs the server handed us — nothing else.
  if (typeof url === "string" && /^https?:\/\//i.test(url)) {
    chrome.tabs.create({ url });
  }
}

// --------------------------------------------------------------- state cache
// chrome.storage.session: instant repaint when the rep returns to a tab.
// The server has its own 24h cache, so the background refresh is cheap.

const CACHE_PREFIX = "recog:";
const CACHE_MAX_ENTRIES = 200;

function urlKey(url) {
  let base = String(url).split("#")[0];
  try {
    const u = new URL(base);
    const host = u.hostname.toLowerCase();
    // LinkedIn URLs: drop the query string entirely (matches the server's
    // cache-key semantics) so ?utm variants of the same profile/company
    // don't multiply cache entries.
    if (host === "linkedin.com" || host.endsWith(".linkedin.com")) {
      base = u.origin + u.pathname;
    }
  } catch (e) {
    // Not a parseable URL — fall through with the hash-stripped string.
  }
  return CACHE_PREFIX + base;
}

async function cacheGet(url) {
  try {
    const key = urlKey(url);
    const stored = await chrome.storage.session.get(key);
    return stored[key] || null;
  } catch (e) {
    return null;
  }
}

async function cachePut(url, response) {
  try {
    const key = urlKey(url);
    // Preserve the `enrich` key the service worker merges into this same
    // entry — a fresh recognize must never wipe enrichment jobs/results.
    // Same for `draft`: an in-progress add-to-HubSpot flow must survive a
    // background recognize refresh.
    const prev = await chrome.storage.session.get(key);
    const entry = { response, ts: Date.now() };
    if (prev[key] && prev[key].enrich) entry.enrich = prev[key].enrich;
    if (prev[key] && prev[key].draft) entry.draft = prev[key].draft;
    if (prev[key] && prev[key].recordView) entry.recordView = prev[key].recordView;
    await chrome.storage.session.set({ [key]: entry });
    // LRU cap: keep at most CACHE_MAX_ENTRIES per-URL results, evicting the
    // oldest by stored ts, so a long browsing session can't grow the
    // session-storage cache without bound.
    const all = await chrome.storage.session.get(null);
    const entries = Object.entries(all).filter(([k]) =>
      k.startsWith(CACHE_PREFIX)
    );
    if (entries.length > CACHE_MAX_ENTRIES) {
      entries.sort((a, b) => ((a[1] && a[1].ts) || 0) - ((b[1] && b[1].ts) || 0));
      const evict = entries
        .slice(0, entries.length - CACHE_MAX_ENTRIES)
        .map(([k]) => k);
      await chrome.storage.session.remove(evict);
    }
  } catch (e) {
    // Quota or transient failure — the panel still works without the cache.
  }
}

// ----------------------------------------------------------------- renderers

function renderIdle() {
  clearMain();
  const box = el("div", "state-msg");
  box.appendChild(el("div", "big", "Nothing to recognize here"));
  box.appendChild(
    el("div", null, "Browse to a LinkedIn profile or a company website.")
  );
  main.appendChild(box);
  setDot("idle", "Idle");
}

function renderUnsupported(message) {
  clearMain();
  const box = el("div", "state-msg");
  box.appendChild(el("div", "big", "Sales Navigator page"));
  box.appendChild(
    el(
      "div",
      null,
      message ||
        "Enrichment can't use Sales Nav URLs. Open the person's public LinkedIn profile to work this person."
    )
  );
  main.appendChild(box);
  setDot("idle", "Unsupported surface");
}

function renderLoading() {
  if (recordView) return renderRecordSheet(); // sheet stays on top
  clearMain();
  const box = el("div", "skeleton");
  box.appendChild(el("div", "skel-bar w60"));
  box.appendChild(el("div", "skel-bar w80"));
  box.appendChild(el("div", "skel-bar w40"));
  box.appendChild(el("div", "skel-bar w80"));
  main.appendChild(box);
}

function renderError(status, message) {
  // An open record sheet is never clobbered by a (background) recognize
  // failure — the error resurfaces on ← Back / the next recognize.
  if (recordView) return renderRecordSheet();
  clearMain();
  setDot("err", "Error");
  const banner = el("div", "banner error");

  if (status === 401) {
    banner.appendChild(
      el("div", null, "Not signed in — the backend rejected your token.")
    );
    const link = el("button", "btn link", "Set your token in options");
    link.addEventListener("click", () => chrome.runtime.openOptionsPage());
    banner.appendChild(link);
  } else {
    banner.appendChild(el("div", null, message || "Something went wrong."));
    const retry = el("button", "btn", "Retry");
    retry.style.marginTop = "6px";
    retry.addEventListener("click", () => recognize({ force: false }));
    banner.appendChild(retry);
  }
  main.appendChild(banner);
}


// Company-surface match list: {hs_company_id, name, domain, icp_tier,
// is_target, preferred} per the wire contract. The preferred match is
// highlighted and tagged.
function buildCompanyMatches(matches, mergeCandidate) {
  const card = el("div", "card");
  card.appendChild(el("div", "card-title", "Company matches"));

  for (const m of matches) {
    const row = el("div", m && m.preferred ? "match-row preferred" : "match-row");

    const nameLine = el("div", "match-name");
    nameLine.appendChild(el("span", null, pick(m, "name") || "(unnamed)"));
    if (m && m.preferred) {
      nameLine.appendChild(el("span", "chip preferred-tag", "preferred"));
    }
    row.appendChild(nameLine);

    const domain = pick(m, "domain");
    if (domain) row.appendChild(el("div", "match-domain", domain));

    const chips = el("div", "chip-row");
    chips.appendChild(el("span", "chip tier", tierChipText(pick(m, "icp_tier"))));
    if (m && m.is_target) {
      chips.appendChild(el("span", "chip target", "Target account"));
    }
    row.appendChild(chips);

    card.appendChild(row);
  }

  if (mergeCandidate) {
    card.appendChild(
      el(
        "div",
        "banner subdued",
        "Two HubSpot records share this domain — flagged for merge review."
      )
    );
  }

  return card;
}

function buildContactCard(contact) {
  const card = el("div", "card");
  card.appendChild(el("div", "card-title", "Contact"));

  const name =
    pick(contact, "name", "full_name") ||
    [pick(contact, "first_name"), pick(contact, "last_name")]
      .filter(Boolean)
      .join(" ") ||
    "(no name)";
  card.appendChild(el("div", "contact-name", name));

  const rows = [
    ["Title", pick(contact, "title", "jobtitle", "job_title")],
    ["Company", pick(contact, "company", "company_name")],
    ["Lifecycle", pick(contact, "lifecycle_stage", "lifecyclestage", "lifecycle")],
    ["Modified", fmtDate(pick(contact, "last_modified", "lastmodifieddate", "updated_at"))],
  ];
  for (const [label, value] of rows) {
    if (value === null) continue;
    const kv = el("div", "kv");
    kv.appendChild(el("span", "k", label));
    kv.appendChild(el("span", null, value));
    card.appendChild(kv);
  }

  const btns = el("div", "btn-row");
  const hsUrl = pick(contact, "hubspot_url");
  if (hsUrl) {
    const btn = el("button", "btn primary", "Open in HubSpot");
    btn.addEventListener("click", () => openUrl(hsUrl));
    btns.appendChild(btn);
  }
  const contactId = pick(contact, "hs_contact_id", "contact_id", "id");
  if (contactId !== null) {
    const view = el("button", "btn view-record", "View record →");
    view.addEventListener("click", () => openRecord("contact", contactId));
    btns.appendChild(view);
  }
  if (btns.firstChild) card.appendChild(btns);

  return card;
}

// ---------------------------------------------------------------- record view
// A read-only "what do we have on this record?" sheet so a rep doesn't have
// to open HubSpot. It is a panel state LAYERED over the current view: "← Back"
// returns to whatever was on screen, and the open record is persisted per URL
// so tab flips restore it. All data comes from POST /record via the service
// worker; render discipline is unchanged (createElement/textContent only).

async function recordPut(url, rv) {
  try {
    const key = urlKey(url);
    const stored = await chrome.storage.session.get(key);
    const entry = stored[key] || { response: null, ts: Date.now() };
    if (rv) entry.recordView = rv;
    else delete entry.recordView;
    await chrome.storage.session.set({ [key]: entry });
  } catch (e) {
    // Storage hiccup — the in-memory recordView still drives this panel life.
  }
}

function openRecord(type, id) {
  recordView = {
    type: String(type),
    id: String(id),
    state: "loading",
    status: 0,
    error: null,
    data: null,
    descExpanded: false,
  };
  fetchRecord();
}

function closeRecord() {
  recordView = null;
  recordPut(currentUrl, null);
  if (lastData) renderResult(lastData);
  else recognize({});
}

async function fetchRecord() {
  const url = currentUrl;
  const rv = recordView;
  if (!rv) return;
  rv.state = "loading";
  rv.error = null;
  recordPut(url, rv);
  renderRecordSheet();

  const resp = await chrome.runtime
    .sendMessage({ type: "record", record_type: rv.type, id: rv.id })
    .catch(() => null);

  // Closed or replaced while the fetch was out (same URL): drop it —
  // persisting now would resurrect a record the rep dismissed.
  if (url === currentUrl && recordView !== rv) return;

  if (!resp) {
    rv.state = "error";
    rv.status = 0;
    rv.error = "The extension background worker did not respond.";
  } else if (!resp.ok) {
    rv.state = "error";
    rv.status = resp.status;
    rv.error = resp.error || null;
  } else {
    rv.state = "done";
    rv.status = resp.status;
    rv.data = resp.data || {};
  }
  // Rep flipped tabs mid-fetch: persist the result to ITS url's entry (the
  // flip back restores it instantly) but don't paint over the new page.
  recordPut(url, rv);
  if (url === currentUrl && recordView === rv) renderRecordSheet();
}

// Tier text for record chips. HubSpot's raw ICP-tier property values look
// like "tier_2"; humanize those, and pass an already-display value through
// tierChipText.
function recordTierText(tier) {
  const m = String(tier === null || tier === undefined ? "" : tier).match(
    /^tier[_\s]?(\d+)$/i
  );
  if (m) return "Tier " + m[1];
  return tierChipText(tier);
}

// HubSpot booleans arrive as the strings "true"/"false" as often as real
// booleans — treat both honestly.
function isTruthyFlag(v) {
  return v === true || String(v).toLowerCase() === "true";
}

function kvCopyRow(label, value) {
  const kv = el("div", "kv");
  kv.appendChild(el("span", "k", label));
  const val = el("span", "kv-copy");
  val.appendChild(el("span", null, value));
  val.appendChild(copyButton(value));
  kv.appendChild(val);
  return kv;
}

// Details grid: rows with empty values are omitted ENTIRELY (no "—" rows).
function buildRecordGrid(rows) {
  const grid = el("div", "record-grid");
  for (const [label, value] of rows) {
    if (value === null || value === undefined || value === "") continue;
    grid.appendChild(kvRow(label, value));
  }
  return grid;
}

function buildCompanyRecordSheet(data) {
  const frag = document.createDocumentFragment();
  const rec = (data && data.record) || {};

  const card = el("div", "card");
  card.appendChild(el("div", "card-title", "Company record"));
  card.appendChild(el("div", "record-title", pick(rec, "name") || "(unnamed company)"));

  const chips = el("div", "chip-row");
  chips.appendChild(el("span", "chip tier", recordTierText(pick(rec, "hs_ideal_customer_profile"))));
  if (isTruthyFlag(pick(rec, "hs_is_target_account"))) {
    chips.appendChild(el("span", "chip target", "Target account"));
  }
  card.appendChild(chips);

  const hsUrl = pick(data, "hubspot_url");
  if (hsUrl) {
    const row = el("div", "btn-row");
    const open = el("button", "btn primary", "Open in HubSpot");
    open.addEventListener("click", () => openUrl(hsUrl));
    row.appendChild(open);
    card.appendChild(row);
  }

  const cityState = [pick(rec, "city"), pick(rec, "state")]
    .filter(Boolean)
    .join(", ");
  card.appendChild(
    buildRecordGrid([
      ["Domain", pick(rec, "domain")],
      ["Industry", pick(rec, "industry")],
      ["Employees", pick(rec, "numberofemployees")],
      ["Location", cityState || null],
      ["Lifecycle", pick(rec, "lifecyclestage")],
      ["Phone", pick(rec, "phone")],
      ["Website", pick(rec, "website")],
      ["Created", fmtDate(pick(rec, "createdate"))],
      ["Modified", fmtDate(pick(rec, "hs_lastmodifieddate"))],
      ["Owner", pick(data, "owner_name")],
    ])
  );

  const desc = String(pick(rec, "description") || "");
  if (desc) {
    const rv = recordView;
    const expanded = !!(rv && rv.descExpanded);
    card.appendChild(
      el("div", "record-desc" + (expanded ? "" : " clamped"), desc)
    );
    if (desc.length > 220) {
      const toggle = el("button", "btn link", expanded ? "Show less" : "Show more");
      toggle.addEventListener("click", () => {
        if (!recordView) return;
        recordView.descExpanded = !recordView.descExpanded;
        recordPut(currentUrl, recordView);
        renderRecordSheet();
      });
      card.appendChild(toggle);
    }
  }
  frag.appendChild(card);

  const contacts = Array.isArray(data.contacts) ? data.contacts : [];
  const ccard = el("div", "card");
  ccard.appendChild(el("div", "card-title", "Contacts (" + contacts.length + ")"));
  if (!contacts.length) {
    ccard.appendChild(el("div", "match-note", "No contacts on this company."));
  }
  for (const c of contacts) {
    if (!c) continue;
    const row = el("div", "match-row");
    const name =
      [pick(c, "firstname"), pick(c, "lastname")].filter(Boolean).join(" ") ||
      "(no name)";
    const nameLine = el("div", "match-name");
    nameLine.appendChild(el("span", null, name));
    const lifecycle = pick(c, "lifecyclestage");
    if (lifecycle) nameLine.appendChild(el("span", "chip", lifecycle));
    row.appendChild(nameLine);
    const title = pick(c, "jobtitle");
    if (title) row.appendChild(el("div", "match-domain", title));
    const email = pick(c, "email");
    if (email) {
      const line = el("div", "kv-copy");
      line.appendChild(el("span", "match-domain", email));
      line.appendChild(copyButton(email));
      row.appendChild(line);
    }
    ccard.appendChild(row);
  }
  frag.appendChild(ccard);

  return frag;
}

function buildContactRecordSheet(data) {
  const frag = document.createDocumentFragment();
  const rec = (data && data.record) || {};

  const card = el("div", "card");
  card.appendChild(el("div", "card-title", "Contact record"));
  const name =
    [pick(rec, "firstname"), pick(rec, "lastname")].filter(Boolean).join(" ") ||
    "(no name)";
  card.appendChild(el("div", "record-title", name));

  const lifecycle = pick(rec, "lifecyclestage");
  if (lifecycle) {
    const chips = el("div", "chip-row");
    chips.appendChild(el("span", "chip", lifecycle));
    card.appendChild(chips);
  }

  const hsUrl = pick(data, "hubspot_url");
  if (hsUrl) {
    const row = el("div", "btn-row");
    const open = el("button", "btn primary", "Open in HubSpot");
    open.addEventListener("click", () => openUrl(hsUrl));
    row.appendChild(open);
    card.appendChild(row);
  }

  const grid = el("div", "record-grid");
  const title = pick(rec, "jobtitle");
  if (title) grid.appendChild(kvRow("Title", title));
  const email = pick(rec, "email");
  if (email) grid.appendChild(kvCopyRow("Email", email));
  const extra = String(pick(rec, "hs_additional_emails") || "")
    .split(/[;,]/)
    .map((s) => s.trim())
    .filter(Boolean);
  for (const addr of extra) grid.appendChild(kvCopyRow("Also", addr));
  const phone = pick(rec, "phone");
  if (phone) grid.appendChild(kvRow("Phone", phone));
  const li = pick(rec, "hs_linkedin_url");
  if (li) {
    const kv = el("div", "kv");
    kv.appendChild(el("span", "k", "LinkedIn"));
    const btn = el("button", "btn link", "Open profile");
    btn.addEventListener("click", () => openUrl(li)); // http(s)-gated in openUrl
    kv.appendChild(btn);
    grid.appendChild(kv);
  }
  const rest = [
    ["Owner", pick(data, "owner_name")],
    ["Created", fmtDate(pick(rec, "createdate"))],
    ["Modified", fmtDate(pick(rec, "lastmodifieddate"))],
    ["Last contacted", fmtDate(pick(rec, "notes_last_contacted"))],
    ["Deals", pick(rec, "num_associated_deals")],
  ];
  for (const [label, value] of rest) {
    if (value === null || value === undefined || value === "") continue;
    grid.appendChild(kvRow(label, value));
  }
  card.appendChild(grid);
  frag.appendChild(card);

  const comp = data.company && typeof data.company === "object" ? data.company : null;
  if (comp) {
    const cc = el("div", "card");
    cc.appendChild(el("div", "card-title", "Company"));
    cc.appendChild(el("div", "contact-name", pick(comp, "name") || "(unnamed company)"));
    const domain = pick(comp, "domain");
    if (domain) cc.appendChild(el("div", "match-domain", domain));
    const chips = el("div", "chip-row");
    chips.appendChild(el("span", "chip tier", recordTierText(pick(comp, "tier"))));
    if (isTruthyFlag(comp.is_target_account)) {
      chips.appendChild(el("span", "chip target", "Target account"));
    }
    cc.appendChild(chips);
    const cUrl = pick(data, "company_hubspot_url");
    if (cUrl) {
      const row = el("div", "btn-row");
      const open = el("button", "btn", "Open company in HubSpot");
      open.addEventListener("click", () => openUrl(cUrl));
      row.appendChild(open);
      cc.appendChild(row);
    }
    frag.appendChild(cc);
  }

  return frag;
}

function renderRecordSheet() {
  const rv = recordView;
  if (!rv) return;
  clearMain();

  const back = el("div", "back-row");
  const backBtn = el("button", "btn", "← Back");
  backBtn.addEventListener("click", () => closeRecord());
  back.appendChild(backBtn);
  main.appendChild(back);

  if (rv.state === "loading") {
    const box = el("div", "skeleton");
    box.appendChild(el("div", "skel-bar w60"));
    box.appendChild(el("div", "skel-bar w80"));
    box.appendChild(el("div", "skel-bar w40"));
    box.appendChild(el("div", "skel-bar w80"));
    main.appendChild(box);
    return;
  }

  if (rv.state === "error") {
    if (rv.status === 404) {
      main.appendChild(
        el(
          "div",
          "banner subdued",
          "Record no longer exists (may have been merged)."
        )
      );
      return;
    }
    const banner = el("div", "banner error");
    banner.appendChild(
      el(
        "div",
        null,
        rv.status === 401
          ? "The backend rejected your token — set your rep token in the extension options."
          : rv.error || "Something went wrong."
      )
    );
    main.appendChild(banner);
    const retry = el("button", "btn", "Retry");
    retry.addEventListener("click", () => fetchRecord());
    main.appendChild(retry);
    return;
  }

  const data = rv.data || {};
  main.appendChild(
    rv.type === "contact"
      ? buildContactRecordSheet(data)
      : buildCompanyRecordSheet(data)
  );
}

// Possible-match list on a red profile: contacts who might be this person
// but have no LinkedIn URL on file (server wire contract: possible_matches =
// [{hs_contact_id, name, jobtitle, email_domain, lifecycle_stage, owner_name,
// hubspot_url}], possible_match_note rendered verbatim). Phase 4 adds the
// one-click link write: an inline confirm, then commit shape B (a
// single-field write of THIS tab's LinkedIn URL onto that contact).
function buildPossibleMatches(matches, note) {
  const card = el("div", "card");
  card.appendChild(el("div", "card-title", "Possibly already in HubSpot"));

  for (const m of matches) {
    const row = el("div", "match-row");
    row.appendChild(el("div", "match-name", pick(m, "name") || "(no name)"));

    const title = pick(m, "jobtitle");
    if (title) row.appendChild(el("div", "match-domain", title));

    const domain = pick(m, "email_domain");
    if (domain) row.appendChild(el("div", "match-domain", "@" + domain));

    const chips = el("div", "chip-row");
    const lifecycle = pick(m, "lifecycle_stage");
    if (lifecycle) chips.appendChild(el("span", "chip", lifecycle));
    const owner = pick(m, "owner_name");
    if (owner) chips.appendChild(el("span", "chip", "Owner: " + owner));
    if (chips.firstChild) row.appendChild(chips);

    const hsUrl = pick(m, "hubspot_url");
    if (hsUrl) {
      const btn = el("button", "btn link", "Open in HubSpot");
      btn.addEventListener("click", () => openUrl(hsUrl)); // https-gated in openUrl
      row.appendChild(btn);
    }

    const id = String(pick(m, "hs_contact_id") || "");
    if (id) row.appendChild(buildLinkControl(m, id));

    card.appendChild(row);
  }

  if (note) card.appendChild(el("div", "banner subdued", note));
  return card;
}

// Per-row link-profile control. It's a CRM write, so a deliberate inline
// confirm sits between the button and the POST. State lives in linkBusy
// (in-memory, keyed by hs_contact_id, reset on every URL change) — a
// successful link immediately re-runs recognize with force_refresh, which
// flips the profile green and removes this card entirely.
function buildLinkControl(m, id) {
  const wrap = el("div", "link-ctl");
  const state = linkBusy[id];
  const name = pick(m, "name") || "this contact";

  if (!state) {
    const btn = el("button", "btn", "Link this profile → " + name);
    btn.addEventListener("click", () => {
      linkBusy[id] = { step: "confirm" };
      rerender();
    });
    wrap.appendChild(btn);
    return wrap;
  }

  if (state.step === "confirm") {
    wrap.appendChild(
      el(
        "div",
        "match-note",
        "Writes this LinkedIn URL onto " + name + "'s HubSpot record."
      )
    );
    const btns = el("div", "btn-row");
    const yes = el("button", "btn primary", "Confirm link");
    yes.addEventListener("click", () => doLinkProfile(id));
    btns.appendChild(yes);
    const no = el("button", "btn link", "Cancel");
    no.addEventListener("click", () => {
      delete linkBusy[id];
      rerender();
    });
    btns.appendChild(no);
    wrap.appendChild(btns);
    return wrap;
  }

  if (state.step === "posting") {
    const line = el("div", "finding");
    line.appendChild(el("span", "spinner"));
    line.appendChild(el("span", null, "linking…"));
    wrap.appendChild(line);
    return wrap;
  }

  if (state.step === "done") {
    if (state.dry) {
      wrap.appendChild(
        el(
          "div",
          "banner warn",
          "DRY RUN — nothing was written to HubSpot. This link WOULD have been saved."
        )
      );
    } else {
      const line = el("div", "finding");
      line.appendChild(el("span", "spinner"));
      line.appendChild(el("span", null, "linked — re-checking this profile…"));
      wrap.appendChild(line);
    }
    return wrap;
  }

  // step === "error"
  wrap.appendChild(el("div", "banner error", state.text || "Link failed."));
  if (!state.final) {
    const retry = el("button", "btn link", "Try again");
    retry.addEventListener("click", () => {
      linkBusy[id] = { step: "confirm" };
      rerender();
    });
    wrap.appendChild(retry);
  }
  return wrap;
}

async function doLinkProfile(id) {
  const url = currentUrl;
  linkBusy[id] = { step: "posting" };
  rerender();

  // Single-field write — no preview step by design: the inline confirm IS
  // the deliberate click, and the payload is one URL onto one record.
  const resp = await chrome.runtime
    .sendMessage({
      type: "commit",
      body: {
        idempotency_key: crypto.randomUUID(),
        confirm: true,
        link_linkedin: { hs_contact_id: id, linkedin_url: url },
      },
    })
    .catch(() => null);

  // Rep flipped tabs mid-write: linkBusy is per-URL state and was reset by
  // onSurfaceChanged; don't paint a stale row onto the new page.
  if (url !== currentUrl) return;

  if (resp && resp.ok) {
    const dry = !!(resp.data && resp.data.dry_run === true);
    linkBusy[id] = { step: "done", dry: dry };
    rerender();
    // The contact now carries this LinkedIn URL — a forced recognize should
    // come back green. (On a dry run nothing changed, so don't re-check.)
    if (!dry) recognize({ force: true });
    return;
  }

  const err = (resp && resp.data) || {};
  const detailMsg =
    (err.detail && typeof err.detail === "object" && err.detail.message) || null;
  if (resp && resp.status === 422 && err.error === "linkedin_conflict") {
    linkBusy[id] = {
      step: "error",
      text:
        detailMsg ||
        "Already linked to a different profile — likely a different person.",
      final: true,
    };
  } else if (resp && resp.status === 401) {
    linkBusy[id] = {
      step: "error",
      text: "The backend rejected your token — set your rep token in the extension options.",
      final: true,
    };
  } else {
    linkBusy[id] = {
      step: "error",
      text: detailMsg || (resp && resp.error) || "Link failed.",
      final: false,
    };
  }
  rerender();
}

// ---------------------------------------------------------------- enrichment
// Phase 2, "honest async": each priced button POSTs /enrich through the
// service worker, which owns the job from there (30s alarms + notification).
// The panel only displays state and fast-polls (3s) while it's open.
// Prices in the labels are FIXED COPY — every number rendered next to a
// result (cost, balance, spent) comes from the server, never client math.

const ENRICH_FIELDS = [
  { field: "work_email", button: "Get work email — 1 credit", label: "Work email" },
  { field: "mobile", button: "Get mobile — 10 credits", label: "Mobile" },
  { field: "personal_email", button: "Get personal email — 3 credits", label: "Personal email" },
];

function fieldSpec(field) {
  return ENRICH_FIELDS.find((s) => s.field === field) || null;
}

// Header credit counts, straight off /status (workspace_balance may be
// null = unknown; render nothing for it in that case).
function renderCredits(status) {
  if (!status || typeof status !== "object") return;
  const parts = [];
  if (status.workspace_balance !== null && status.workspace_balance !== undefined) {
    parts.push("Credits: " + status.workspace_balance);
  }
  if (status.spent_today !== null && status.spent_today !== undefined) {
    parts.push("you today: " + status.spent_today);
  }
  creditsLabel.textContent = parts.join(" · ");
}

async function refreshStatus() {
  const resp = await chrome.runtime
    .sendMessage({ type: "get_status" })
    .catch(() => null);
  if (resp && resp.ok) renderCredits(resp.data);
}

function statusChip(status) {
  const s = String(status || "unknown").toLowerCase();
  const cls =
    s === "verified" ? "verified"
    : s === "risky" ? "risky"
    : s === "inferred" ? "inferred"
    : "unknown";
  const chip = el("span", "status-chip " + cls, s);
  if (cls === "inferred") {
    chip.title = "model-guessed — cannot be committed as an email";
  }
  return chip;
}

// Copy-to-clipboard button, shared by enrichment hit rows and the record
// sheet's email rows.
function copyButton(value) {
  const copy = el("button", "btn icon copy", "⧉");
  copy.title = "Copy";
  copy.addEventListener("click", () => {
    navigator.clipboard
      .writeText(String(value))
      .then(() => {
        copy.textContent = "✓";
        setTimeout(() => {
          copy.textContent = "⧉";
        }, 1200);
      })
      .catch(() => {});
  });
  return copy;
}

// One found email/phone: value · type · status chip · provider · cost · copy.
function buildHitRow(value, type, status, provider, cost) {
  const row = el("div", "hit-row");
  row.appendChild(el("span", "hit-value", value));
  if (type) row.appendChild(el("span", "hit-type", type));
  row.appendChild(statusChip(status));
  if (provider) row.appendChild(el("span", "provider-tag", provider));
  if (typeof cost === "number" && isFinite(cost)) {
    row.appendChild(el("span", "hit-cost", cost + " cr"));
  }
  row.appendChild(copyButton(value));
  return row;
}

function collectHitRows(result) {
  const rows = [];
  if (result && Array.isArray(result.emails)) {
    for (const e of result.emails) {
      if (!e) continue;
      const cost = Number(e.cost_credits);
      rows.push(
        buildHitRow(
          pick(e, "address") || "(no address)",
          pick(e, "type"),
          pick(e, "status"),
          pick(e, "provider"),
          isFinite(cost) ? cost : null
        )
      );
    }
  }
  if (result && Array.isArray(result.phones)) {
    for (const p of result.phones) {
      if (!p) continue;
      const cost = Number(p.cost_credits);
      rows.push(
        buildHitRow(
          pick(p, "number") || "(no number)",
          pick(p, "type"),
          pick(p, "status"),
          pick(p, "provider"),
          isFinite(cost) ? cost : null
        )
      );
    }
  }
  return rows;
}

function buildEnrichCard(titleText) {
  const card = el("div", "card");
  card.appendChild(el("div", "card-title", titleText));

  if (enrichBanner) {
    card.appendChild(el("div", "banner " + enrichBanner.cls, enrichBanner.text));
  }

  for (const spec of ENRICH_FIELDS) {
    const entry = currentEnrich[spec.field];
    const section = el("div", "enrich-field");

    if (
      entry &&
      (entry.state === "pending" ||
        entry.state === "queued" ||
        entry.state === "running")
    ) {
      // In flight ("pending" = the SW's POST is still on the wire): this
      // field shows the honest-async line; the OTHER buttons stay enabled.
      const line = el("div", "finding");
      line.appendChild(el("span", "spinner"));
      line.appendChild(
        el(
          "span",
          null,
          "finding " + spec.label.toLowerCase() + "… usually 1–3 minutes"
        )
      );
      section.appendChild(line);
      if (entry.by) {
        section.appendChild(
          el("div", "match-note", "Started by " + entry.by + " — you'll both see the result.")
        );
      }
    } else {
      const btn = el("button", "btn enrich", spec.button);
      btn.addEventListener("click", () => startEnrich(spec.field));
      section.appendChild(btn);

      // Billing honesty: "not billed" is only claimed when the server
      // reported credits_billed === 0 on the terminal state. Client-side
      // finalizations (timeout, vanished job, repeated 4xx) never carried a
      // billing number → "billing unknown". Attached jobs (entry.by set)
      // attribute the teammate who started them and make no billing claim.
      if (entry && entry.state === "done") {
        const rows = collectHitRows(entry.result);
        if (rows.length === 0) {
          let text = spec.label + ": not found";
          if (entry.by) text += " — started by " + entry.by;
          else if (entry.credits_billed === 0) text += " — not billed";
          else text += " — billing unknown";
          section.appendChild(el("div", "hit-row none", text));
        } else {
          for (const r of rows) section.appendChild(r);
          if (entry.by) {
            section.appendChild(
              el("div", "match-note", "Started by " + entry.by)
            );
          }
        }
      } else if (entry && entry.state === "failed") {
        let text = spec.label + " lookup failed";
        if (entry.error) text += " — " + entry.error;
        if (entry.by) text += " — started by " + entry.by;
        else if (entry.credits_billed === 0) text += " — not billed";
        else text += " — billing unknown";
        section.appendChild(el("div", "hit-row none failed", text));
      }
    }

    card.appendChild(section);
  }

  return card;
}

function rerender() {
  if (lastData) renderResult(lastData, { rerender: true });
}

function inFlightJobIds() {
  const ids = [];
  for (const key of Object.keys(currentEnrich)) {
    const e = currentEnrich[key];
    if (e && (e.state === "queued" || e.state === "running") && e.job_id) {
      ids.push(e.job_id);
    }
  }
  return ids;
}

// 3s fast-poll while the panel is open: asks the SW to poll each in-flight
// job NOW. Updates come back via the "enrich_update" broadcast. The timer
// kills itself once nothing is in flight (the SW's 30s alarms remain the
// suspension-proof baseline either way).
let fastPollBusy = false; // at most ONE in-flight poll round at a time

function ensureFastPoll() {
  if (fastPollTimer) return;
  fastPollTimer = setInterval(() => {
    const ids = inFlightJobIds();
    if (!ids.length) {
      clearInterval(fastPollTimer);
      fastPollTimer = null;
      return;
    }
    // Skip the tick if the previous round hasn't returned — a slow backend
    // must not stack concurrent /result polls (and concurrent finalizations).
    if (fastPollBusy) return;
    fastPollBusy = true;
    Promise.all(
      ids.map((id) =>
        chrome.runtime
          .sendMessage({ type: "poll_job", job_id: id })
          .catch(() => {})
      )
    ).finally(() => {
      fastPollBusy = false;
    });
  }, 3000);
}

async function startEnrich(field) {
  if (currentSurface !== "linkedin_profile") return;
  const spec = fieldSpec(field);
  if (!spec) return;
  const url = currentUrl;

  // Optimistic flip to "finding…" — the SW confirms (with a job_id) or the
  // error path below rolls it back.
  enrichBanner = null;
  currentEnrich[field] = { state: "queued", ts: Date.now() };
  rerender();

  const profileName =
    (lastData && lastData.contact && pick(lastData.contact, "name", "full_name")) || "";

  // Identity for the vendor (enrichment vendors reject nameless requests):
  // green contact > title-derived name_hint > nothing (the server then falls
  // back to its cached hint).
  const hint = (lastData && lastData.name_hint) || {};
  const nameParts = String(
    profileName || pick(hint, "full_name") || ""
  ).split(/\s+/).filter(Boolean);
  const resp = await chrome.runtime
    .sendMessage({
      type: "enrich",
      url: url,
      linkedin_url: url,
      field: field,
      profile_name: profileName,
      first_name: pick(hint, "first_name") || nameParts[0] || "",
      last_name: pick(hint, "last_name") || nameParts.slice(-1)[0] || "",
      company_domain: "",
      company_name: "",
    })
    .catch(() => null);

  // Rep switched tabs mid-request: session storage (written by the SW) is
  // the source of truth for that other URL; don't touch this panel state.
  if (url !== currentUrl) return;

  if (!resp) {
    delete currentEnrich[field];
    enrichBanner = { cls: "error", text: "The extension background worker did not respond." };
    rerender();
    return;
  }

  if (resp.busy) {
    // The SW refused a duplicate: an attempt for this field is already
    // pending/queued/running. Keep the spinner and sync the REAL state
    // (with its job_id) from session storage so the fast-poll tracks it.
    const cached = await cacheGet(url);
    if (url !== currentUrl) return;
    if (cached && cached.enrich) currentEnrich = cached.enrich;
    if (inFlightJobIds().length) ensureFastPoll();
    rerender();
    return;
  }

  if (resp.ok && resp.data && resp.data.job_id) {
    currentEnrich[field] = {
      job_id: String(resp.data.job_id),
      state: "queued",
      ts: Date.now(),
    };
    ensureFastPoll();
    refreshStatus(); // 202 changes reserved/spent — keep the header honest
    rerender();
    return;
  }

  const errData = resp.data || {};

  if (resp.status === 402) {
    delete currentEnrich[field];
    const cap = pick(errData, "cap");
    const spent = pick(errData, "spent");
    enrichBanner = {
      cls: "error",
      text:
        cap !== null && spent !== null
          ? "Daily credit cap reached (spent " + spent + " of " + cap + ") — resets midnight UTC"
          : "Daily credit cap reached — resets midnight UTC",
    };
  } else if (resp.status === 409) {
    const by = pick(errData, "by") || "Someone";
    if (errData.job_id) {
      // Someone else's job — the SW already attached to it; poll it exactly
      // as if it were ours.
      currentEnrich[field] = {
        job_id: String(errData.job_id),
        state: "queued",
        by: String(by),
        ts: Date.now(),
      };
      ensureFastPoll();
    } else {
      // 409 without a job_id: nothing to attach to or poll — show the
      // banner but roll the optimistic spinner back so the button returns
      // (flipping to "queued" here would strand a spinner forever).
      delete currentEnrich[field];
    }
    enrichBanner = { cls: "subdued", text: by + " is already enriching this profile" };
  } else if (resp.status === 400 && errData.error === "sales_nav_url") {
    // Server-side classifier says this is a Sales Nav URL after all.
    delete currentEnrich[field];
    renderUnsupported();
    return;
  } else if (resp.status === 503) {
    delete currentEnrich[field];
    enrichBanner = { cls: "subdued", text: "Enrichment isn't configured yet." };
  } else if (resp.status === 401) {
    delete currentEnrich[field];
    renderError(401, resp.error);
    return;
  } else {
    delete currentEnrich[field];
    enrichBanner = { cls: "error", text: resp.error || "Enrichment request failed." };
  }
  rerender();
}

// ------------------------------------------------------ add-to-HubSpot flow
// Phases 3-4 (resolve → commit). The whole flow lives in ONE draft object,
// persisted under `draft` in the same per-URL session entry as recognize +
// enrichment state — so it survives tab flips and panel reopens. The
// idempotency key is minted ONCE per flow (crypto.randomUUID at flow start):
// preview (confirm:false) and confirm (confirm:true) send the SAME key, so a
// retried confirm can't create two contacts. A new key exists only when the
// rep starts a NEW flow.
//
// Stages: resolving → resolve → form → previewing → preview → committing →
// done, with hold_domain (422 domain_mismatch), conflict (409), and
// resolve_error as side exits.

let currentDraft = null; // the flow draft for currentUrl (null = no flow)
let linkBusy = {}; // hs_contact_id -> {step,...} for the link-profile rows

async function draftPut(url, draft) {
  try {
    const key = urlKey(url);
    const stored = await chrome.storage.session.get(key);
    const entry = stored[key] || { response: null, ts: Date.now() };
    if (draft) entry.draft = draft;
    else delete entry.draft;
    await chrome.storage.session.set({ [key]: entry });
  } catch (e) {
    // Storage hiccup — the in-memory draft still drives this panel life.
  }
}

function clearDraft(opts = {}) {
  currentDraft = null;
  draftPut(currentUrl, null);
  // After a real create the profile is in HubSpot now — force a fresh
  // recognize so the panel flips green instead of re-showing stale red.
  if (opts.refresh) recognize({ force: true });
  else rerender();
}

// Bare domain from whatever the vendor handed back (domain, website URL...).
function normDomain(value) {
  if (!value) return "";
  let s = String(value).trim().toLowerCase();
  s = s.replace(/^https?:\/\//, "").replace(/^www\./, "");
  return s.split("/")[0].split("?")[0];
}

// Best-effort (First, Last) from the /in/<slug> path — mirror of the
// server's slug_name_guess, only as the LAST-RESORT fallback when neither
// the enrichment result nor the server's tab-title name_hint carried a
// name (see startAddFlow's preference chain — the garbled-slug-name failure
// mode). Drops LinkedIn's numeric/hex de-dup suffix and initials. Returns
// null for single-word slugs on purpose: an empty field invites a fix, a
// slug-cased mash of a name invites a bad record.
function slugNameGuess(url) {
  let path = "";
  try {
    path = new URL(url).pathname;
  } catch (e) {
    return null;
  }
  const m = path.match(/^\/in\/([^/]+)/);
  if (!m) return null;
  let slug = m[1];
  try {
    slug = decodeURIComponent(slug);
  } catch (e) {
    // Keep the raw slug.
  }
  const tokens = slug.split("-").filter(Boolean);
  while (
    tokens.length &&
    /^(?:\d+|[0-9a-f]{5,})$/i.test(tokens[tokens.length - 1]) &&
    /\d/.test(tokens[tokens.length - 1])
  ) {
    tokens.pop();
  }
  const words = tokens.filter((t) => t.length > 1 && /^[a-z][a-z.'’]*$/i.test(t));
  if (words.length < 2) return null;
  const cap = (w) => w.charAt(0).toUpperCase() + w.slice(1);
  return { first: cap(words[0]), last: cap(words[words.length - 1]) };
}

function anyEnrichDone() {
  for (const spec of ENRICH_FIELDS) {
    const e = currentEnrich[spec.field];
    if (e && e.state === "done") return true;
  }
  return false;
}

// Merged view over every DONE enrichment field for the current URL:
// all emails (de-duped by address), all phones, and the vendor's
// profile/company passthrough dicts.
function collectEnrichDone() {
  const emails = [];
  const phones = [];
  let profile = {};
  let company = {};
  for (const spec of ENRICH_FIELDS) {
    const e = currentEnrich[spec.field];
    if (!e || e.state !== "done" || !e.result) continue;
    const r = e.result;
    if (Array.isArray(r.emails)) {
      for (const em of r.emails) if (em && em.address) emails.push(em);
    }
    if (Array.isArray(r.phones)) {
      for (const p of r.phones) if (p && p.number) phones.push(p);
    }
    if (r.profile && typeof r.profile === "object") {
      profile = Object.assign({}, r.profile, profile);
    }
    if (r.company && typeof r.company === "object") {
      company = Object.assign({}, r.company, company);
    }
  }
  const seen = new Set();
  const uniq = emails.filter((e) => {
    const a = String(e.address).toLowerCase();
    if (seen.has(a)) return false;
    seen.add(a);
    return true;
  });
  return { emails: uniq, phones: phones, profile: profile, company: company };
}

// ---------------------------------------------------- late-arriving results
// A pilot review found: rep starts "Get work email", opens the add flow,
// then clicks "Get mobile" — the mobile landed in the enrich card but the
// open draft's phone stayed empty. These functions flow a terminal DONE
// enrichment into the open draft, with two hard rules: only EMPTY fields are
// filled (rep input always wins), and anything the rep will CONFIRM must
// first be re-previewed (a merge on the preview step steps back to the form).

// Phone preference for a late merge: mobile first, then direct, then any
// other non-inferred hit.
function pickMergePhone(phones) {
  const usable = (phones || []).filter(
    (p) => p && p.number && p.status !== "inferred"
  );
  const byType = (t) =>
    usable.find((p) => String(p.type || "").toLowerCase() === t);
  return byType("mobile") || byType("direct") || usable[0] || null;
}

// Merge every DONE enrichment result into the draft's form. Fills ONLY
// empty fields, appends NEW non-inferred emails to the picker, and returns
// human-readable "what arrived" strings ([] = nothing usable — all inferred,
// or every field already filled). Mutates draft.form; callers persist.
function mergeEnrichIntoDraft(draft) {
  if (!draft || !draft.form) return [];
  const f = draft.form;
  const found = collectEnrichDone();
  const merged = [];

  // Emails: append addresses the picker doesn't have yet. Inferred stays
  // excluded — the server refuses to commit those (see initForm).
  const usable = found.emails.filter((e) => e && e.status !== "inferred");
  const have = new Set(
    (f.emails || []).map((e) => String(e.address).toLowerCase())
  );
  const fresh = [];
  for (const e of usable) {
    const addr = String(e.address).toLowerCase();
    if (have.has(addr)) continue;
    have.add(addr);
    fresh.push({
      address: String(e.address),
      status: String(e.status || "unknown"),
    });
  }
  if (fresh.length) {
    f.emails = (f.emails || []).concat(fresh);
    for (const e of fresh) merged.push("email " + e.address);
    // Select the best new address only if the email field itself is empty —
    // a rep-chosen (or rep-cleared) selection is never overridden.
    if (!String(f.email || "").trim()) {
      const best = fresh.find((e) => e.status === "verified") || fresh[0];
      f.email = best.address;
    }
  }
  // Keep the "N inferred excluded" note honest as late results land.
  f.inferredCount = found.emails.length - usable.length;

  // Phone: only into an empty field.
  if (!String(f.phone || "").trim()) {
    const hit = pickMergePhone(found.phones);
    if (hit) {
      f.phone = String(hit.number);
      merged.push(String(hit.type || "phone") + " " + f.phone);
    }
  }

  // Names / job title from the vendor profile, only where blank.
  const fills = [
    ["first_name", pick(found.profile, "firstname"), "first name"],
    ["last_name", pick(found.profile, "lastname"), "last name"],
    [
      "jobtitle",
      pick(found.profile, "title", "job_title", "headline"),
      "job title",
    ],
  ];
  for (const [key, value, label] of fills) {
    if (value && !String(f[key] || "").trim()) {
      f[key] = String(value);
      merged.push(label + " " + f[key]);
    }
  }
  return merged;
}

// Apply a (possibly queued) merge to a draft sitting on the FORM or PREVIEW
// step. If anything landed: a preview steps back to the form and drops the
// stored preview — what gets confirmed must always match what was previewed
// — and a dismissible note says what arrived. Persists immediately so the
// merged draft survives a tab flip.
function applyLateMerge(draft) {
  draft.pendingMerge = false;
  const merged = mergeEnrichIntoDraft(draft);
  if (merged.length) {
    if (draft.stage !== "form") {
      draft.stage = "form";
      draft.preview = null;
    }
    draft.mergeNote = "Added from enrichment: " + merged.join(", ");
  }
  draftPut(currentUrl, draft);
  return merged.length > 0;
}

// Entry point when a terminal DONE result lands (or is restored) while a
// draft exists for the current URL. Mid-submit ("previewing"/"committing")
// the draft is never touched: the merge is queued and applied when the
// response lands — and only if the commit did not succeed (buildAddFlow
// applies the queued flag once the draft is back on form/preview). Other
// stages (resolve, hold, conflict, done) are left alone: resolve re-reads
// enrichment fresh in initForm, and the exception cards demand the rep's
// explicit decision first.
function maybeMergeLateEnrich() {
  const draft = currentDraft;
  if (!draft || !draft.form) return;
  if (draft.stage === "previewing" || draft.stage === "committing") {
    draft.pendingMerge = true;
    draftPut(currentUrl, draft);
    return;
  }
  if (draft.stage !== "form" && draft.stage !== "preview") return;
  applyLateMerge(draft);
}

function fmtTier(tier) {
  if (tier === "tier_1") return "Tier 1";
  if (tier === "tier_2") return "Tier 2";
  if (tier === "tier_3") return "Tier 3";
  return tier ? String(tier) : "none";
}

// Owner-source badge for the would-do report: where the owner assignment
// came from. Triage is the loud one — no owner could be resolved.
function ownerBadge(source) {
  const s = String(source || "").toLowerCase().replace(/[\s-]+/g, "_");
  if (!s) return null;
  if (s === "needs_triage" || s === "triage") {
    return el("span", "badge triage", "NEEDS TRIAGE");
  }
  if (s === "company_owner") return el("span", "badge", "account owner");
  if (s === "rep") return el("span", "badge", "you");
  return el("span", "badge", String(source));
}

async function startAddFlow() {
  const url = currentUrl;
  const found = collectEnrichDone();
  // Name preference chain (the garbled-slug-name failure mode):
  //   1. enrich profile first/last — vendor data, live mode only;
  //   2. the recognize payload's name_hint — server-parsed from the tab
  //      TITLE, i.e. the person's real display name;
  //   3. client slug-guess — last resort (null for single-word slugs:
  //      show nothing rather than garbage).
  const hint = (lastData && lastData.name_hint) || null;
  const guess = slugNameGuess(url);
  const first =
    pick(found.profile, "firstname") ||
    (hint && hint.first_name) ||
    (guess && guess.first) ||
    "";
  const last =
    pick(found.profile, "lastname") ||
    (hint && hint.last_name) ||
    (guess && guess.last) ||
    "";
  // Matching email: best NON-inferred address. An inferred (model-guessed)
  // email must never drive a match — it could weld this person onto the
  // wrong record.
  const matchEmail = found.emails.find((e) => e && e.status !== "inferred");
  // Company name: enrich result first, then the title-derived hint (set on
  // company surfaces; usually absent on profiles — same chain, one rung
  // shorter).
  const companyName =
    pick(found.company, "company", "company_name") ||
    (hint && hint.company_name) ||
    "";
  const draft = {
    stage: "resolving",
    idempotency_key: crypto.randomUUID(),
    contact: {
      first_name: String(first),
      last_name: String(last),
      email: matchEmail ? String(matchEmail.address) : "",
      linkedin_url: url,
      company_name: String(companyName),
    },
    company: {
      name: String(companyName),
      domain: normDomain(
        pick(found.company, "domain", "company_domain", "company_website")
      ),
      state: "",
      linkedin_url: String(pick(found.company, "company_linkedin_url") || ""),
    },
    resolve: null,
    companyChoice: null,
    form: null,
    preview: null,
    outcome: null,
    hold: null,
    conflict: null,
    notice: null,
    error: null,
    pendingMerge: false, // late enrichment queued while a submit was in flight
    mergeNote: null, // dismissible "Added from enrichment: …" line on the form
  };
  currentDraft = draft;
  await runResolve(url, draft);
}

// POST /resolve for a draft. Also re-run after a 409 conflict ("refresh
// matches") — the draft (and its idempotency key) survives; only the match
// lists are refetched.
async function runResolve(url, draft) {
  draft.stage = "resolving";
  draft.error = null;
  draft.conflict = null;
  await draftPut(url, draft);
  if (url === currentUrl) rerender();

  const resp = await chrome.runtime
    .sendMessage({
      type: "resolve",
      body: { contact: draft.contact, company: draft.company },
    })
    .catch(() => null);

  if (!resp) {
    draft.stage = "resolve_error";
    draft.error = "The extension background worker did not respond.";
  } else if (!resp.ok) {
    draft.stage = "resolve_error";
    draft.error =
      resp.status === 401
        ? "The backend rejected your token — set your rep token in the extension options."
        : resp.error || "Match check failed.";
  } else {
    draft.stage = "resolve";
    draft.resolve = resp.data || {};
  }
  // Persist even if the rep flipped tabs — the draft is per-URL state.
  await draftPut(url, draft);
  if (url === currentUrl) rerender();
}

function chooseCompany(choice) {
  const draft = currentDraft;
  if (!draft) return;
  draft.companyChoice = choice;
  initForm(draft, choice);
  draft.stage = "form";
  draft.notice = null;
  draftPut(currentUrl, draft);
  rerender();
}

function initForm(draft, choice) {
  const found = collectEnrichDone();
  // Inferred emails are EXCLUDED from the picker entirely — the server
  // refuses to commit them, so offering one would be a trap.
  const usable = found.emails.filter((e) => e && e.status !== "inferred");
  const inferredCount = found.emails.length - usable.length;
  const emails = usable.map((e) => ({
    address: String(e.address),
    status: String(e.status || "unknown"),
  }));
  draft.form = {
    first_name: draft.contact.first_name,
    last_name: draft.contact.last_name,
    jobtitle: String(pick(found.profile, "title", "job_title", "headline") || ""),
    emails: emails,
    inferredCount: inferredCount,
    email: emails.length ? emails[0].address : "",
    phone: found.phones.length ? String(found.phones[0].number) : "",
    tier: "", // BLANK by default — tiering is a deliberate choice
    target_account: false,
    alternate_domain_confirmed: false,
    newCompany:
      choice && choice.create
        ? {
            name: draft.company.name,
            domain: draft.company.domain,
            state: "",
            linkedin_url: draft.company.linkedin_url,
          }
        : null,
  };
}

function validateForm(draft) {
  const f = draft.form || {};
  if (!String(f.first_name || "").trim() || !String(f.last_name || "").trim()) {
    return "First and last name are required.";
  }
  if (draft.companyChoice && draft.companyChoice.create) {
    const nc = f.newCompany || {};
    if (!String(nc.name || "").trim()) return "The new company needs a name.";
  }
  return null;
}

function buildCommitBody(draft, confirmFlag) {
  const f = draft.form || {};
  const emails = Array.isArray(f.emails) ? f.emails : [];
  const sel = emails.find((e) => e && e.address === f.email) || null;
  const choice = draft.companyChoice || {};
  const nc = f.newCompany || {};
  const company = choice.create
    ? {
        new: {
          name: String(nc.name || "").trim(),
          domain: normDomain(nc.domain || ""),
          state: String(nc.state || "").trim(),
          linkedin_url: String(nc.linkedin_url || "").trim(),
        },
      }
    : { hs_company_id: String(choice.hs_company_id || "") };
  return {
    idempotency_key: draft.idempotency_key,
    confirm: !!confirmFlag,
    contact: {
      first_name: String(f.first_name || "").trim(),
      last_name: String(f.last_name || "").trim(),
      jobtitle: String(f.jobtitle || "").trim(),
      email: sel ? sel.address : "",
      email_status: sel ? sel.status : null,
      phone: String(f.phone || "").trim(),
      linkedin_url: draft.contact.linkedin_url,
    },
    company: company,
    tier: f.tier || null,
    target_account: !!f.target_account,
    alternate_domain_confirmed: !!f.alternate_domain_confirmed,
  };
}

// The wire contract for confirm:false is {preview: {<plan>}}. Belt and
// suspenders (from a pilot fix): older server builds shipped the
// plan under `would` (with preview:true), and a bare plan is conceivable —
// tolerate all three so a version skew never renders "(empty preview)".
function previewPlanOf(data) {
  const d = data && typeof data === "object" ? data : {};
  if (d.preview && typeof d.preview === "object") return d.preview;
  if (d.would && typeof d.would === "object") return d.would;
  if (d.contact_props || d.owner || d.company_new || d.company_id) return d;
  return {};
}

// POST /commit. confirm:false = preview (zero side effects), confirm:true =
// the real write. Both send the flow's idempotency key.
async function submitCommit(confirmFlag) {
  const draft = currentDraft;
  if (!draft || !draft.form) return;
  const url = currentUrl;

  const problem = validateForm(draft);
  if (problem) {
    draft.stage = "form";
    draft.notice = { cls: "error", text: problem };
    draftPut(url, draft);
    rerender();
    return;
  }

  draft.notice = null;
  const returnStage = confirmFlag ? "preview" : "form";
  draft.stage = confirmFlag ? "committing" : "previewing";
  await draftPut(url, draft);
  if (url === currentUrl) rerender();

  const resp = await chrome.runtime
    .sendMessage({ type: "commit", body: buildCommitBody(draft, confirmFlag) })
    .catch(() => null);

  if (!resp) {
    draft.stage = returnStage;
    draft.notice = {
      cls: "error",
      text: "The extension background worker did not respond.",
    };
  } else if (resp.ok) {
    if (confirmFlag) {
      draft.outcome = resp.data || {};
      draft.stage = "done";
      // The commit succeeded — a merge queued mid-flight must NOT touch
      // the record now; the enrich card still shows the late result.
      draft.pendingMerge = false;
    } else {
      draft.preview = previewPlanOf(resp.data);
      draft.stage = "preview";
    }
  } else {
    applyCommitError(draft, resp, confirmFlag, returnStage);
  }
  await draftPut(url, draft);
  if (url === currentUrl) rerender();
}

function applyCommitError(draft, resp, confirmFlag, returnStage) {
  const err = resp.data || {};
  const code = String(err.error || "");
  const detail =
    err.detail && typeof err.detail === "object" ? err.detail : {};
  const detailMsg = pick(detail, "message") || pick(err, "message");

  if (resp.status === 422 && code === "domain_mismatch") {
    // The hold card — the rep decides: drop the email, or vouch that the
    // company legitimately uses this second domain.
    draft.hold = { detail: detail, confirmFlag: !!confirmFlag };
    draft.stage = "hold_domain";
    return;
  }
  if (resp.status === 422 && code === "consumer_email") {
    // Informational, not scary: a gmail/yahoo address can't anchor a
    // company match.
    draft.stage = "form";
    draft.notice = {
      cls: "subdued",
      text:
        detailMsg ||
        "That's a consumer email address (gmail/yahoo/…) — it can't be committed here. Pick a work email or continue without one.",
    };
    return;
  }
  if (resp.status === 422) {
    const texts = {
      inferred_email:
        "That email is inferred (model-guessed) — it can't be written to HubSpot. Pick a different email or none.",
      auto_create_trap:
        "Blocked: this looks like an auto-created shell record trap.",
      linkedin_conflict:
        "This LinkedIn URL is already on a different HubSpot contact — likely a different person.",
    };
    draft.stage = "form";
    draft.notice = {
      cls: "error",
      text:
        detailMsg ||
        texts[code] ||
        "Blocked — " + (code ? code.replace(/_/g, " ") : "invalid request"),
    };
    return;
  }
  if (resp.status === 409) {
    // The world changed under us (teammate created the contact, company
    // merged/appeared). Never retry blindly — show what happened and offer
    // a fresh resolve.
    draft.conflict = { code: code, detail: detail };
    draft.stage = "conflict";
    return;
  }
  if (resp.status === 402) {
    draft.stage = returnStage;
    draft.notice = {
      cls: "error",
      text:
        detailMsg ||
        "Daily promote cap reached — no more tier/target promotions today. Resets midnight UTC.",
    };
    return;
  }
  if (resp.status === 401) {
    draft.stage = returnStage;
    draft.notice = {
      cls: "error",
      text: "The backend rejected your token — set your rep token in the extension options.",
    };
    return;
  }
  draft.stage = returnStage;
  draft.notice = { cls: "error", text: resp.error || "Commit failed." };
}

// ----- flow renderers --------------------------------------------------

function buildResolveCard(draft) {
  const card = el("div", "card");
  card.appendChild(el("div", "card-title", "Add to HubSpot"));

  const rs = draft.resolve || {};
  const contactMatches = Array.isArray(rs.contact_matches)
    ? rs.contact_matches
    : [];
  const companyMatches = Array.isArray(rs.company_matches)
    ? rs.company_matches
    : [];
  const flags = rs.flags || {};
  const exact = contactMatches.filter((m) => m && m.confidence === "exact");
  const possible = contactMatches.filter((m) => m && m.confidence !== "exact");

  if (exact.length) {
    // An exact contact match KILLS the create path entirely: the person
    // already exists, so the only offer is opening the record.
    for (const m of exact) {
      const row = el("div", "match-row exact");
      row.appendChild(
        el(
          "div",
          "exact-headline",
          pick(m, "hubspot_url")
            ? "Already exists — open in HubSpot"
            : "Already exists in HubSpot"
        )
      );
      row.appendChild(el("div", "match-name", pick(m, "name") || "(no name)"));
      const bits = [pick(m, "jobtitle"), pick(m, "email")]
        .filter(Boolean)
        .join(" · ");
      if (bits) row.appendChild(el("div", "match-domain", bits));
      const matchedOn = pick(m, "matched_on");
      if (matchedOn) {
        const chips = el("div", "chip-row");
        chips.appendChild(el("span", "chip", "matched on " + matchedOn));
        row.appendChild(chips);
      }
      const hsUrl = pick(m, "hubspot_url");
      if (hsUrl) {
        const btn = el("button", "btn primary", "Open in HubSpot");
        btn.style.marginTop = "6px";
        btn.addEventListener("click", () => openUrl(hsUrl));
        row.appendChild(btn);
      }
      card.appendChild(row);
    }
    card.appendChild(
      el("div", "banner subdued", "This person already exists — nothing to create.")
    );
    const close = el("button", "btn link", "Close");
    close.addEventListener("click", () => clearDraft());
    card.appendChild(close);
    return card;
  }

  if (possible.length) {
    card.appendChild(
      el("div", "match-note", "Possible existing contacts — check these before creating:")
    );
    for (const m of possible) {
      const row = el("div", "match-row");
      const nameLine = el("div", "match-name");
      nameLine.appendChild(el("span", null, pick(m, "name") || "(no name)"));
      nameLine.appendChild(el("span", "chip possible", "possible"));
      row.appendChild(nameLine);
      const bits = [pick(m, "jobtitle"), pick(m, "email")]
        .filter(Boolean)
        .join(" · ");
      if (bits) row.appendChild(el("div", "match-domain", bits));
      const matchedOn = pick(m, "matched_on");
      if (matchedOn) {
        row.appendChild(el("div", "match-domain", "matched on " + matchedOn));
      }
      const hsUrl = pick(m, "hubspot_url");
      if (hsUrl) {
        const btn = el("button", "btn link", "Open in HubSpot");
        btn.addEventListener("click", () => openUrl(hsUrl));
        row.appendChild(btn);
      }
      card.appendChild(row);
    }
  }

  card.appendChild(el("div", "card-title pick-title", "Pick the company"));

  if (flags.merge_candidate) {
    card.appendChild(
      el(
        "div",
        "banner warn",
        "Two HubSpot records share this domain — flagged for merge review. Pick the preferred one."
      )
    );
  }
  if (flags.fuzzy_only) {
    card.appendChild(
      el(
        "div",
        "banner subdued",
        "Name-similarity matches only (no domain match) — double-check before picking."
      )
    );
  }

  for (const m of companyMatches) {
    if (!m) continue;
    let cls = "pick-row";
    if (m.preferred) cls += " preferred";
    if (m.subdomain_record) cls += " subdomain";
    const row = el("button", cls);
    const nameLine = el("div", "match-name");
    nameLine.appendChild(el("span", null, pick(m, "name") || "(unnamed)"));
    if (m.preferred) {
      nameLine.appendChild(el("span", "chip preferred-tag", "preferred"));
    }
    row.appendChild(nameLine);
    const sub = [pick(m, "domain"), pick(m, "state")].filter(Boolean).join(" · ");
    if (sub) row.appendChild(el("div", "match-domain", sub));
    const chips = el("div", "chip-row");
    chips.appendChild(el("span", "chip tier", tierChipText(pick(m, "icp_tier"))));
    if (m.is_target) chips.appendChild(el("span", "chip target", "Target account"));
    const matchedOn = pick(m, "matched_on");
    if (matchedOn) {
      chips.appendChild(
        el(
          "span",
          "chip",
          String(matchedOn) + (m.confidence ? " · " + m.confidence : "")
        )
      );
    }
    row.appendChild(chips);
    if (m.subdomain_record) {
      row.appendChild(
        el(
          "div",
          "subdomain-note",
          "careers/mail subdomain — don't pick as the main record"
        )
      );
    }
    row.addEventListener("click", () =>
      chooseCompany({
        hs_company_id: String(m.hs_company_id),
        name: String(pick(m, "name") || ""),
        domain: String(pick(m, "domain") || ""),
      })
    );
    card.appendChild(row);
  }

  if (!companyMatches.length) {
    card.appendChild(
      el("div", "match-note", "No matching companies found in HubSpot.")
    );
  }

  // ALWAYS the last row, and always a deliberate extra click with the
  // near-matches still on screen — never preselected, never a default.
  const createRow = el("button", "pick-row create", "Create new company…");
  createRow.addEventListener("click", () => chooseCompany({ create: true }));
  card.appendChild(createRow);

  const cancel = el("button", "btn link", "Cancel");
  cancel.addEventListener("click", () => clearDraft());
  card.appendChild(cancel);
  return card;
}

function buildCommitForm(draft) {
  const f = draft.form || {};
  const card = el("div", "card");
  card.appendChild(el("div", "card-title", "New contact — check the details"));

  if (draft.notice) {
    card.appendChild(el("div", "banner " + draft.notice.cls, draft.notice.text));
  }

  // Late-arriving enrichment that was merged into empty fields — subtle,
  // dismissible, and persisted with the draft (survives tab flips).
  if (draft.mergeNote) {
    const note = el("div", "merge-note");
    note.appendChild(el("span", null, draft.mergeNote));
    const dismiss = el("button", "btn icon dismiss", "×");
    dismiss.title = "Dismiss";
    dismiss.addEventListener("click", () => {
      draft.mergeNote = null;
      draftPut(currentUrl, draft);
      rerender();
    });
    note.appendChild(dismiss);
    card.appendChild(note);
  }

  function textField(target, labelText, key, placeholder) {
    const wrap = el("div", "form-field");
    wrap.appendChild(el("label", null, labelText));
    const input = el("input");
    input.type = "text";
    input.value = target[key] || "";
    if (placeholder) input.placeholder = placeholder;
    input.addEventListener("input", () => {
      target[key] = input.value;
      draftPut(currentUrl, draft); // fire-and-forget; survives tab flips
    });
    wrap.appendChild(input);
    return wrap;
  }

  card.appendChild(textField(f, "First name", "first_name"));
  card.appendChild(textField(f, "Last name", "last_name"));
  card.appendChild(textField(f, "Job title", "jobtitle"));

  // Email picker. Inferred emails never appear here (see initForm); the
  // purple note says how many were held back and why.
  const emailWrap = el("div", "form-field");
  emailWrap.appendChild(el("label", null, "Email"));
  const select = el("select");
  const noneOpt = el("option", null, "(no email)");
  noneOpt.value = "";
  select.appendChild(noneOpt);
  for (const e of f.emails || []) {
    const opt = el("option", null, e.address + " — " + e.status);
    opt.value = e.address;
    select.appendChild(opt);
  }
  select.value = f.email || "";
  select.addEventListener("change", () => {
    f.email = select.value;
    draftPut(currentUrl, draft);
  });
  emailWrap.appendChild(select);
  card.appendChild(emailWrap);
  if (f.inferredCount > 0) {
    card.appendChild(
      el(
        "div",
        "inferred-note",
        f.inferredCount +
          " inferred email" +
          (f.inferredCount === 1 ? "" : "s") +
          " excluded — model-guessed, can't be written to HubSpot."
      )
    );
  }

  card.appendChild(textField(f, "Phone", "phone"));

  const choice = draft.companyChoice || {};
  if (choice.create) {
    card.appendChild(el("div", "report-sub", "New company"));
    const nc = f.newCompany || (f.newCompany = { name: "", domain: "", state: "", linkedin_url: "" });
    // Prefill the domain from the selected email's domain (a pilot request)
    // — but NEVER from a consumer domain: prefilling
    // gmail.com as a company domain would tee up the exact junk-company
    // trap the commit guards exist to block. Only fills an untouched
    // field; anything the rep typed wins.
    if (!nc.domain && f.email && f.email.includes("@")) {
      const emailDomain = f.email.split("@").pop().toLowerCase().trim();
      if (emailDomain && !CONSUMER_EMAIL_DOMAINS.has(emailDomain)) {
        nc.domain = emailDomain;
      }
    }
    card.appendChild(textField(nc, "Company name", "name"));
    card.appendChild(textField(nc, "Domain", "domain", "acme.com"));
    card.appendChild(textField(nc, "State", "state", "e.g. TX"));
    card.appendChild(textField(nc, "Company LinkedIn URL", "linkedin_url"));
  } else {
    card.appendChild(
      kvRow(
        "Company",
        [choice.name, choice.domain].filter(Boolean).join(" · ") ||
          "HubSpot company " + (choice.hs_company_id || "")
      )
    );
  }

  // Tier — BLANK by default; a plain dropdown, no gates and no reason
  // field (by design — tier_1 included like the others).
  const tierWrap = el("div", "form-field");
  tierWrap.appendChild(el("label", null, "Tier"));
  const tierSel = el("select");
  for (const [v, t] of [["", "— no tier —"], ["tier_1", "Tier 1"], ["tier_2", "Tier 2"], ["tier_3", "Tier 3"]]) {
    const o = el("option", null, t);
    o.value = v;
    tierSel.appendChild(o);
  }
  tierSel.value = f.tier || "";
  tierSel.addEventListener("change", () => {
    f.tier = tierSel.value;
    draftPut(currentUrl, draft);
  });
  tierWrap.appendChild(tierSel);
  card.appendChild(tierWrap);

  const check = el("label", "form-check");
  const cb = el("input");
  cb.type = "checkbox";
  cb.checked = !!f.target_account;
  cb.addEventListener("change", () => {
    f.target_account = cb.checked;
    draftPut(currentUrl, draft);
  });
  check.appendChild(cb);
  check.appendChild(el("span", null, "Target account"));
  card.appendChild(check);

  const btns = el("div", "btn-row");
  const preview = el("button", "btn primary", "Preview");
  preview.addEventListener("click", () => submitCommit(false));
  btns.appendChild(preview);
  // From a pilot fix: an explicit way back from the form to the
  // company-pick card. Re-renders from the STORED resolve response — same
  // matches, same flow, same idempotency key; refetches only if the stored
  // response is missing (e.g. a draft restored without it).
  const back = el("button", "btn", "← Change company");
  back.addEventListener("click", () => {
    draft.notice = null;
    if (draft.resolve) {
      draft.stage = "resolve";
      draftPut(currentUrl, draft);
      rerender();
    } else {
      runResolve(currentUrl, draft);
    }
  });
  btns.appendChild(back);
  const cancel = el("button", "btn link", "Cancel");
  cancel.addEventListener("click", () => clearDraft());
  btns.appendChild(cancel);
  card.appendChild(btns);
  return card;
}

// The would-do report — used by both the preview (confirm:false) and the
// dry-run outcome. Known sections render structured; any OTHER scalar the
// server reports renders generically after them (honesty: never hide part
// of what the server says it would do).
function buildWouldReport(p) {
  const wrap = el("div", "report");
  if (!p || typeof p !== "object" || Object.keys(p).length === 0) {
    wrap.appendChild(el("div", "match-note", "(empty preview)"));
    return wrap;
  }
  const shown = new Set();

  const props = p.contact_props;
  if (props && typeof props === "object") {
    shown.add("contact_props");
    wrap.appendChild(el("div", "report-sub", "Contact"));
    for (const k of Object.keys(props)) {
      const v = props[k];
      if (v === null || v === undefined || v === "") continue;
      wrap.appendChild(kvRow(k.replace(/_/g, " "), String(v)));
    }
  }

  // COMPANY — what the write would create or touch (a pilot fix: the
  // report never showed the company). company_new is the
  // create payload of a brand-new company; company_id (+ name/domain when
  // the server had them from the live verify) is an existing record;
  // company_update_props carries the tier/target changes that would be
  // PATCHed onto it. Omitted only when the plan truly has no company
  // (e.g. the link_linkedin variant).
  const compNew =
    p.company_new && typeof p.company_new === "object"
      ? p.company_new
      : p.company_props && typeof p.company_props === "object"
        ? p.company_props // older plan shape: same payload, older key
        : null;
  const hasExisting = p.company_id !== undefined && p.company_id !== null;
  if (compNew || hasExisting) {
    for (const k of [
      "company_new", "company_props", "company_id", "company_name",
      "company_domain", "company_update_props", "tier", "target",
      "target_account",
    ]) {
      shown.add(k);
    }
    wrap.appendChild(el("div", "report-sub", "Company"));
    if (compNew) {
      const bits = [
        pick(compNew, "name"),
        pick(compNew, "domain"),
        pick(compNew, "state"),
      ].filter(Boolean);
      wrap.appendChild(
        kvRow("Will be created", bits.join(" · ") || "(no details)")
      );
      const li = pick(compNew, "linkedin_company_page", "linkedin_url");
      if (li) wrap.appendChild(kvRow("LinkedIn page", String(li)));
    } else {
      const bits = [pick(p, "company_name"), pick(p, "company_domain")]
        .filter(Boolean);
      wrap.appendChild(
        kvRow(
          "Existing record",
          bits.join(" · ") || "HubSpot company " + String(p.company_id)
        )
      );
    }
    const updates =
      p.company_update_props && typeof p.company_update_props === "object"
        ? p.company_update_props
        : {};
    if (updates.hs_ideal_customer_profile) {
      wrap.appendChild(
        kvRow("Tier", "→ " + fmtTier(updates.hs_ideal_customer_profile))
      );
    }
    if (isTruthyFlag(updates.hs_is_target_account)) {
      wrap.appendChild(kvRow("Target account", "→ yes"));
    }
  }

  const owner = p.owner;
  if (owner && typeof owner === "object") {
    shown.add("owner");
    const kv = el("div", "kv");
    kv.appendChild(el("span", "k", "Owner"));
    const val = el("span");
    val.appendChild(
      el("span", null, String(pick(owner, "name", "id") || "unassigned") + " ")
    );
    const badge = ownerBadge(pick(owner, "source"));
    if (badge) val.appendChild(badge);
    kv.appendChild(val);
    wrap.appendChild(kv);
    // WHY triage, when the server explains it (a pilot review found a bare
    // TRIAGE badge just makes reps ask).
    const why = pick(owner, "why");
    if (why) wrap.appendChild(el("div", "match-note", String(why)));
  }

  // Standalone tier/target rows only when no COMPANY section rendered them
  // (plans without a company involved).
  if ("tier" in p && !shown.has("tier")) {
    shown.add("tier");
    wrap.appendChild(kvRow("Tier", fmtTier(p.tier)));
  }
  for (const key of ["target", "target_account"]) {
    if (key in p && !shown.has(key)) {
      shown.add(key);
      wrap.appendChild(kvRow("Target account", p[key] ? "yes" : "no"));
    }
  }

  if (p.note_preview) {
    shown.add("note_preview");
    wrap.appendChild(el("div", "report-sub", "Note that will be logged"));
    wrap.appendChild(el("div", "note-preview", String(p.note_preview)));
  }

  for (const k of Object.keys(p)) {
    if (shown.has(k)) continue;
    const v = p[k];
    if (v === null || v === undefined || typeof v === "object") continue;
    wrap.appendChild(kvRow(k.replace(/_/g, " "), String(v)));
  }
  return wrap;
}

function buildPreviewCard(draft) {
  const card = el("div", "card");
  card.appendChild(el("div", "card-title", "Preview — nothing written yet"));
  if (draft.notice) {
    card.appendChild(el("div", "banner " + draft.notice.cls, draft.notice.text));
  }
  card.appendChild(buildWouldReport(draft.preview || {}));
  const btns = el("div", "btn-row");
  const confirm = el("button", "btn primary", "Confirm — create contact");
  confirm.addEventListener("click", () => submitCommit(true));
  btns.appendChild(confirm);
  const back = el("button", "btn", "Edit");
  back.addEventListener("click", () => {
    draft.stage = "form";
    draft.notice = null;
    draftPut(currentUrl, draft);
    rerender();
  });
  btns.appendChild(back);
  const cancel = el("button", "btn link", "Cancel");
  cancel.addEventListener("click", () => clearDraft());
  btns.appendChild(cancel);
  card.appendChild(btns);
  return card;
}

// The domain-mismatch hold card: both domains on screen, two honest ways
// out. "Same company" re-submits at the SAME confirm level the 422 came
// from, with alternate_domain_confirmed:true.
function buildHoldCard(draft) {
  const card = el("div", "card");
  card.appendChild(el("div", "card-title", "Hold — domains don't match"));
  const hold = draft.hold || {};
  const detail = hold.detail || {};
  card.appendChild(
    el(
      "div",
      "banner warn",
      pick(detail, "message") ||
        "The email's domain and the company's domain are different. Same company under another domain — or the wrong email?"
    )
  );
  const emailDomain = pick(detail, "email_domain");
  if (emailDomain) card.appendChild(kvRow("Email domain", String(emailDomain)));
  const companyDomain = pick(detail, "company_domain", "domain");
  if (companyDomain) {
    card.appendChild(kvRow("Company domain", String(companyDomain)));
  }

  const btns = el("div", "btn-row");
  const discard = el("button", "btn", "Discard email");
  discard.addEventListener("click", () => {
    const d = currentDraft;
    if (!d || !d.form) return;
    d.form.email = "";
    d.form.alternate_domain_confirmed = false;
    d.hold = null;
    d.stage = "form";
    d.notice = { cls: "subdued", text: "Email removed from the form — preview again." };
    draftPut(currentUrl, d);
    rerender();
  });
  btns.appendChild(discard);
  const same = el("button", "btn primary", "It's the same company (alternate domain)");
  same.addEventListener("click", () => {
    const d = currentDraft;
    if (!d || !d.form) return;
    d.form.alternate_domain_confirmed = true;
    const wasConfirm = !!(d.hold && d.hold.confirmFlag);
    d.hold = null;
    draftPut(currentUrl, d);
    submitCommit(wasConfirm);
  });
  btns.appendChild(same);
  card.appendChild(btns);

  const cancel = el("button", "btn link", "Cancel");
  cancel.addEventListener("click", () => clearDraft());
  card.appendChild(cancel);
  return card;
}

function buildConflictCard(draft) {
  const card = el("div", "card");
  card.appendChild(el("div", "card-title", "Add to HubSpot"));
  const c = draft.conflict || {};
  const detail = c.detail || {};
  const texts = {
    contact_exists:
      "This contact was just created in HubSpot (maybe by a teammate).",
    company_appeared:
      "A matching company appeared in HubSpot since you started — re-check the matches.",
    company_id_stale:
      "The company you picked no longer exists (merged or deleted) — re-check the matches.",
  };
  card.appendChild(
    el(
      "div",
      "banner warn",
      pick(detail, "message") ||
        texts[c.code] ||
        "The record changed on HubSpot's side — refresh the matches."
    )
  );
  const hsUrl = pick(detail, "hubspot_url");
  if (hsUrl) {
    const open = el("button", "btn", "Open the record");
    open.addEventListener("click", () => openUrl(hsUrl));
    card.appendChild(open);
  }
  const btns = el("div", "btn-row");
  const refresh = el("button", "btn primary", "Refresh matches");
  refresh.addEventListener("click", () => {
    const d = currentDraft;
    if (d) runResolve(currentUrl, d);
  });
  btns.appendChild(refresh);
  const cancel = el("button", "btn link", "Cancel");
  cancel.addEventListener("click", () => clearDraft());
  btns.appendChild(cancel);
  card.appendChild(btns);
  return card;
}

function buildOutcomeCard(draft) {
  const card = el("div", "card");
  const o = draft.outcome || {};

  if (o.dry_run === true) {
    // The backend is in rehearsal mode — say so LOUDLY. Nothing exists in
    // HubSpot after this "success".
    card.appendChild(
      el(
        "div",
        "banner warn big",
        "DRY RUN — nothing was written to HubSpot. This is exactly what WOULD happen:"
      )
    );
    card.appendChild(buildWouldReport(o.would || {}));
    const done = el("button", "btn", "Done");
    done.style.marginTop = "8px";
    done.addEventListener("click", () => clearDraft());
    card.appendChild(done);
    return card;
  }

  const msg =
    (o.idempotent ? "Already created by this flow. " : "") +
    (pick(o, "message") || "In HubSpot now.");
  card.appendChild(el("div", "banner sync", msg));

  if (o.owner && typeof o.owner === "object") {
    const kv = el("div", "kv");
    kv.appendChild(el("span", "k", "Owner"));
    const val = el("span");
    val.appendChild(
      el("span", null, String(pick(o.owner, "name", "id") || "unassigned") + " ")
    );
    const badge = ownerBadge(pick(o.owner, "source"));
    if (badge) val.appendChild(badge);
    kv.appendChild(val);
    card.appendChild(kv);
    const ownerWhy = pick(o.owner, "why");
    if (ownerWhy) card.appendChild(el("div", "match-note", String(ownerWhy)));
  }

  const btns = el("div", "btn-row");
  const hsUrl = pick(o, "hubspot_url");
  if (hsUrl) {
    const open = el("button", "btn primary", "Open in HubSpot");
    open.addEventListener("click", () => openUrl(hsUrl));
    btns.appendChild(open);
  }
  const done = el("button", "btn", "Done");
  done.addEventListener("click", () => clearDraft({ refresh: true }));
  btns.appendChild(done);
  card.appendChild(btns);
  return card;
}

function buildFlowSpinner(text) {
  const card = el("div", "card");
  const line = el("div", "finding");
  line.appendChild(el("span", "spinner"));
  line.appendChild(el("span", null, text));
  card.appendChild(line);
  return card;
}

// Entry point from renderResult: the "Add to HubSpot…" button (red profile,
// at least one enrichment DONE) or the in-progress flow at whatever stage
// it's at. Returns null when there's nothing to show.
function buildAddFlow(verdict) {
  const draft = currentDraft;
  if (draft && draft.pendingMerge &&
      (draft.stage === "form" || draft.stage === "preview")) {
    // A merge queued during an in-flight submit — the commit did NOT
    // succeed (success clears the flag), so apply it now, before painting.
    applyLateMerge(draft);
  }
  if (!draft) {
    if (verdict !== "red" || !anyEnrichDone()) return null;
    const card = el("div", "card");
    const btn = el("button", "btn primary add-hubspot", "Add to HubSpot…");
    btn.addEventListener("click", () => startAddFlow());
    card.appendChild(btn);
    return card;
  }
  switch (draft.stage) {
    case "resolving":
      return buildFlowSpinner("checking HubSpot for matches…");
    case "resolve_error": {
      const card = el("div", "card");
      card.appendChild(el("div", "banner error", draft.error || "Match check failed."));
      const btns = el("div", "btn-row");
      const retry = el("button", "btn", "Retry");
      retry.addEventListener("click", () => runResolve(currentUrl, draft));
      btns.appendChild(retry);
      const cancel = el("button", "btn link", "Cancel");
      cancel.addEventListener("click", () => clearDraft());
      btns.appendChild(cancel);
      card.appendChild(btns);
      return card;
    }
    case "resolve":
      return buildResolveCard(draft);
    case "form":
      return buildCommitForm(draft);
    case "previewing":
      return buildFlowSpinner("building the preview…");
    case "committing":
      return buildFlowSpinner("writing to HubSpot…");
    case "preview":
      return buildPreviewCard(draft);
    case "hold_domain":
      return buildHoldCard(draft);
    case "conflict":
      return buildConflictCard(draft);
    case "done":
      return buildOutcomeCard(draft);
    default:
      return null;
  }
}

function renderResult(data, opts = {}) {
  lastData = data;
  // Record sheet open: keep it on top. lastData is updated above, so
  // ← Back lands on the freshest recognize payload.
  if (recordView) return renderRecordSheet();
  const verdict = data && data.verdict;

  if (verdict === "idle") return renderIdle();
  if (verdict === "unsupported_surface") {
    return renderUnsupported(pick(data, "message", "reason"));
  }
  if (verdict !== "green" && verdict !== "red") {
    return renderError(0, "Unexpected response from the backend.");
  }

  clearMain();
  setDot("ok", "Connected");

  // Surface kind: prefer the SERVER's classification (every recognize
  // payload carries surface.kind — it's what the payload was built for);
  // fall back to the client classifier only if the payload lacks it.
  const kind = (data.surface && data.surface.kind) || currentSurface;
  // Company surfaces get company treatment: company headline/copy, and
  // never the person flow (enrich buttons, possible matches, add-contact).
  const companySurface = kind === "linkedin_company" || kind === "website";

  const head = el("div", "verdict-line " + verdict);
  head.appendChild(el("span", null, verdict === "green" ? "●" : "●"));
  head.appendChild(
    el(
      "span",
      null,
      verdict === "green"
        ? "Already in HubSpot"
        : companySurface
          ? "Company not in HubSpot"
          : "Not in HubSpot"
    )
  );
  main.appendChild(head);

  if (verdict === "green" && data.contact) {
    main.appendChild(buildContactCard(data.contact));
    // Contact payloads may carry multiple_matches: >1 means the profile
    // matched several HubSpot contact records.
    const nMatches = Number(data.multiple_matches);
    if (isFinite(nMatches) && nMatches > 1) {
      main.appendChild(
        el(
          "div",
          "match-note",
          nMatches + " possible records matched this profile."
        )
      );
    }
  }

  if (verdict === "red" && companySurface) {
    // Company surface with no HubSpot match: company copy, no person flow.
    // name_hint.company_name (server-parsed from the tab title) personalizes
    // the line when present.
    const box = el("div", "state-msg");
    const hintName =
      data.name_hint && data.name_hint.company_name
        ? String(data.name_hint.company_name).trim()
        : "";
    if (hintName) {
      box.appendChild(el("div", "big", hintName + " isn't in HubSpot yet."));
    }
    box.appendChild(
      el(
        "div",
        null,
        kind === "linkedin_company"
          ? "No HubSpot company matches this LinkedIn page. Visit the company's website to match by domain, or browse a person's profile here to add a contact."
          : "No HubSpot company matches this domain."
      )
    );
    main.appendChild(box);
  }

  // Enrichment card — public profile pages only (/enrich needs the tab's
  // public LinkedIn URL). Red = find contact info for a new person; green =
  // refresh what's on the contact.
  if (
    !companySurface &&
    currentSurface === "linkedin_profile" &&
    (verdict === "red" || (verdict === "green" && data.contact))
  ) {
    main.appendChild(
      buildEnrichCard(verdict === "red" ? "Find contact info" : "Refresh email / phone")
    );
  }

  // Phases 3-4: the add-to-HubSpot flow. Either the "Add to HubSpot…"
  // button (red profile with a DONE enrichment result) or the in-progress
  // flow card at whatever stage it's at — drafts survive tab flips via the
  // per-URL session entry, so this renders on green too when a flow is
  // still open (e.g. the success card right after a create).
  if (
    !companySurface &&
    currentSurface === "linkedin_profile" &&
    (verdict === "red" || currentDraft)
  ) {
    const flow = buildAddFlow(verdict);
    if (flow) main.appendChild(flow);
  }

  // Red profile with name-guessed candidates: surface contacts who might be
  // this person but simply lack a LinkedIn URL on file. Never on company
  // surfaces — contact creation starts from a person.
  if (
    !companySurface &&
    verdict === "red" &&
    Array.isArray(data.possible_matches) &&
    data.possible_matches.length
  ) {
    main.appendChild(
      buildPossibleMatches(
        data.possible_matches,
        pick(data, "possible_match_note")
      )
    );
  }

  // Company-surface payloads: candidate HubSpot company records for this
  // domain, with the preferred one highlighted.
  if (Array.isArray(data.company_matches) && data.company_matches.length) {
    main.appendChild(
      buildCompanyMatches(data.company_matches, data.merge_candidate === true)
    );
  }

  // Footnote: cache freshness + manual refresh.
  const meta = el("div", "meta-row");
  if (opts.restored) {
    meta.appendChild(el("span", null, "restored · refreshing…"));
  } else if (data.cached) {
    meta.appendChild(el("span", null, "cached " + fmtAge(data.cache_age_s)));
  } else {
    meta.appendChild(el("span", null, "fresh"));
  }
  meta.appendChild(el("span", "spacer"));
  const refresh = el("button", "btn icon", "↻");
  refresh.title = "Refresh from HubSpot";
  refresh.addEventListener("click", () => recognize({ force: true }));
  meta.appendChild(refresh);
  main.appendChild(meta);
}

// ------------------------------------------------------------------ engine

async function recognize(opts = {}) {
  const url = currentUrl;
  const surface = currentSurface;
  const seq = ++reqSeq;

  if (!opts.background) renderLoading();

  const resp = await chrome.runtime
    .sendMessage({
      type: "recognize",
      url,
      surface,
      page_title: currentTitle,
      force: !!opts.force,
    })
    .catch(() => null);

  if (seq !== reqSeq) return; // a newer tab/request won; drop this one

  if (!resp) {
    renderError(0, "The extension background worker did not respond.");
    return;
  }
  if (!resp.ok) {
    renderError(resp.status, resp.error);
    return;
  }

  await cachePut(url, resp.data);
  renderResult(resp.data);
}

async function onSurfaceChanged(url, surface, pageTitle) {
  currentUrl = url || "";
  currentSurface = surface || "noise";
  currentTitle = pageTitle || "";
  surfaceLabel.textContent =
    surface === "website"
      ? hostnameOf(url)
      : SURFACE_LABELS[surface] || "";

  if (surface === "noise" || !url) {
    reqSeq++; // invalidate any in-flight request
    currentEnrich = {};
    enrichBanner = null;
    currentDraft = null;
    linkBusy = {};
    recordView = null;
    renderIdle();
    return;
  }

  // Sales Nav: nothing to ask the server — enrichment can't use these URLs.
  if (surface === "sales_nav") {
    reqSeq++;
    currentEnrich = {};
    enrichBanner = null;
    currentDraft = null;
    linkBusy = {};
    recordView = null;
    renderUnsupported();
    return;
  }

  // Instant repaint from session cache, then refresh in the background
  // (the server's own 24h cache makes the refresh nearly free).
  const cached = await cacheGet(currentUrl);

  // Restore enrichment state for this URL: completed results render
  // instantly; in-flight jobs resume the "finding…" state and fast-poll
  // (the SW's 30s alarms were running the whole time anyway).
  currentEnrich = (cached && cached.enrich) || {};
  enrichBanner = null;
  // Restore the add-to-HubSpot draft for this URL (an in-progress flow
  // resumes exactly where it was); link-row state is transient per URL.
  currentDraft = (cached && cached.draft) || null;
  linkBusy = {};
  // Restore an open record sheet for this URL. A completed record repaints
  // instantly (via the renderResult guard below); one that never finished
  // loading is re-fetched fresh.
  recordView = (cached && cached.recordView) || null;
  if (recordView && recordView.state !== "done") fetchRecord();
  // Enrichment that finished while this tab was in the background never hit
  // the enrich_update path (it only repaints the on-screen URL) — merge any
  // late results into a restored draft now, same rules as the live path.
  maybeMergeLateEnrich();
  if (inFlightJobIds().length) ensureFastPoll();

  if (cached && cached.response) {
    renderResult(cached.response, { restored: true });
    recognize({ background: true });
  } else {
    recognize({});
  }
}

// -------------------------------------------------------------------- wiring

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || typeof msg !== "object") return;

  if (msg.type === "tab_changed") {
    onSurfaceChanged(msg.url, msg.surface, msg.page_title);
    return;
  }

  // SW re-fetched /status after a job completed — update the header.
  if (msg.type === "status_update") {
    renderCredits(msg.data);
    return;
  }

  // A job poll got a 401 — the rep token is bad/expired. Polling is paused
  // (nothing was finalized); tell the rep how to fix it.
  if (msg.type === "enrich_auth_error") {
    enrichBanner = {
      cls: "error",
      text: "The backend rejected your token — set your rep token in the extension options. The lookup will resume once it's fixed.",
    };
    rerender();
    return;
  }

  // A job for some URL moved (queued/running/done/failed). Only repaint if
  // it's the URL on screen; other URLs pick it up from session storage
  // when the rep returns to them.
  if (msg.type === "enrich_update") {
    if (!currentUrl || urlKey(String(msg.url || "")) !== urlKey(currentUrl)) return;
    cacheGet(currentUrl).then((entry) => {
      if (entry && entry.enrich) currentEnrich = entry.enrich;
      // The job this update is about reached a terminal state → any
      // transient banner ("X is already enriching this profile") is stale.
      const moved = msg.field ? currentEnrich[msg.field] : null;
      if (moved && (moved.state === "done" || moved.state === "failed")) {
        enrichBanner = null;
      }
      // Terminal DONE while an add-to-HubSpot draft is open: flow the late
      // result into the draft's empty fields (rep input always wins).
      if (moved && moved.state === "done") maybeMergeLateEnrich();
      if (inFlightJobIds().length) ensureFastPoll();
      rerender();
    });
  }
});

// First paint: ask the worker what tab is in front right now, and fetch
// /status for the header credit counts.
chrome.runtime
  .sendMessage({ type: "get_active_tab" })
  .then((info) => {
    if (info) onSurfaceChanged(info.url, info.surface, info.page_title);
    else renderIdle();
  })
  .catch(() => renderIdle());

refreshStatus();
