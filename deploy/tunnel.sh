#!/usr/bin/env bash
# Expose the local dashboard (127.0.0.1:8787) on a public HTTPS URL so phones can reach it.
#
#   ./deploy/tunnel.sh              # Cloudflare quick tunnel (no account needed)
#   ./deploy/tunnel.sh ngrok        # ngrok instead (needs `ngrok config add-authtoken <token>` once)
#
# The dashboard asks for a password on anything that is not this machine; it is generated on first
# use and stored in data/ui_auth.json (gitignored). This script prints it.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8787}"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR" state

if ! curl -sf -o /dev/null "http://127.0.0.1:$PORT/"; then
  echo "The dashboard is not running on port $PORT — start it first:  vfsbot ui"
  exit 1
fi

password() {
  python3 - <<'PY'
import json, pathlib, secrets
f = pathlib.Path("data/ui_auth.json")
try:
    print(json.loads(f.read_text())["password"])
except Exception:
    pw = secrets.token_urlsafe(9)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"user": "vfs", "password": pw}, indent=2))
    print(pw)
PY
}

case "${1:-cloudflare}" in
  ngrok)
    [ -x "$BIN_DIR/ngrok" ] || {
      echo "Installing ngrok..."
      curl -sL https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz | tar xz -C "$BIN_DIR"
      chmod +x "$BIN_DIR/ngrok"
    }
    echo "Sign in on the phone with:  user vfs  /  password $(password)"
    exec "$BIN_DIR/ngrok" http "$PORT"
    ;;
  *)
    [ -x "$BIN_DIR/cloudflared" ] || {
      echo "Installing cloudflared..."
      curl -sL -o "$BIN_DIR/cloudflared" \
        https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
      chmod +x "$BIN_DIR/cloudflared"
    }
    echo "Sign in on the phone with:  user vfs  /  password $(password)"
    echo "Starting the tunnel — the https://….trycloudflare.com line below is your link:"
    exec "$BIN_DIR/cloudflared" tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate
    ;;
esac
