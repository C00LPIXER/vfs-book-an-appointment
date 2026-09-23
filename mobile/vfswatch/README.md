# VFS D-Visa Watch — Android app

A standalone watcher for the **public** VFS data. It does not touch the Python bot: it calls the same
no-login endpoint itself, on the phone, and raises an alarm when a D-visa date appears.

* opens on the last-updated time VFS reports (the site refreshes roughly hourly), plus every centre
* checks by itself **every hour**, and again after a reboot
* when a date shows up for the watched category: **alarm sound + full-screen notification + a call**
  to the number set in the app — once per new opening, not on every check
* opening the app stops the alarm

## Why a WebView

The endpoint is behind Cloudflare, which scores the client's TLS fingerprint: `HttpURLConnection`
and OkHttp are answered `403205`, Chromium gets `200`. The app therefore loads the public VFS page in
a hidden WebView (Chromium) and runs the site's own `fetch()` — see `Fetcher.java`.

## Build & install (no Android Studio, no Gradle)

```bash
export ANDROID_SDK=$HOME/android/sdk JAVA_HOME=$HOME/android/jdk-17.0.20.1+1
./build.sh            # aapt2 -> javac -> d8 -> zipalign -> apksigner  => app.apk (~28 KB)
./build.sh install    # the same, then adb install -r and launch
```

Toolchain, once: JDK 17 (Adoptium tarball) and the Android command-line tools with
`platform-tools`, `platforms;android-34`, `build-tools;34.0.0` — all under `~/android`, no root.

## On the phone, once

1. Allow **notifications** and **phone calls** when asked (the call permission is what lets it ring
   you; without it you still get the alarm and the notification).
2. Type the number to call, press **Save**.
3. Press **Test alarm** to hear it and confirm the call goes out.
4. Battery settings → allow the app to run in the background / disable optimisation for it, so the
   hourly alarm is not deferred.
