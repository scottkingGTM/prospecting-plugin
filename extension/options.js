// Prospecting Plugin — options page.
//
// Token hygiene: the token is written to chrome.storage.local and NEVER
// rendered back into the page. After save (and on load, when a token
// exists) the field stays empty and a "token saved ✓, ends in …xxxx" line
// shows instead. "Test connection" uses the CURRENT form values so a rep
// can verify before saving; if the token field is empty it falls back to
// the saved token.

const DEFAULT_BACKEND = "https://YOUR-BACKEND.example.com";

// Exact origins the extension may talk to. Mirror of manifest.json
// host_permissions AND of ALLOWED_BACKEND_ORIGINS in background.js (which
// independently refuses to attach the token anywhere else — defense in
// depth against a tampered stored URL exfiltrating the rep token).
const ALLOWED_BACKEND_ORIGINS = [
  "http://127.0.0.1:8080",
  "https://YOUR-BACKEND.example.com",
];

function isAllowedBackend(url) {
  try {
    return ALLOWED_BACKEND_ORIGINS.includes(new URL(url).origin);
  } catch (e) {
    return false;
  }
}

const urlInput = document.getElementById("backend-url");
const tokenInput = document.getElementById("rep-token");
const tokenState = document.getElementById("token-state");
const statusLine = document.getElementById("status-line");
const testBtn = document.getElementById("test-btn");
const saveBtn = document.getElementById("save-btn");

function setStatus(text, ok) {
  statusLine.textContent = text;
  statusLine.className = ok ? "ok" : "err";
}

function showTokenSaved(token) {
  const last4 = token.length >= 4 ? token.slice(-4) : token;
  tokenState.textContent = "token saved ✓, ends in …" + last4;
}

async function load() {
  const stored = await chrome.storage.local.get(["backendUrl", "token"]);
  urlInput.value = stored.backendUrl || DEFAULT_BACKEND;
  if (stored.token) showTokenSaved(stored.token);
}

function normalizedBackend() {
  const raw = urlInput.value.trim() || DEFAULT_BACKEND;
  return raw.replace(/\/+$/, "");
}

async function effectiveToken() {
  const typed = tokenInput.value.trim();
  if (typed) return typed;
  const stored = await chrome.storage.local.get("token");
  return stored.token || "";
}

testBtn.addEventListener("click", async () => {
  const backendUrl = normalizedBackend();
  if (!isAllowedBackend(backendUrl)) {
    setStatus("URL not in the allowed backend list", false);
    return;
  }
  setStatus("Testing…", true);
  const token = await effectiveToken();
  if (!token) {
    setStatus("No token — paste your rep token first.", false);
    return;
  }
  const resp = await chrome.runtime
    .sendMessage({
      type: "test_connection",
      backendUrl,
      token,
    })
    .catch(() => null);

  if (!resp) {
    setStatus("Background worker did not respond.", false);
  } else if (!resp.ok) {
    setStatus(
      resp.status === 401
        ? "Token rejected (401) — check it and try again."
        : "Failed: " + resp.error,
      false
    );
  } else {
    const d = resp.data || {};
    const bits = ["Connected as " + (d.rep || "unknown rep")];
    if (typeof d.dry_run === "boolean") {
      bits.push(d.dry_run ? "dry-run ON" : "dry-run OFF");
    }
    if (d.version) bits.push("v" + d.version);
    setStatus(bits.join(" · "), true);
  }
});

saveBtn.addEventListener("click", async () => {
  const backendUrl = normalizedBackend();
  if (!isAllowedBackend(backendUrl)) {
    setStatus("URL not in the allowed backend list", false);
    return;
  }
  const typed = tokenInput.value.trim();

  const update = { backendUrl };
  if (typed) update.token = typed;
  await chrome.storage.local.set(update);

  if (typed) {
    tokenInput.value = ""; // never leave the token sitting in the field
    showTokenSaved(typed);
  }
  setStatus("Saved.", true);
});

load();
