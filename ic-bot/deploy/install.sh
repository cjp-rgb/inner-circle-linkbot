#!/usr/bin/env bash
# Run ON THE VPS from inside the unzipped folder:  sudo bash deploy/install.sh
set -euo pipefail
DIR=/opt/inner-circle-bot
mkdir -p "$DIR"
cp bot.py "$DIR/"
[ -f "$DIR/.env" ] || cp env.example "$DIR/.env"
apt-get update -qq && apt-get install -y -qq python3-venv >/dev/null
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q "python-telegram-bot[job-queue]==21.6" httpx
cp deploy/inner-circle-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable inner-circle-bot >/dev/null
if grep -q '^BOT_TOKEN=$' "$DIR/.env"; then
  echo ""
  echo "Installed. Now fill in $DIR/.env  (nano $DIR/.env)  then run:"
  echo "  systemctl restart inner-circle-bot && journalctl -u inner-circle-bot -f"
else
  systemctl restart inner-circle-bot
  echo "Bot restarted. Logs:  journalctl -u inner-circle-bot -f"
fi
