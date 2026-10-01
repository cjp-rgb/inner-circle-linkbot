# Run @TICwebinarbot on your VPS

One command installs it as a background service that restarts on its own and survives reboots.

## Before you start
- Create the webinar group, add @TICwebinarbot as an **admin** (invite users + pin messages), and get the group's chat ID (add @RawDataBot, copy the negative number, remove it).
- Have the bot token from @BotFather ready.
- Make sure the bot is **not** also running on Railway or anywhere else. Two copies with the same token fight each other.

## Install
SSH into the VPS, then run:

```
git clone https://github.com/cjp-rgb/inner-circle-linkbot.git /tmp/ticbot && sudo bash /tmp/ticbot/deploy/install.sh
```

It asks for the token, group chat ID, event name and prize (press Enter to accept the defaults), then starts the bot and shows the last log lines. You want to see `✅ Bot is running.` and `Bot initialised. Group=… prize=$100`.

## Everyday commands
| Task | Command |
|---|---|
| Watch live logs | `journalctl -u ticwebinarbot -f` |
| Restart | `sudo systemctl restart ticwebinarbot` |
| Stop | `sudo systemctl stop ticwebinarbot` |
| Change prize, group, token | `sudo nano /etc/ticwebinarbot.env` then restart |
| Pull the latest code from GitHub | `sudo bash /opt/ticwebinarbot/deploy/install.sh` |

The referral database is at `/var/lib/ticwebinarbot/referrals.db` and is kept across updates and restarts. Pointing the bot at a new group automatically starts a fresh race.

## Test before sharing links
1. DM the bot `/start` → you get your link and the $100 message.
2. From a second account, **click** the link to join.
3. `/leaderboard` in the group → shows 1 referral.
4. `sudo systemctl restart ticwebinarbot`, then `/leaderboard` again → still 1.
