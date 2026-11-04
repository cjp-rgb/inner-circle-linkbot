# Inner Circle bot: setup (about 20 minutes)

## 1. Telegram (5 min)
1. @BotFather → /newbot → copy the token. Then /setprivacy → Disable (so it can work in groups).
2. Add the bot to the **old Trade Hub** and the **new group** as **admin**. In the new group give it
   "Invite users via link" and "Add members" (needed to create links and approve join requests).
3. Make a private **admin group**: you, John and the bot. Approve/Reject buttons land here.
4. All three groups use topics, which is fine. Topic ids: open the topic, copy a message link
   (t.me/c/123456/**45**/678 → 45). General is topic 1.
5. Get IDs: add @RawDataBot to each group briefly (or forward a message to it) → note each chat id
   (starts -100…). Your own user id: message @userinfobot. Same for John.

## 2. Install on the VPS (10 min)
From your laptop:
    scp migration-bot.zip root@YOUR_VPS_IP:~
On the VPS:
    apt-get install -y unzip && unzip -o migration-bot.zip -d inner-circle-bot && cd inner-circle-bot
    sudo bash deploy/install.sh
    nano /opt/inner-circle-bot/.env        # fill in token, 3 chat ids, ADMIN_IDS
    systemctl restart inner-circle-bot
    journalctl -u inner-circle-bot -f      # should say "bot running"

## 3. Get your links (2 min)
- You and John each DM the bot `/affiliate CJ` and `/affiliate John`, then tap Approve in the admin chat.
  Those are your personal invite links for the new group. Post yours at the end of the webinar.
- Each affiliate DMs `/affiliate` and then `/tradequo https://their-tradequo-link`. You approve.

## 3b. Partner Hub leaderboard (replaces the old partner bot)
- Fill HUB_CHAT_ID, FTD_TOPIC_ID, LEADERBOARD_TOPIC_ID in .env and restart.
- Turn the old partner bot off so there aren't two leaderboards:  `systemctl disable --now ticpartnerbot`
- In the Partner Hub send `/setleaderboard` (admin). The board posts in the Leaderboard topic and pins.
- Ask every partner to DM the bot `/affiliate` so they get their invite link and appear on the board.
- Re-add this month's existing FTDs with `/addftd @username` (once per FTD).

## 4. Test before 6pm
- From a spare account: tap your invite link → bot DMs → pick "I need a TradeQuo account" → send a UID
  → Approve in the admin chat → check you're let in and receive the setup + tiers message.
- In the admin chat: `/postnow` to test the old-group post, `/status`, `/affiliates`, `/export`.

## Day to day
- Every join request shows: name, UID, upline, which link. **Check the UID in the TradeQuo IB portal, then tap Approve.**
- Affiliates get a DM each time someone joins through their link, with a **Send FTD** button. When they tap it the bot
  checks it (approved, has a UID, came through their link, not already counted), posts ✅ NEW FTD in the FTD topic and
  updates the pinned leaderboard. `/mystats` shows their numbers. Fix mistakes with `/addftd` / `/removeftd @user`.
- Update the bot: copy a new bot.py to /opt/inner-circle-bot and `systemctl restart inner-circle-bot`.
- Data lives in /opt/inner-circle-bot/migration.db. Back it up with `cp`.
