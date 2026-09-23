#!/usr/bin/env bash
# Prepare an Android phone as the bot's internet connection (a residential mobile IP that changes on
# demand). Run it with the phone plugged in over USB, USB debugging and USB tethering both on.
#
#   ./deploy/phone-setup.sh            # check + make the phone the default route
#   ./deploy/phone-setup.sh --revert   # give the office line the default route back
#
# The phone's own WiFi is switched off: tethering over WiFi would share the office line and the IP
# would never change. Only mobile data gives a new carrier IP per reconnect.
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ip_now() { curl -s --max-time 15 https://api.ipify.org || true; }

if [ "${1:-}" = "--revert" ]; then
  for c in $(nmcli -t -f NAME,TYPE connection show --active | awk -F: '$2=="ethernet"{print $1}'); do
    dev=$(nmcli -t -f connection.interface-name connection show "$c" | cut -d: -f2)
    case "$dev" in usb*|enp*u*) nmcli connection modify "$c" ipv4.route-metric 600 && echo "demoted $c ($dev)";; esac
  done
  nmcli networking off && sleep 2 && nmcli networking on
  say "Office line restored. Public IP: $(ip_now)"
  exit 0
fi

command -v adb >/dev/null || { echo "adb is missing — run:  sudo dnf install -y android-tools"; exit 1; }

say "1. Phone visible to adb?"
adb devices | sed 1d | grep -q "device$" || {
  echo "   No authorised phone. On the phone: Developer options -> USB debugging ON,"
  echo "   then accept the 'Allow USB debugging?' prompt (tick 'Always allow'). Re-run this."
  exit 1
}
adb devices | sed 1d | grep "device$" | awk '{print "   phone: "$1}'

say "2. Switching the phone to mobile data (WiFi off, data on)"
adb shell svc wifi disable || true
adb shell svc data enable  || true
sleep 6

say "3. USB tethering interface"
dev=""
for i in $(ls /sys/class/net); do
  case "$i" in usb*|enp*u*|rndis*) dev="$i";; esac
done
[ -n "$dev" ] || {
  echo "   Not found. On the phone: Settings -> Hotspot & tethering -> USB tethering ON, then re-run."
  exit 1
}
echo "   interface: $dev"

say "4. Making the phone the preferred route (office line stays as fallback)"
conn=$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: -v d="$dev" '$2==d{print $1}' | head -1)
if [ -n "$conn" ]; then
  nmcli connection modify "$conn" ipv4.route-metric 50
  nmcli connection up "$conn" >/dev/null
  echo "   '$conn' set to metric 50 (lower wins; unplug the phone and the office line takes over again)"
else
  echo "   NetworkManager has no connection for $dev yet — wait a few seconds and re-run."
  exit 1
fi
sleep 4

say "5. Result"
echo "   default route : $(ip route show default | head -1)"
echo "   public IP     : $(ip_now)"
echo
echo "Now open the dashboard -> Settings -> Free IP rotation -> tick 'Rotate IP before every login'"
echo "and press 'Test now'. Two different addresses = done. Run with --revert to undo the routing."
