#!/usr/bin/env bash
# Borrow the connected Android phone's mobile connection for the bot, over USB.
#
#   ./deploy/phone-proxy.sh            # start sharing + bridge the port + show the exit IP
#   ./deploy/phone-proxy.sh stop       # stop sharing
#   ./deploy/phone-proxy.sh ip         # just print the IP the bot would use
#
# The phone app (mobile/vfswatch) runs a SOCKS5 server bound to its own loopback; `adb forward`
# bridges it to 127.0.0.1:1080 on this machine. Point an account's Proxy field — and
# `public_proxy` in config.yaml — at socks5://127.0.0.1:1080 and only the bot's traffic leaves
# through the phone's carrier IP. This machine keeps its own line for everything else.
#
# Why bother: Cloudflare refuses hosting ASNs outright for VFS (a DigitalOcean IP gets 403201 even
# in a real browser), while a Jio/Airtel mobile address is what ordinary applicants use. Toggling the
# phone's mobile data also hands you a new IP in seconds if one ever gets rate-limited.
set -euo pipefail
cd "$(dirname "$0")/.."

ADB="${ADB:-$HOME/android/sdk/platform-tools/adb}"
PKG=com.fourindegree.vfswatch
PORT="${PORT:-1080}"

[ -x "$ADB" ] || { echo "adb not found at $ADB (set ADB=/path/to/adb)"; exit 1; }
"$ADB" devices | sed 1d | grep -q "device$" || {
  echo "No authorised phone. Plug it in, enable USB debugging, accept the prompt, then retry."
  "$ADB" devices | sed 1d
  exit 1
}

exit_ip() { curl -s --max-time 25 --socks5-hostname "127.0.0.1:$PORT" https://api.ipify.org || true; }

case "${1:-start}" in
  stop)
    "$ADB" shell am start -n "$PKG/.MainActivity" -e proxy stop >/dev/null 2>&1 || true
    "$ADB" forward --remove "tcp:$PORT" >/dev/null 2>&1 || true
    echo "Sharing stopped. Remember to clear the accounts' Proxy field / public_proxy."
    ;;
  ip)
    echo "through the phone: $(exit_ip)"
    echo "this machine     : $(curl -s --max-time 15 https://api.ipify.org || true)"
    ;;
  *)
    "$ADB" shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1 || true
    "$ADB" shell am start -n "$PKG/.MainActivity" -e proxy start >/dev/null 2>&1
    sleep 6
    "$ADB" forward "tcp:$PORT" "tcp:$PORT" >/dev/null
    ip="$(exit_ip)"
    mine="$(curl -s --max-time 15 https://api.ipify.org || true)"
    if [ -z "$ip" ]; then
      echo "The phone is not answering on port $PORT — open the app and check it says 'ON'."
      exit 1
    fi
    echo "phone exit IP : $ip"
    echo "this machine  : $mine"
    [ "$ip" = "$mine" ] && echo "WARNING: identical — the phone is on WiFi, not mobile data." || true
    echo
    echo "Now put this in the account's Proxy field (dashboard → Settings → VFS accounts):"
    echo "    socks5://127.0.0.1:$PORT"
    echo "and, if you want the public poll to use it too, in 'Proxy for the public poll'."
    ;;
esac
