# What the three reference bots do, and what we can take from them

Repos read (cloned into `research/`, gitignored — not our code):

| Repo | Approach |
|---|---|
| `ranjan-mohanty/vfs-appointment-bot` | Playwright/Selenium driving the booking form, country-specific classes (DE, IT). Same shape as ours; no API work. |
| `mominurr/visa.vfsglobal.com` | Scripted browser flows for Italy/Netherlands. Detection only. |
| **`North-web-dev/vfs-monitor`** | **No browser at all** — logs in over HTTP, keeps a pool of JWTs, and polls the API directly. This is the interesting one. |

## The three things that unlock everything

### 1. `clientsource` is an RSA-encrypted timestamp — we can generate it

The SPA does not receive `clientsource` from the server; it computes it:

```python
csk = sessionStorage["csk_str"]          # RSA public key, base64 DER — on the PUBLIC login page
pub = load_der_public_key(base64.b64decode(csk))
clientsource = base64.b64encode(pub.encrypt(str(int(time.time()*1000)).encode(),
    padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))).decode()
```

**Verified on our own account/IP:** `GET /configuration/fields/bgr/ind` with a self-generated
`clientsource` → **200**. This explains every earlier failure: replaying a captured `clientsource`
(a stale timestamp) gave `409 Repeated Delay`, and omitting it gave `401101 Invalid Request` on
repeat calls. With a fresh one per request, repeated API calls are legitimate.

### 2. `cfmlift: mobile` header

Sent on `/user/login` and slot checks. The API answers the mobile client path.

### 3. Cloudflare warm-up before the API call

`GET /{route}/login` (HTML) then `GET /configuration/fields/{mission}/{country}` on the same
session, carrying the cookies forward, before the POST. Cheap and it seeds the cookies the WAF wants.

## How they log in without a browser

```
POST https://lift-api.vfsglobal.com/user/login      (form-encoded)
  username, password=<RSA-OAEP(password)>, missioncode, countrycode, languageCode,
  captcha_version=cloudflare-v1, captcha_api_key=<Turnstile token>
  headers: clientsource, cfmlift: mobile, route, origin, referer, UA
```

The Turnstile token comes from **CapSolver** (paid, ~$1 per 1000). They also buy `cf_clearance`
solves and cache them per proxy session. We do not need this while a real browser + the phone's
mobile IP passes Cloudflare for free — but it is the fallback if that ever stops working.

They then keep a **JWT pool**: one login per account, JWT reused for ~4 h (`REFRESH_AGE_MIN = 240`),
with per-account cooldowns. That is the answer to our "session dies after 3 checks" problem: the
session did not die, we simply had no valid `clientsource` for the follow-up calls.

## The booking endpoints (what we still lack)

```
POST /appointment/applicants   {missionCode, countryCode, centerCode, loginUser, languageCode}
      -> applicantList[].urn          # the saved applicant to book for
POST /appointment/calendar     {..., urn}
      -> calendars[].date             # the actual bookable dates
POST /appointment/CheckIsSlotAvailable  -> earliestDate
```

None of the three repos completes a booking end to end over the API: they stop at detection
(`CheckIsSlotAvailable`) or fill the form in a browser. `/appointment/calendar` + a saved applicant
`urn` is as far as the API work goes.

## What we should adopt, in order

1. **Generate `clientsource` per request** (proven) — makes repeated API slot checks work, so one
   login covers every centre in seconds instead of ~45 s each.
2. **Add `cfmlift: mobile` and the warm-up GET** to match the SPA exactly.
3. **Keep the JWT for ~4 h** after a browser login instead of logging in again per round.
4. **Browser only for login** (Cloudflare + OTP), everything after that over HTTP.
5. Optional, only if Cloudflare starts refusing our mobile IP: CapSolver for Turnstile.
