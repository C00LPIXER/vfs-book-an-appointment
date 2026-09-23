# VFS Slot Status — the simple app

A read-only view for someone who just wants to look. Same data and same hourly check as
`../vfswatch`, but nothing to configure and no alarm: **a notification is the only alert**.

* headline: **No slots open** — or the centres and dates, in green, when something opens
* **VFS UPDATED** (their own stamp, counting up) and **NEXT UPDATE** (when this app looks again)
* every centre with its date, or a dash
* one **Refresh** button

No phone number, no call, no SMS, no settings screen, and the only permission it asks for is
notifications.

## Build & install

```bash
export ANDROID_SDK=$HOME/android/sdk JAVA_HOME=$HOME/android/jdk-17.0.20.1+1
./build.sh install
```

Package `com.fourindegree.vfslite`, so it installs alongside the full watcher without clashing.
