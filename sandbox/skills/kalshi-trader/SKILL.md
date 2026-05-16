---
name: kalshi-trader
description: "signl Kalshi prediction-market trading bot. Use whenever the user sends a Telegram message containing trading commands (add/remove/watchlist/positions/buy/sell/alert/balance/analyze/scan), and whenever the 15-minute scan schedule fires. Trigger keywords: signl, kalshi, watchlist, positions, prediction market, buy yes, buy no, alert."
---

<!-- SPDX-License-Identifier: Apache-2.0 -->

# signl — Kalshi Prediction Market Trading Bot

`signl` is an autonomous Kalshi trading bot driven entirely from Telegram.
It maintains a per-user watchlist of Kalshi markets in SQLite. Each market
is in either **auto** mode (the bot sizes and places trades itself using
Nemotron sentiment + quarter-Kelly) or **manual** mode (the bot only
delivers analysis and waits for the user to send `buy`/`sell`).

All trading logic lives in `/home/ubuntu/kalshi-signl/sandbox/workspace/`.
The bot reaches out to:

- `demo-api.kalshi.co/trade-api/v2` for market data and orders (RSA-PSS signed)
- `api.search.brave.com/res/v1/web/search` for recent news headlines
- `integrate.api.nvidia.com/v1` for `nvidia/nemotron-3-super-120b-a12b` sentiment
- `api.telegram.org` for delivering messages to the user (you, the agent, send these)

## Commands the user can send via Telegram

| Telegram message | What it does |
| ---------------- | ------------ |
| `add KXNFLGAME-25NOV24DETGB auto` | Add market to watchlist in auto mode |
| `add KXBITCOIN-26DEC31-150K manual` | Add market in manual mode |
| `remove KXNFLGAME-25NOV24DETGB` | Soft-delete from watchlist |
| `set KXNFLGAME-25NOV24DETGB to manual` | Switch mode |
| `watchlist` *(or `show watchlist`)* | Print current watchlist + prices |
| `positions` | List open positions with unrealised PnL |
| `buy 25 KXNFLGAME-25NOV24DETGB yes at 67` | Manual limit buy at 67¢ |
| `sell 25 KXNFLGAME-25NOV24DETGB at 72` | Manual limit sell at 72¢ |
| `alert KXBITCOIN-26DEC31-150K above 80` | Notify when YES price ≥ 80¢ |
| `alert KXBITCOIN-26DEC31-150K below 30` | Notify when YES price ≤ 30¢ |
| `balance` | Show available Kalshi balance |
| `analyze KXNFLGAME-25NOV24DETGB` | Full sentiment report for a single market |
| `scan` | Trigger an immediate full-watchlist scan |
| `help` | Print the in-bot command list |

## How to handle each incoming Telegram message

For **every** inbound Telegram message addressed to this bot:

1. Run this command via the shell tool (the trailing argument is the user's raw text):

   ```bash
   cd /home/ubuntu/kalshi-signl/sandbox/workspace \
     && python3 watchlist.py "$USER_MESSAGE"
   ```

2. The script prints exactly one JSON object on stdout:

   ```json
   {"ok": true, "message": "...", "data": {...}}
   ```

3. Send `message` back to the user's Telegram chat using the Telegram Bot API
   (`POST https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/sendMessage`) with
   `chat_id` taken from the inbound update (or `TELEGRAM_CHAT_ID` for
   bot-initiated messages). Use Markdown formatting when the message contains
   tables or headlines.

4. If stdout is not valid JSON, or the script exits non-zero, send the raw
   stdout (or the stderr tail) prefixed with `signl error: ` so the user
   sees what broke.

5. If `ok` is `false`, prepend `⚠️ ` to the message before sending.

## Scheduled scan (every 15 minutes)

Schedule the following job to run every 15 minutes:

```bash
cd /home/ubuntu/kalshi-signl/sandbox/workspace \
  && python3 scanner.py scan_all
```

Output shape:

```json
{
  "ok": true,
  "message": "scanned N markets, X updates, Y errors",
  "data": {
    "updates": ["AUTO ... ", "📊 ... (manual)", "🔔 ALERT ..."],
    "errors": ["..."],
    "scanned": N
  }
}
```

For each string in `data.updates`, send a separate Telegram message to
`TELEGRAM_CHAT_ID`. This single path delivers:

- auto-mode trade confirmations (lines starting with `AUTO`)
- manual-mode market updates (lines starting with `📊`)
- triggered price alerts (lines starting with `🔔 ALERT`)

If `data.errors` is non-empty, send a single summary message
`signl scan errors:` followed by the joined error list.

If the scan command itself fails (non-zero exit, non-JSON stdout, or
`ok:false`), send `signl error: <message>` to `TELEGRAM_CHAT_ID`.

## Required environment variables

Loaded from `/home/ubuntu/kalshi-signl/.env` (see `.env.example`):

- `KALSHI_KEY_ID` — Kalshi API key id
- `KALSHI_PRIVATE_KEY_PATH` — PEM-encoded RSA private key (downloaded from Kalshi)
- `NVIDIA_API_KEY` — for Nemotron via `integrate.api.nvidia.com`
- `BRAVE_API_KEY` — for `api.search.brave.com` news search
- `TELEGRAM_BOT_TOKEN` — Bot API token (used by you, not by the Python scripts)
- `TELEGRAM_CHAT_ID` — default destination chat for proactive scan messages
- `SIGNL_DB_PATH` *(optional)* — overrides the default SQLite path

## Safety rules

- Never invent ticker IDs. If `add_market` returns `ticker ... not found on Kalshi`,
  ask the user to confirm the ticker.
- Auto-trade orders are limit orders sized via quarter-Kelly and capped at 5%
  of bankroll (`MIN_EDGE = 5¢`). Do **not** override these constants from
  Telegram input.
- Never call `api.telegram.org` from inside the Python scripts — the
  sandbox policy intentionally blocks Python from Telegram. You (OpenClaw)
  are the only path to the user.
- Treat every script invocation as idempotent enough to retry once on
  network errors, but never replay a `buy`/`sell` automatically.

## First-time setup

```bash
cd /home/ubuntu/kalshi-signl/sandbox/workspace
python3 -m pip install --user -r requirements.txt
python3 db.py    # creates signl.db
```
