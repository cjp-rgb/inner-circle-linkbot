#!/usr/bin/env bash
# Install or update the Partner Hub bot on an Ubuntu/Debian VPS as a systemd service.
# Safe to re-run: the first run asks for settings, later runs just update the code
# and restart the bot. Run as root:  sudo bash deploy/install_partner.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/cjp-rgb/inner-circle-linkbot.git}"
APP_DIR=/opt/ticpartnerbot
DATA_DIR=/var/lib/ticpartnerbot          # FTD database lives here (survives updates)
ENV_FILE=/etc/ticpartnerbot.env          # bot token + settings (readable by root + bot only)
SERVICE=ticpartnerbot
BOT_USER=ticpartnerbot

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo bash $0" >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv >/dev/null

PYV=$(python3 -c 'import sys; print("%d%d" % sys.version_info[:2])')
if (( PYV < 39 )); then
  echo "Python 3.9 or newer is needed (found $(python3 --version)). Upgrade the server's Python first." >&2
  exit 1
fi

echo "==> Creating service user and folders"
id -u "$BOT_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$BOT_USER"
mkdir -p "$DATA_DIR"
chown "$BOT_USER:$BOT_USER" "$DATA_DIR"
chmod 700 "$DATA_DIR"

echo "==> Fetching the latest code"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch -q origin
  git -C "$APP_DIR" reset -q --hard origin/HEAD
else
  git clone -q "$REPO_URL" "$APP_DIR"
fi

echo "==> Installing Python dependencies"
[[ -d "$APP_DIR/.venv" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [[ ! -f "$ENV_FILE" ]]; then
  echo
  echo "==> First-time setup. Answers are saved to $ENV_FILE"
  read -r -p "Partner bot token (a NEW bot from @BotFather, not TICwebinarbot): " BOT_TOKEN
  read -r -p "Partner Hub chat ID (starts with -100): " GROUP_ID
  (umask 077; cat > "$ENV_FILE" <<ENV
BOT_TOKEN=$BOT_TOKEN
GROUP_ID=$GROUP_ID
TZ_NAME=Europe/London
HUB_NAME=Partner Hub
DB_PATH=$DATA_DIR/partners.db
ENV
  )
  chown root:"$BOT_USER" "$ENV_FILE"
  chmod 640 "$ENV_FILE"
else
  echo "==> Keeping existing settings in $ENV_FILE (edit that file to change them)"
fi

echo "==> Installing the systemd service"
cat > /etc/systemd/system/$SERVICE.service <<UNIT
[Unit]
Description=Partner Hub FTD leaderboard bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$BOT_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python -m partnerbot.main
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"
sleep 6

echo
if systemctl is-active -q "$SERVICE"; then
  echo "✅ Bot is running."
else
  echo "❌ Bot failed to start. Recent logs:"
fi
journalctl -u "$SERVICE" -n 15 --no-pager
echo
echo "Logs any time:   journalctl -u $SERVICE -f"
echo "Restart:         systemctl restart $SERVICE"
echo "Change settings: nano $ENV_FILE  then restart"
echo "Update the code: sudo bash $APP_DIR/deploy/install_partner.sh"
