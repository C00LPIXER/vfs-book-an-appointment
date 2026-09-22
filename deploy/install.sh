#!/usr/bin/env bash
# One-shot setup on the office machine (Fedora/Ubuntu). Run from the project folder.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
. .venv/bin/activate
pip install -q -e .
playwright install chromium >/dev/null   # fallback only; a real Brave/Chrome is required for VFS
if ! command -v brave-browser >/dev/null && [ ! -x /opt/brave.com/brave/brave ] && ! command -v google-chrome >/dev/null; then
  echo "!! Install Brave (https://brave.com/linux) or Google Chrome (.deb/.rpm, not Flatpak) before running."
fi
mkdir -p data state
mkdir -p state documents ~/.config/systemd/user
cp deploy/vfsbot-ui.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vfsbot-ui
loginctl enable-linger "$USER" || true
echo "Web tool: http://127.0.0.1:8787  (open it, fill Settings, press Start)"
