# Prospecting Plugin — Chrome extension

A pinned side panel that recognizes the person or company on the current tab
against HubSpot, finds emails/phones on demand, and — after an explicit
resolve → preview → confirm flow — creates the contact in HubSpot. It reads
only the **tab URL and title** (via the `tabs` permission) — no content
scripts, no host permissions for the sites you browse, no page reads.

## Load it unpacked

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. **Load unpacked** → select this `extension/` folder.
4. Pin it (puzzle-piece icon → pin). Clicking the icon opens the side panel.
5. Open **Options** (right-click the icon → Options) and set:
   - **Backend URL** — `http://127.0.0.1:8080` for local dev, or your
     deployed backend's URL.
   - **Rep token** — your personal token. It identifies you; every action is
     logged against it. Don't share it.
   - Click **Test connection** — you should see
     "Connected as \<you\> · dry-run ON/OFF".

Then browse to any LinkedIn profile (`linkedin.com/in/…`), LinkedIn company
page, or a company website. The panel recognizes automatically on tab change.

## Pointing it at your backend

The extension only talks to backend origins on a hard allowlist — any other
URL is refused by design (the Options page won't save it, and the service
worker refuses to attach the rep token, or even fetch, off-list). This is
deliberate defense in depth: MV3 `host_permissions` alone does not stop a
service-worker fetch from sending the `Authorization` header to an arbitrary
origin, so a tampered stored URL could otherwise exfiltrate the token.

To use your own backend, update the origin in **all three** places together:

1. `manifest.json` → `host_permissions`
2. `background.js` → `ALLOWED_BACKEND_ORIGINS` (and `DEFAULT_BACKEND`)
3. `options.js` → `ALLOWED_BACKEND_ORIGINS` (and `DEFAULT_BACKEND`)

### The extension id and CORS

An unpacked extension gets a **different id on each machine** (derived from the
folder path). The backend's CORS allowlist (`EXTENSION_ORIGIN`) must include
your install's origin:

1. Find your id on `chrome://extensions`.
2. Set `EXTENSION_ORIGIN=chrome-extension://<your-id>` on the backend, restart.

Publishing the extension (or self-hosting a signed build) gives it a stable
id, so this stops being a per-machine step.

## What each surface shows

| Surface | Behavior |
|---|---|
| LinkedIn profile (`/in/…`) | Person recognition (green = in HubSpot, red = net-new). Priced enrichment buttons; on a net-new person, an **Add to HubSpot** flow. |
| Company website | Company recognition by domain: matches, ICP tier, target flag. |
| LinkedIn company page | Company recognition. |
| Sales Navigator | Not workable — the panel says to open the public `/in/` profile (enrichment vendors can't use Sales Nav URLs). |
| Everything else | Idle. |

## Enrichment (honest async)

On a LinkedIn profile the panel shows priced buttons — **Get work email**,
**Get mobile**, **Get personal email**. Each is independent; every price and
credit number shown comes from the server, never client math.

- **Async.** Clicking starts a background job; the button shows "finding…".
  You can keep browsing or close the panel — the service worker keeps polling
  (a 30s `chrome.alarms` baseline, plus a 3s fast-poll while the panel is
  open) and fires a **desktop notification** when the job finishes. Reopening
  the panel restores in-flight and finished state (results live in
  `chrome.storage.session`).
- **No double-spend.** While a field's lookup is in flight, clicking again
  does nothing; retries reuse the same idempotency key so the server replays
  the existing job instead of reserving credits twice.
- **Team-aware.** If a teammate is already enriching the same profile, the
  panel attaches to their job — you both see the result and nobody is
  double-billed.
- **Result statuses:** `verified` (confirmed), `risky` (may bounce),
  `unknown` (unverifiable), `inferred` (model-guessed — **display only, can
  never be committed**).
- **"Not billed" is only claimed when the server confirms 0 credits.** If the
  extension had to give up client-side (timeout, vanished job), it says
  "billing unknown" instead — check the credit counts in the header.

## Adding a contact (resolve → preview → confirm)

On a net-new LinkedIn profile, once at least one enrichment lookup has
finished, an **Add to HubSpot…** button appears. Nothing is written until the
final click:

1. **Resolve** — "who might this already be?" Exact contact matches are shown
   loudly (if one exists, the create path disappears — you just open it).
   Possible matches and company matches are listed; you pick an existing
   company or deliberately choose **Create new company**.
2. **Preview** — an editable summary (name, title, an email picker that
   **excludes inferred addresses**, phone, company, an optional ICP-tier
   dropdown blank by default, a target-account checkbox). **Preview** asks the
   backend what it *would* do (`confirm:false`, zero side effects) and renders
   the full would-do report, including the resolved owner and where the
   assignment came from.
3. **Confirm** — sends the same payload with `confirm:true` and the same
   idempotency key, so a retried/double-clicked confirm can't create two
   contacts.

If dry-run is on, a confirmed commit writes nothing and shows a prominent
amber **DRY RUN** banner with the would-do report.

**Domain-mismatch hold.** If the chosen email's domain doesn't match the
company's, the backend refuses and the panel offers two honest ways out:
**Discard email** (create without it) or **It's the same company (alternate
domain)** (re-submit acknowledging the alternate). Consumer emails
(gmail/yahoo/…) are flagged; inferred emails are blocked outright.

## Linking a profile to an existing contact

On a net-new profile the panel may list **possible matches** — contacts with
the same name but no LinkedIn URL on file. **Link this profile → \<name\>**
writes just the LinkedIn URL onto that contact (a single-field write) after an
inline confirm, then re-checks the profile (it should go green). If the
contact already has a *different* profile URL, the backend refuses — likely a
different person.

## Files

- `manifest.json` — MV3; permissions `sidePanel`, `tabs`, `storage`,
  `notifications`, `alarms`; host permissions limited to your backend
  origin(s). No content scripts.
- `background.js` — service worker. Watches tab changes (debounced), owns
  **all** backend fetches (the `Authorization` header is attached in exactly
  one place; the token never reaches panel code and never goes in a URL), and
  owns enrichment jobs end-to-end (register → poll → notify).
- `sidepanel.html/css/js` — the panel. All DOM built with
  `createElement`/`textContent` (server data treated as untrusted; no
  `innerHTML`).
- `options.html/js` — backend URL + rep token. The token is stored in
  `chrome.storage.local` and never rendered back into the page (shows
  "ends in …xxxx").

## No build step

Plain ES modules for the pages, a classic script for the service worker. No
npm, no bundler — edit a file, hit reload on `chrome://extensions`.

> This extension ships with **no icons** — Chrome shows its default extension
> icon in the toolbar, and the completion notification uses a blank inline
> icon. To brand it, add icon PNGs, reference them in `manifest.json`
> (`icons` + `action.default_icon`), and set `NOTIF_ICON` in `background.js`.
