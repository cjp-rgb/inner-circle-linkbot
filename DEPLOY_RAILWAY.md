# Deploying the Ambassador / Link Bot to Railway

Follow these in order. ~15 minutes. Steps marked **[YOU]** only you can do.

## 1. Rotate the bot token  **[YOU]**
The old token was exposed in a shared zip — it must NOT be reused.
1. Open Telegram → @BotFather
2. `/mybots` → pick the link bot → **API Token** → **Revoke current token**
3. Copy the NEW token. You'll paste it into Railway in step 5.
   (If you'd rather use a brand-new bot: `/newbot`, name it, copy that token.)

## 2. Get the webinar chat ID  **[YOU]**
1. Create the webinar Telegram group (named after the brand once decided).
2. Add the bot to the group as an **admin** (it needs admin to read joins + post the leaderboard).
3. Temporarily set BOT_TOKEN locally and run `python bot/get_chat_id.py`, OR
   add @RawDataBot to the group and read the `chat id` (a negative number like -1001234567890). Remove RawDataBot after.

## 3. Push this folder to a new private GitHub repo  **[YOU]**
```
cd deploy_linkbot
git init
git add .
git commit -m "Ambassador link bot - initial"
git branch -M main
git remote add origin https://github.com/cjp-rgb/inner-circle-linkbot.git   # create this repo first (private)
git push -u origin main
```
The .gitignore already excludes .env, the venv and the .db — nothing secret gets committed.

## 4. Create the Railway service  **[YOU]**
1. railway.app → sign in with GitHub → **New Project** → **Deploy from GitHub repo**
2. Pick `inner-circle-linkbot`. Railway auto-detects Python (Nixpacks) and uses the start command in railway.json.

## 5. Set environment variables in Railway  **[YOU]**
Project → **Variables** → add each:
| Variable | Value |
|---|---|
| `BOT_TOKEN` | the NEW token from step 1 |
| `GROUP_ID` | the chat ID from step 2 |
| `LEADERBOARD_CHAT_ID` | same as GROUP_ID (or a separate channel) |
| `LEADERBOARD_TZ` | `Europe/London` |
| `LEADERBOARD_TIMES` | `13:00,20:00` |
| `EVENT_NAME` | `The Inner Circle Webinar` (or final brand name) |
| `DB_PATH` | `/data/referrals.db`  ← see step 6 |

## 6. Add a PERSISTENT VOLUME (critical — do not skip)
Railway's normal disk wipes on every redeploy. Without a volume, all referral
counts and the leaderboard reset to zero each time you push or it restarts.
1. Project → service → **Settings** → **Volumes** → **New Volume**
2. Mount path: `/data`
3. Confirm `DB_PATH` (step 5) is set to `/data/referrals.db` so the DB lives on the volume.

## 7. Deploy & verify
1. Railway auto-deploys on push. Watch **Deployments → logs**.
2. Look for: `Bot initialised. Group=... leaderboard=...` and the scheduled-times line.
3. In Telegram, DM the bot `/start` → it should reply with your referral link.
4. `/leaderboard` → should show standings.
5. Have a test account join via a referral link → confirm the referrer's count goes up.

## Updating later
Push to GitHub `main` → Railway redeploys automatically. The /data volume keeps your data.
