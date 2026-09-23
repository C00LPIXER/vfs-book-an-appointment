# VFS D-Visa Watch — Android app

A standalone watcher for the **public** VFS data. It does not touch the Python bot: it calls the same
no-login endpoint itself, on the phone, and raises an alarm when a D-visa date appears.

* opens on the last-updated time VFS reports (the site refreshes roughly hourly), plus every centre
* checks by itself **every hour**, and again after a reboot
* when a date shows up for the watched category: **alarm sound + full-screen notification + a call to
  number 1 + an SMS to both numbers** — once per new opening, not on every check
* two views: **Centres** (a readable row per centre with its date) and **Raw JSON** (pretty-printed,
  selectable, with a copy button)
* opening the app stops the alarm

## How the fetch works (three things that each broke it once)

1. **Cloudflare scores the TLS fingerprint.** `HttpURLConnection`/OkHttp are answered `403205`;
   Chromium gets `200`. So the call happens inside a hidden WebView on the real VFS page.
2. **The request must look like the site's own.** Same origin and cookies, the `x-auth-token` the SPA
   sends (read off the page's own request via `shouldInterceptRequest`), and *no invented headers* —
   an extra one such as `route` turns it into a CORS preflight that VFS refuses, which the browser
   reports only as a bare "Failed to fetch".
3. **`evaluateJavascript` does not await Promises.** An `async` function hands back `"null"`
   immediately, which looks exactly like a network failure. The result therefore comes back through a
   `@JavascriptInterface` bridge instead.

## Timing

Each check reads VFS's own "last updated N minutes back" stamp and books the next one for when that
copy is about to be replaced (60 − age + 3 min), so every check lands on fresh data instead of
drifting against VFS's hourly cycle. A failed check retries in 10 minutes.

## Build & install (no Android Studio, no Gradle)

```bash
export ANDROID_SDK=$HOME/android/sdk JAVA_HOME=$HOME/android/jdk-17.0.20.1+1
./build.sh            # aapt2 -> javac -> d8 -> zipalign -> apksigner  => app.apk (~28 KB)
./build.sh install    # the same, then adb install -r and launch
```

Toolchain, once: JDK 17 (Adoptium tarball) and the Android command-line tools with
`platform-tools`, `platforms;android-34`, `build-tools;34.0.0` — all under `~/android`, no root.

## On the phone, once

1. Allow **notifications**, **phone** and **SMS** when asked.
2. Enter number 1 (call + SMS) and optionally number 2 (SMS only), tick "Send an SMS as well as
   calling" if you want texts, press **Save**.
3. Press **Test alarm** to hear it and confirm the call goes out.
4. Battery settings → allow the app to run in the background / disable optimisation for it, so the
   hourly alarm is not deferred.
