# VFS Slot Watcher — India → Bulgaria

Built by the **4indegree** tech team · **AAI** (Anas and Amal Intelligence).

A notify-only appointment-slot watcher for `visa.vfsglobal.com/ind/en/bgr`. It checks slot
availability across all 15 Indian centres and, when a slot opens for the target visa category,
**alerts the team and calls Aslam over WhatsApp until he acknowledges**. Booking is always done
by a human in the applicant's own account.

> **Scope / stance.** This project watches availability and raises alerts. It uses a **real
> browser on a normal residential connection** so Cloudflare passes the same way it does for a
> person. It deliberately does **not** implement IP rotation, proxy/VPN cycling, browser-
> fingerprint spoofing, CAPTCHA-solving, or multi-account rotation — those are anti-bot
> circumvention, they get accounts and IPs banned, and (for watching) they add zero value because
> availability is identical for every account. See **§7**.

---

## 1. What it does

- **Public mode (recommended, no login):** polls VFS's public *"earliest available date"* endpoint,
  which returns every centre × every visa category in one call. Zero login, zero OTP, minimal
  footprint. The figure is VFS's own *indicative* earliest date (it carries a "last updated N
  minutes ago" stamp) — a safe early-warning signal.
- **Login mode (optional):** logs into one account and reads the real *post-login* availability.
  More accurate but higher-maintenance (OTP per session, idle-logout, cool-off risk). See **§5**.
- **Alerting:** on a slot for the watched category → WhatsApp message + call to a configured
  contact, repeated up to N times until they reply an acknowledgement keyword ("ok"). Optional
  Telegram / email / Twilio channels.
- **Dashboard** (`http://127.0.0.1:8787`): live availability matrix with "last updated N minutes
  back", Start/Stop/Pause, OTP box, event log, screenshots, multi-account settings — dark theme.

---

## 2. Architecture

```
 web tool (FastAPI, :8787)  ──start/stop/pause──▶  watcher process (per instance)
   settings · OTP · logs · screenshots · matrix         │
                                                        ▼
                                   real Brave/Chrome (persistent profile)
                                                        │
        ┌───────────────────────────────────────────────┼───────────────────────────┐
        ▼ public mode                                                                 ▼ login mode
  load public page once (passes Cloudflare, mints cf_clearance + reads x-auth-token)   login → OTP → (passport once)
  every N min: in-page fetch centerwithearliestslot → all 15 centres × categories      per-centre CheckIsSlotAvailable
        │                                                                              │
        └───────────────► slot for target category? ──► WhatsApp message + call (ack loop) ◄──┘
```

Key modules (`vfsbot/`):

| File | Role |
|------|------|
| `public_watcher.py` | login-free watcher; in-page `fetch` of the public earliest-date endpoint |
| `watcher.py`        | login watcher (Brave, Cloudflare, OTP, passport, per-centre check), shared `SlotResult` |
| `cli.py`            | `watch` loop, run-window/interval scheduling, state files, instances |
| `notify.py`         | Telegram / email / Twilio call / WhatsApp channels + quiet hours |
| `whatsapp_web.py`   | drives an already-linked WhatsApp Web session: message, voice call, read reply |
| `ack.py`            | escalation orchestrator: message → call → wait for "ok" → repeat → record |
| `otp.py`            | IMAP auto-OTP (reads the OTP from the account's own inbox) |
| `events.py`         | SQLite event log shared with the UI |
| `schedule.py`       | run-window, burst window, randomized `next_delay` |
| `config.py`         | `config.yaml` model + `.env` secrets |
| `ui/`               | FastAPI server + single-file dark dashboard |

---

## 3. The public endpoint (how the fast, no-login check works)

Discovered from the VFS homepage widget *"Find the Earliest Available Appointment Date"*:

```
POST https://lift-api.vfsglobal.com/appointment/centerwithearliestslot
body: {"missionCode":"bgr","countryCode":"ind","cultureCode":"en-US"}
headers: content-type: application/json
         x-auth-token: <static token the SPA sends on public calls>
```

Response (trimmed):

```json
{
  "totalVac": "15",
  "lastUpdatedOn": "2026-9-21 14:14:22",
  "lastUpdatedValue": 12, "lastUpdatedType": "M",   // "updated 12 minutes ago"
  "vacList": [
    { "vacName": "Bulgaria Visa Application Centre-Cochin",
      "visaGroupList": [
        { "displayName": "Long Stay D visa", "earliestAvailableDate": "" },
        { "displayName": "Schengen Visa- Less than 90 days", "earliestAvailableDate": "23 Sep 2026" }
      ] } ...
  ]
}
```

`earliestAvailableDate` is `""` when nothing is open, or a human date when a slot exists. One call
covers all 15 centres × all categories. `lastUpdatedValue`/`Type` is what the site's *"Last updated
N minutes back"* uses; the dashboard shows the same, advancing live between polls.

**Why it needs a browser (and why curl/Postman alone get 403).** The `lift-api` host is behind
Cloudflare. A request only succeeds if it carries a **fresh `cf_clearance` cookie**, and that cookie
can only be minted by a real browser that passes Cloudflare's check. Once minted, the cookie is
bound to the **IP + User-Agent** it was issued for and lives ~15–30 min. So:

- **Playwright's bundled Chromium (headless or headed):** blocked — `{"code":"403201"}` / Turnstile
  error `600010`.
- **Plain `curl`/`httpx`/Postman with only the token:** `403201` (no cf_clearance).
- **`curl`/`httpx`/Postman with a fresh cf_clearance cookie + the matching browser User-Agent, same
  IP:** `200` — works until the cookie expires. (Postman failed in testing because it sends
  `User-Agent: PostmanRuntime`, which doesn't match the cookie; overriding UA + cookie would work.)
- **Real Brave/Chrome, in-page `fetch`:** `200` — the request rides the browser's own cf_clearance.
  This is what the watcher does.

`vfsbot earliest --curl` prints a ready-to-run curl (fresh cookie + all headers) — 200 for ~15–30 min.

---

## 4. Timings & behaviour (measured in this project)

| Thing | Observed value |
|------|----------------|
| Cloudflare Turnstile auto-pass (real Brave, cool IP) | ~5 s |
| Sign in → OTP screen | ~5 s |
| OTP submitted → logged in | ~4 s |
| **Idle logout** (Angular `ng2-idle`, `ng2Idle.main.expiry`) | **~20 min** of no activity → keep-alive prevents it |
| Public earliest-date refresh (`lastUpdatedOn`) | lagging, ~15–60 min (seen 12/29/37/51 min) |
| Public sweep, all 15 centres (in-page fetch) | ~6 s |
| Login-mode per-centre check (dropdown method) | ~45–50 s each (slow; a fast logged-in call is the next optimization) |
| `cf_clearance` cookie lifetime | ~15–30 min, IP+UA bound |
| Rate-limit cool-off (observed) | ~2 h |
| Slot release cadence (per the site) | "new slots every **2nd Monday of the month at 2 pm** IST" |

Session facts:
- After login, the JWT lives in `sessionStorage` (`JWT`), plus `loginResponse` in `localStorage`.
  The session exists only while that browser stays open → the watcher keeps **one** window alive
  and nudges it between polls.
- The **x-auth-token is static** (same value across sessions) — it's shipped in the SPA, not secret.
- **OTP is required on every login** (email/SMS/WhatsApp). Auto-OTP via IMAP removes the human step.
- First login of an account asks for the **passport bio page** (≤ 2 MB); the bot selects the file
  and a human presses Continue (VFS locks the extracted data).

---

## 5. Login mode & the blocking we saw

Login mode works, but is fragile by nature:

1. **OTP every session.** Set IMAP (Gmail App Password) for hands-free, or type it in the dashboard.
2. **20-min idle logout** → keep-alive.
3. **Cool-offs & escalation.** Repeated logins in a short window raise Cloudflare scrutiny: Turnstile
   stops auto-passing (the widget goes blank / "Verifying…" and never clears), sign-in stalls, and a
   ~2 h cool-off can follow. In testing, ~15+ logins/OTPs in ~90 min triggered exactly this.
4. An account can be **flagged/blocked** for automated or multi-login patterns; when that happens the
   login simply won't proceed for that account.

**Root cause of "login not working" is almost always login *churn*, not a code bug.** VFS is far
stricter on repeated *authentication* than on a slow, already-logged-in reader. The durable fix is
to log in **once** and keep the session alive — not to log in more, from more accounts or IPs.

---

## 6. Setup & usage

```bash
git clone <repo> vfs-book-an-appointment && cd vfs-book-an-appointment
python3 -m venv .venv && source .venv/bin/activate
pip install -e . && playwright install chromium   # a REAL Brave/Chrome is still required
cp .env.example .env                               # fill credentials / channels
./deploy/install.sh                                # optional: systemd user service for the web tool
```

Requires a real **Brave** (`/opt/brave.com/brave/brave`) or Google Chrome (non-Flatpak). Playwright's
own Chromium is blocked by VFS; Flatpak Chrome can't be driven.

```bash
vfsbot ui                     # dashboard at http://127.0.0.1:8787
vfsbot watch --instance public --mode public   # login-free watcher (also runnable from the UI)
vfsbot earliest               # one-shot: print all centres × categories now
vfsbot earliest --available   # only rows with a date
vfsbot earliest --curl        # full API call with a fresh cf_clearance cookie
vfsbot whatsapp-setup         # link WhatsApp Web once (QR scan)
vfsbot test-notify            # test Telegram/email/call/WhatsApp
```

Config in `config.yaml` (interval, centres + priority, category, run window, burst, alerts,
whatsapp escalation). Secrets in `.env`. Multiple accounts are managed on the dashboard's Settings
tab (stored in `state/accounts.json`, gitignored) — for **per-applicant booking**, not rotation.

### WhatsApp escalation

Uses an already-linked WhatsApp Web session in its own Brave profile (`vfsbot whatsapp-setup`).
On a slot: message with the details → **voice call** (`aria-label="Voice call"`) → wait
`call_interval_seconds` for a reply containing the ack keyword → repeat up to `call_attempts` →
record the ack in the event log and stop. The profile suppresses session-restore so it opens one
clean tab (no window pile-up).

---

## 7. What this project does NOT do — and why

- **No IP rotation / VPN cycling.** A steady residential IP with a consistent fingerprint is what
  Cloudflare trusts. An IP that hops networks on a timer is a *stronger* bot signal and invites
  harder blocks. Rotating IPs to get past a block is circumventing an access control.
- **No multi-account rotation.** Availability is the same for every account, so extra accounts give
  zero extra visibility for watching — they only multiply logins and rate-limit exposure. Cycling
  accounts to get past a block is ban-evasion.
- **No fingerprint spoofing / CAPTCHA-solving.** We removed *all* emulation (fake locale/timezone/
  viewport) precisely because those get flagged; the real browser passes on its own.

The safe, durable design is the opposite of evasion: **one real browser, one stable IP, human-paced
polling (30–60 min, randomized), one login kept alive.** That's what's implemented here.

---

## 8. Files & state

- `config.yaml` — settings (committed). `.env`, `state/accounts.json` — secrets (gitignored).
- `state/` — `events.db`, `<instance>.json` status, `screenshots/`, logs (gitignored).
- `documents/` — passport image (gitignored). `*-profile/` — live browser sessions (gitignored).
- `deploy/` — systemd unit + install script.

---

*Notify-only. Booking is performed by the applicant in their own account. Respect VFS's terms and a
gentle request rate.*
