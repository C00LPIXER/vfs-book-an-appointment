# VFS Slot Watcher — India → Bulgaria

Built by the **4indegree** tech team · **AAI** (Anas and Amal Intelligence).

Appointment-slot watcher for `visa.vfsglobal.com/ind/en/bgr`. Checks slot availability across all 15
Indian centres and, when a slot opens for the target visa category, alerts the team and calls a
contact over WhatsApp until acknowledged.

---

## 1. What it does

- **Public mode (no login):** polls VFS's public *"earliest available date"* endpoint, which returns
  every centre × every visa category in one call. No login, no OTP. The figure is VFS's *indicative*
  earliest date, carrying a "last updated N minutes ago" stamp.
- **Login mode:** logs into one account and reads the *post-login* availability (per-centre
  `CheckIsSlotAvailable`). More accurate; needs OTP per session and keeps the session alive.
- **Alerting (WhatsApp Web only):** on a slot for the watched category → message to every configured
  contact; contacts in *message + call* mode are then voice-called and re-called every interval until
  they reply the ack keyword. Contacts flagged *bot issues* also get OTP / needs-human / error notices.
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
| `accounts.py`       | account pool: per-account creds / IMAP / proxy / profile, LRU rotation, cool-off benching |
| `proxies.py`        | automatic per-account proxies: cloud server per account (DigitalOcean/Vultr API) + SSH SOCKS tunnel |
| `cli.py`            | `watch` loop, run-window/interval scheduling, state files, instances |
| `notify.py`         | hands every notification to `vfsbot.ack` in a subprocess (never blocks the watcher) |
| `whatsapp_web.py`   | drives an already-linked WhatsApp Web session: message, voice call, read reply |
| `ack.py`            | multi-contact escalation: message all → call the *message_call* ones → poll chats for acks → repeat |
| `otp.py`            | IMAP auto-OTP (reads the OTP from the account's own inbox) |
| `events.py`         | SQLite event log shared with the UI |
| `schedule.py`       | run-window, burst window, randomized `next_delay` |
| `config.py`         | `config.yaml` model (settings only, no secrets) |
| `ui/`               | FastAPI server + single-file dark dashboard |

---

## 3. The public endpoint

From the VFS homepage widget *"Find the Earliest Available Appointment Date"*:

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
covers all 15 centres × all categories. `lastUpdatedValue`/`Type` is the source of the site's
*"Last updated N minutes back"*.

Visa categories seen in the response: `Long Stay D visa`, `Schengen Visa- Less than 90 days`,
`Non-Schengen Visa- Less than 90 days`, `Business`, `Seasonal worker`, `Embassy Approved Interview`
(centres spell some of these slightly differently).

### Cloudflare access data

The `lift-api` host is behind Cloudflare. A request succeeds only if it carries a fresh
`cf_clearance` cookie, which is minted by a browser that passes Cloudflare's check. The cookie is
bound to the **IP + User-Agent** it was issued for and lives ~15–30 min. Observed results:

| Client | Result |
|--------|--------|
| Playwright bundled Chromium (headless or headed) | blocked — `{"code":"403201"}` / Turnstile error `600010` |
| `curl`/`httpx`/Postman, token only, no cf_clearance | `403201` |
| `curl`/`httpx` with fresh cf_clearance + matching browser User-Agent, same IP | `200` (until cookie expires) |
| Postman with `PostmanRuntime` User-Agent + cf_clearance | `403201` (UA doesn't match the cookie) |
| Real Brave/Chrome, in-page `fetch` | `200` (rides the browser's own cf_clearance) |

Any emulation flag (fake locale / timezone / viewport) causes Turnstile to refuse a token; a real
browser with no emulation passes. `vfsbot earliest --curl` prints a ready-to-run curl (fresh cookie
+ all headers) that returns `200` for ~15–30 min.

---

## 4. Timings & behaviour (measured)

| Thing | Observed value |
|------|----------------|
| Cloudflare Turnstile auto-pass (real Brave, cool IP) | ~5 s |
| Sign in → OTP screen | ~5 s |
| OTP submitted → logged in | ~4 s |
| Idle logout (Angular `ng2-idle`, `ng2Idle.main.expiry`) | ~20 min of no activity (keep-alive prevents it) |
| Public earliest-date refresh (`lastUpdatedOn`) | lagging, ~15–60 min (seen 12 / 29 / 37 / 51 min) |
| Public sweep, all 15 centres (in-page fetch) | ~6 s |
| Login-mode per-centre check (dropdown method) | ~45–50 s each |
| `cf_clearance` cookie lifetime | ~15–30 min, IP + UA bound |
| Rate-limit cool-off (observed) | ~2 h |
| Slot release cadence (per the site text) | "new slots every 2nd Monday of the month at 2 pm" (IST) |

Session facts:
- After login the JWT is in `sessionStorage` (`JWT`), plus `loginResponse` in `localStorage`. The
  session exists only while the browser stays open → the watcher keeps one window alive and nudges
  it between polls.
- The **x-auth-token is static** (same value across sessions) — shipped in the SPA.
- **OTP is required on every login** (email / SMS / WhatsApp). IMAP auto-OTP removes the manual step.
- First login of an account requests the **passport bio page** (≤ 2 MB); the bot selects the file
  and either presses Continue itself (`passport_auto_continue`) or waits `passport_wait_minutes` for
  a human to (VFS locks the extracted data). Not a cool-off: the account is retried next round.
- OTP: the inbox is polled every 10 s; humans are only asked if no App Password is set, the inbox
  rejects the login, or no OTP mail has arrived after 60 s.

---

## 5. Login mode & observed blocking

1. **OTP every session.** IMAP (Gmail App Password) for auto-fetch, or typed in the dashboard.
2. **20-min idle logout** → keep-alive.
3. **Cool-offs & escalation.** Repeated logins in a short window raise Cloudflare scrutiny: Turnstile
   stops auto-passing (the widget goes blank / "Verifying…" and never clears), sign-in stalls, and a
   ~2 h cool-off can follow. Observed after ~15+ logins/OTPs in ~90 min.
4. An account can be flagged/blocked for automated or multi-login patterns; when that happens the
   login does not proceed for that account.

### Account rotation & per-account proxy (`vfsbot/accounts.py`, `cli.rotate_loop`)

Login mode does not keep one session alive; every sweep is a fresh identity:

```
pick least-recently-used account (enabled, not cooling off, ≠ last one)
 → launch Brave with that account's own profile (browser-profile/<email-slug>/) and proxy
 → GET ip_check_url through the browser; abort (ProxyError) if the proxy is dead, the IP equals the
   machine's own IP while a proxy is set, or equals the IP the previous account just used
 → login (creds pre-filled, Turnstile auto-pass, OTP from that account's own Gmail via IMAP)
 → per-centre CheckIsSlotAvailable sweep → close browser
```

Bench rules (`data/accounts_state.json`, doubling per consecutive failure, max 24 h):

| Event | Detection | Bench |
|-------|-----------|-------|
| Turnstile cool-off | widget iframe present, no `cf-turnstile-response` token and empty frame body for 90 s | `cooloff_hours` (2 h) |
| WAF block page | `{"code":"403…"}` / "Attention required" | `cooloff_hours` |
| login / OTP timeout | nothing after `login_wait_minutes` (3) | `cooloff_hours / 2` |
| proxy dead / leaking | IP lookup fails or IP unchanged | 1 h |

#### Automatic proxies (`vfsbot/proxies.py`, `config.yaml → proxy_auto`)

With a cloud API token in `data/proxy_provider.json` (dashboard → *Proxy auto-provision*) and
`proxy_auto.enabled`, the rotation loop provisions a server for every enabled account that has no
proxy before using it: `ssh-keygen` (once, `data/proxy_key`) → register key with the provider →
create instance tagged `vfs-proxy` (DigitalOcean `blr1` / `s-1vcpu-512mb-10gb`, or Vultr `bom` /
`vc2-1c-1gb`, Ubuntu 24.04) → poll until active → wait for sshd to accept the key → start
`ssh -N -D 127.0.0.1:<18000+i>` detached → set the account's proxy to `socks5://127.0.0.1:<port>`
(`proxy_auto: true`). `Watcher.__enter__` calls `ensure_tunnel()` for auto accounts, so a dead tunnel
is restarted right before the browser launches. Records in `data/proxies.json`; `vfsbot proxies
status|provision|destroy|tunnels|attach|key`. A proxy typed by hand into the account overrides auto.
Datacenter IPs draw more Turnstile challenges than residential ones.

Own / free-tier servers (`provider: manual`): create the VM yourself (Oracle Cloud Always-Free has
Mumbai/Hyderabad; AWS free tier has Mumbai), add `data/proxy_key.pub` (shown on the dashboard) as
its SSH key, then *Attach server to account* with its IP + SSH user (`ubuntu` / `opc` / `root`). The
bot verifies SSH, records `{provider: manual, ip, user, ssh_port}` and runs the same tunnel;
*Detach* only drops the tunnel, your server is left running. Free VPN services are unsuitable:
shared, already-challenged IPs that change between connections (`cf_clearance` is IP-bound).

After a failure the next account is tried after `retry_minutes` (5); if every account is benched the
watcher sleeps until the first one frees. `cf_clearance` is IP+UA bound, so the pairing
*account ↔ profile ↔ proxy* is kept fixed — a cookie minted for one account's IP is never replayed
from another. Per-account fields (dashboard → Settings → VFS accounts, stored in `data/accounts.json`): `email`, `password`,
`imap_host`, `imap_user` (defaults to email), `imap_password` (Gmail App Password), `proxy`
(`socks5://user:pass@host:port` or `http://…`), `enabled`. Settings in `config.yaml → rotation`.

---

## 6. Setup & usage

```bash
git clone <repo> vfs-book-an-appointment && cd vfs-book-an-appointment
python3 -m venv .venv && source .venv/bin/activate
pip install -e . && playwright install chromium   # a real Brave/Chrome is still required
./deploy/install.sh                                # optional: systemd user service for the web tool
# accounts, passwords, proxies and WhatsApp contacts are entered on the dashboard (Settings tab)
```

Requires a real **Brave** (`/opt/brave.com/brave/brave`) or Google Chrome (non-Flatpak). Playwright's
own Chromium is blocked by VFS; Flatpak Chrome cannot be driven.

```bash
vfsbot ui                     # dashboard at http://127.0.0.1:8787
vfsbot watch --instance public --mode public   # login-free watcher (also runnable from the UI)
vfsbot earliest               # one-shot: print all centres × categories now
vfsbot earliest --available   # only rows with a date
vfsbot earliest --curl        # full API call with a fresh cf_clearance cookie
vfsbot whatsapp-setup         # link WhatsApp Web once (QR scan)
vfsbot test-notify            # test notice to the "bot issues" WhatsApp contacts
```

Config in `config.yaml` (interval, centres + priority, category, run window, burst, WhatsApp contacts,
rotation). Accounts and every other secret live as JSON under `data/` (gitignored) — there is no `.env`.

### WhatsApp escalation

Uses an already-linked WhatsApp Web session in its own Brave profile (`vfsbot whatsapp-setup`).
Contacts (`config.yaml → whatsapp_web.contacts`, edited on the dashboard) each have: `chat` (exact
WhatsApp title), `mode` = `message` | `message_call`, `bot_issues`, optional per-contact
`call_attempts` / `ack_keyword`. On a slot (`python -m vfsbot.ack slot <msg> <key>`):

1. message every enabled contact (baseline = incoming-message count per chat)
2. call round: voice call (`aria-label="Voice call"`, ring `ring_seconds`) to each pending
   `message_call` contact
3. wait `call_interval_seconds`, cycling through the pending chats and checking for a *new* incoming
   message containing that contact's ack keyword; an acked contact is dropped, and with
   `stop_all_on_first_ack` everyone is
4. repeat until every pending contact acked or hit their attempts; acks are recorded in the event log
   and `state/acknowledged.json`. Quiet hours (`notify.quiet_hours_*`) suppress calls, not messages.

`python -m vfsbot.ack notice <text>` sends a message-only notice to the `bot_issues` contacts (OTP,
needs-human, errors, heartbeat). The profile suppresses session-restore so it opens one clean tab.

---

## 7. Running it on another machine

No AI or API keys at runtime — the bot is plain Python (Playwright drives a real Brave, `curl_cffi`
calls the public endpoint, FastAPI serves the dashboard, IMAP reads the OTP).

```bash
git clone <repo> && cd vfs-book-an-appointment
python3 -m venv .venv && source .venv/bin/activate
pip install -e . && playwright install chromium   # a real Brave/Chrome is still required for VFS
vfsbot ui                                         # dashboard on 127.0.0.1:8787
./deploy/tunnel.sh                                # public HTTPS link for phones (+ the password)
```

`data/` (accounts, passwords, IMAP app passwords, proxy state), `state/`, `documents/` and the
`*-profile/` browser sessions are **gitignored** — re-enter the accounts on the Settings tab and run
`vfsbot whatsapp-setup` once, or copy those folders across by hand for an identical setup.

The dashboard trusts requests from the machine itself and asks for a password for anything else
(tunnel, LAN); it lives in `data/ui_auth.json` and `deploy/tunnel.sh` prints it.

### Phone as the bot's IP (`deploy/phone-setup.sh`)

VFS/Cloudflare refuses **hosting ASNs** outright (`403201`) — measured: a DigitalOcean Bangalore IP
(`AS14061`, `hosting: true`) is blocked for both curl and a real browser, while the office Airtel
line (`AS9498`, `hosting: false`) is fine. So cloud VMs, GitHub Actions and datacenter VPNs cannot be
used as exits; only consumer ISP / mobile addresses work.

An Android phone on USB gives exactly that, and a new carrier IP whenever mobile data reconnects
(CGNAT pool). `./deploy/phone-setup.sh` checks adb, switches the phone to mobile data, finds the
tethering interface and gives it the lower route metric; `--revert` puts the office line back.
`config.yaml → ip_rotate` then toggles data before each login and refuses to continue unless the
public IP actually changed; on a VFS throttle the bot rotates and carries on instead of waiting.

## 8. Files & state

- `config.yaml` — settings (committed; no secrets).
- `data/` — **gitignored**, everything secret/runtime as JSON: `accounts.json` (VFS creds, IMAP app
  passwords, proxies), `accounts_state.json` (per-account last use / IP / logins / cool-off),
  `proxy_provider.json` (cloud token), `proxies.json` (provisioned servers), `proxy_key[.pub]`.
- `browser-profile/<email-slug>/` — one Brave profile per rotated account.
- `state/` — `events.db`, `<instance>.json` status, `screenshots/`, logs (gitignored).
- `documents/` — passport image (gitignored). `*-profile/` — live browser sessions (gitignored).
- `deploy/` — systemd unit + install script.
