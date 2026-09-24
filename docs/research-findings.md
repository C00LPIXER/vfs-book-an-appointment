# VFS internals — everything extracted from the three reference bots

Repos read (cloned into `research/`, gitignored — not our code):

| Repo | What it actually does |
|---|---|
| `ranjan-mohanty/vfs-appointment-bot` | Playwright driving the booking form, per-country classes (DE, IT). Same shape as ours, no API work. |
| `mominurr/visa.vfsglobal.com` | Scripted browser flows for Italy/Netherlands. Detection only. |
| **`North-web-dev/vfs-monitor`** | **No browser at all.** HTTP login, JWT pool, direct API polling, proxy rotation, Telegram alerts. Everything below comes from here unless noted. |

---

## 1. The endpoint that needs no account at all

```
GET https://lift-api.vfsglobal.com/master/centerwithslots/{mission}/{country}/{visaCategoryCode}/{culture}
    e.g. .../master/centerwithslots/bgr/ind/LONGSTAY/en-US
    headers: accept, origin, referer, route, user-agent      # no JWT, no clientsource, no cookies
```

**Verified against our own mission/country: `200`.** No login, no account, nothing to get restricted.

How to read the answer (their `classify_response`, reproduced exactly):

| Condition | Meaning |
|---|---|
| `"centerName":null` present, or body is `[]` / `[{}]` / `{}` | **no slots** |
| any item with a non-null `centerName` **and** no `error.code` | **SLOT** — that centre has availability |
| HTTP 403 containing `403204` | IP blocked by the WAF |
| HTTP 403 otherwise (usually `Just a moment`) | Cloudflare interstitial |
| HTTP 429, or body code `429001` / `429201` | rate limited |
| HTTP 500 | server error, retry |

Our current LONGSTAY answer is the negative shape (`centerName:null`, `error.code 4100 "Internal
Server Error"`) — that is simply how VFS says "nothing open", not a fault.

**This is a better detector than the earliest-date endpoint we use today**: no login, no lag, and it
names the centres. The earliest-date feed lags 15–60 min behind.

## 2. `clientsource` is self-generatable (verified)

Not a server secret — an RSA-encrypted timestamp. The public key is in `sessionStorage.csk_str` on
the **public** login page.

```python
pub = load_der_public_key(base64.b64decode(sessionStorage["csk_str"]))
clientsource = b64encode(pub.encrypt(str(int(time.time()*1000)).encode(),
    padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)))
```

**Verified: `GET /configuration/fields/bgr/ind` with our own generated value → `200`.**
The login variant encrypts `"GA;<ISO8601>Z"` instead of the millisecond timestamp.

This explains every failure we hit: a replayed `clientsource` is a stale timestamp → `409 Repeated
Delay`; omitting it → `401101 Invalid Request` on repeat calls.

## 3. Logging in without a browser

```
POST /user/login                      (application/x-www-form-urlencoded)
  username, password=<RSA-OAEP(password)>, missioncode, countrycode, languageCode,
  captcha_version=cloudflare-v1, captcha_api_key=<Turnstile token>
  headers: clientsource, cfmlift: mobile, route, origin, referer, sec-ch-ua*, user-agent
```

Before it, two warm-up GETs on the same session, carrying cookies forward:
1. `GET https://visa.vfsglobal.com/{route}/login` (HTML)
2. `GET {LIFT_API}/configuration/fields/{mission}/{country}` (with `clientsource`)

Turnstile + `cf_clearance` come from **CapSolver** (paid). Fake Dynatrace cookies are attached
(`dtCookie`, `rxVisitor`, `rxvt`, `dtPC`, `lt_sn`) to look like a normal session.

## 4. Every numeric rule they use

| Constant | Value | Meaning |
|---|---|---|
| `JWT_TTL` | 21600 s (6 h) | how long a login token is treated as usable |
| `JWT_MAX_AGE` | 90 min | stricter variant in the hunter |
| `REFRESH_AGE_MIN` | 240 min | refresh the pooled JWT at this age |
| `MIN_REFRESH_GAP_SEC` | 900 s | never re-login the same account sooner |
| `MIN_LOGIN_GAP` | 15 s | floor between any two logins |
| `LOGIN_COOLDOWN` | 120 s | after a failed login |
| `RELOGIN_THROTTLE` | 600 s | after `401101`, wait before re-login |
| `INITIAL_COOLDOWN_MIN` | 240 min | new/untested account sits out this long |
| `CYCLE_INTERVAL` | 30 s | gap between hunt cycles |
| `POLL_INTERVAL` | 300 s | slower poller |
| `PACING` | 25 s | between authenticated requests |
| `REQUEST_PACING_SEC` | 0.5 s | between unauthenticated requests |
| `JITTER_PCT` | 0.15 | ±15 % randomisation on every interval |
| `PER_REQ_TIMEOUT` / `DIRECT_TIMEOUT` | 20 s / 6 s | proxy vs direct |
| `MAX_RETRY_PER_REQ` | 3 | retries per request |
| `VERIFY_HITS` | 2 | confirm a hit twice (1.5 s apart) before alerting |
| `ALERT_COOLDOWN` | 180–300 s | do not re-alert the same thing |
| `CF429_ROTATE` | 5 | consecutive `429201` before rotating the IP |
| `REST_429` | 1800 s (30 min) | account benched after that streak |
| `COOLDOWN_AFTER_403_STREAK` | 5 | consecutive 403s before cooling down |
| `COOLDOWN_DURATION_SEC` | 300 s | that cooldown |
| `DEAD_COOLDOWN` | 1800 s | account marked dead this long |
| `STALE` | 1800 s | session considered stale |

## 5. What they do on each failure (the if/else rules)

```
403 + "Just a moment"      -> rotate proxy, do NOT spend a captcha solve; 5 in a row -> 300 s cooldown
403 + "403204"             -> WAF block on this IP -> rotate IP
401 + "401101"             -> JWT is dead -> re-login (wait ~300-600 s first)
429 or "429001"/"429201"   -> throttled -> sleep 2x pacing; 5 in a row -> bench account 30 min + rotate IP
500                        -> transient, retry
body error.code 4100       -> "no slots" (not an error)
body has centerName + no error.code -> SLOT
```

Two channels are raced in parallel (direct + proxy) and the **first useful answer wins**; a `SLOT`
from either beats a `no-slots` from the other. A hit is then verified twice before anyone is woken.

## 6. The news feed nobody watches

`contentful_poller.py` polls VFS's CMS directly — **no auth beyond a public CDN token**:

```
https://cdn.contentful.com/spaces/xxg4p8gt3sg6/environments/master/entries
content types: countryNews, countryNewsflash, countryCallToAction, flashBanner, heroBanner
keywords: appointment, slot, schedul, available, booking, book, reopen, resum, open, new date, additional
```

This is where VFS announces "slots will open on …". Worth polling: it can tell us a release is
coming *before* any slot exists.

## 7. Booking chain (as far as anyone got)

```
POST /appointment/applicants  {missionCode, countryCode, centerCode, loginUser, languageCode}
     -> applicantList[].urn                       # the saved applicant
POST /appointment/calendar    {..., urn}
     -> calendars[].date                          # the real bookable dates
POST /appointment/CheckIsSlotAvailable            -> earliestDate
```

**None of the three repos completes a booking.** They stop at detection and hand off to a human, or
fill the last steps in a browser. There is no payment/confirm call in any of them.

---

## 8. What this means for us

Adopt in this order:

1. **Poll `master/centerwithslots` as the primary detector** — no login, so it cannot get an account
   restricted, and it names the centres. Frequent polling is safe (they use 30 s with ±15 % jitter).
2. **Generate `clientsource` per request** — makes repeated authenticated calls legitimate, so one
   login can check every centre instead of needing 3–4 logins per round.
3. **Keep the JWT for hours** instead of re-logging in (`JWT_TTL` 6 h, refresh at 4 h).
4. **Adopt their failure rules verbatim** (table in §5) — they encode months of learning about when
   VFS blocks and when it forgives.
5. **Watch the Contentful feed** for release announcements.
6. Log in **only** to confirm and book, never to look.
