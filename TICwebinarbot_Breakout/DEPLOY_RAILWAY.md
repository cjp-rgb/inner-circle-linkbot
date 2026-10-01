# Redeploy @TICwebinarbot: The Inner Circle × Breakout Live

About 15 minutes. Do the steps in order.

## 1. Create the webinar group
1. Create a Telegram group called **The Inner Circle × Breakout Live**.
2. Add **@TICwebinarbot** and make it an **admin** with "Invite users via link" and "Pin messages" turned on.
3. Get the group's chat ID: add **@RawDataBot**, copy the `chat id` (a negative number like `-1001234567890`), then remove RawDataBot.

## 2. Check the bot token
In @BotFather → `/mybots` → TICwebinarbot → **API Token**. Copy it.
(If the old token was ever shared anywhere, choose **Revoke** and use the new one.)

## 3. Put the updated code on GitHub
Replace the files in `cjp-rgb/inner-circle-linkbot` with the ones in this zip. The simplest way:
1. On GitHub, open the repo → **Add file → Upload files**.
2. Drag in the contents of this zip (the `bot` folder plus the files next to it).
3. Delete the old duplicate files at the top level of the repo (`config.py`, `db.py`, `links.py`, `leaderboard.py`, `main.py`, `referrals.py`, `get_chat_id.py`, `__init__.py`). The bot only runs the copies inside `bot/`.
4. Commit.

## 4. Create the Railway service
1. railway.app → **New Project** → **Deploy from GitHub repo** → `inner-circle-linkbot`.
2. **Variables**, add each of these:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | the token from step 2 |
| `GROUP_ID` | the chat ID from step 1 |
| `LEADERBOARD_CHAT_ID` | same as `GROUP_ID` |
| `EVENT_NAME` | `The Inner Circle × Breakout Live` |
| `PRIZE` | `$100` |
| `LEADERBOARD_TZ` | `Europe/London` |
| `LEADERBOARD_TIMES` | `13:00,20:00` |
| `DB_PATH` | `/data/referrals.db` |

3. **Settings → Volumes → New Volume**, mount path `/data`. Don't skip this: without it the leaderboard resets to zero every time Railway restarts.
4. Deploy. In the logs you should see `Bot initialised. Group=… prize=$100`.

## 5. Test before sharing anything
1. DM @TICwebinarbot `/start` → you get your personal link with the $100 message.
2. From a second Telegram account, **click** that link to join (don't add it manually; manual adds don't count).
3. Send `/leaderboard` in the group → you should show 1 referral.
4. In Railway, hit **Redeploy**, then `/leaderboard` again → still 1 means the volume works.
5. Remove the test account if you like. Its referral stays credited, so either leave it or run the race knowing you start on 1.

## Notes
- If you ever point the bot at a different group (new `GROUP_ID`), it automatically clears old links and referral counts and starts a fresh race.
- To change the prize later, edit the `PRIZE` variable in Railway. No code change needed.
