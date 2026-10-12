# VFS Slot Status — Android app

Watches the Bulgaria long-stay D-visa slots for India on VFS Global's public
feed, once an hour, and rings when one opens.

## Download

**[VFS-Slot-Status-v2.8.apk](VFS-Slot-Status-v2.8.apk)** — tap *Download raw file* on that page.

## Installing

1. Open the file. Android will ask to allow installing from wherever you opened it — allow it.
2. Open the app once and work through **Settings → Permissions & background** until every line has a tick.
   Any line left crossed means the hourly check will be late, silent, or will not happen at all.
3. On vivo, iQOO, Xiaomi, Oppo and Realme phones there is one more, which Android cannot report:
   turn on **Auto-start** and allow **high background power use** in the phone's own battery settings.
   Without it those phones hold the alarm for hours.

## What it does

- Checks once an hour, timed from when VFS itself last published — not from when you last opened the app.
- When a slot appears: rings the caller tone on the alarm stream, vibrates, blinks the camera light,
  and puts an answer screen over the lock screen. It keeps ringing every two minutes until somebody
  types a reply, so a pocket cannot silence it.
- A slot still open at the next check says so quietly instead of ringing again.
- Keeps a history of every check, and a log of the exact request and raw response.
- Tells you when a check fails or VFS rate-limits the phone, rather than going quiet.

It only reads the public feed. No account, no login, no booking.
